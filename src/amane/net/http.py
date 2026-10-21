"""curl_cffi TLS 指纹模拟 + 限速 + 重试; 爬虫 / 图片 / Emby 等对外 HTTP 统一经此模块.

指纹按 host 选用, 被站点拦下时轮换; 契约见 ``WebClient.request``.
"""

import asyncio
import random
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

import aiofiles
import httpx2 as httpx
import structlog
from aiolimiter import AsyncLimiter
from curl_cffi import CurlError
from curl_cffi.requests import AsyncSession, BrowserTypeLiteral, Response

from .connectivity_breaker import ConnectivityBreaker
from .errors import FailureKind, FailureReason, RequestError, RequestFailure
from .proxy_pool import ProxyPool
from .recording import get_bound_http_recorder, skip_body_recording
from .response_cache import CachedResponse, ResponseCache

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from curl_cffi.requests.session import HttpMethod

    from ..config import SiteConfig

logger = structlog.get_logger()


_IMPERSONATE_OPTIONS: tuple[BrowserTypeLiteral, ...] = (
    "chrome123",
    "chrome124",
    "chrome131",
    "chrome136",
    "firefox133",
    "firefox135",
)
"""进程内可用的指纹; 轮换按此顺序取下一个."""

_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})

_IMPERSONATE_REJECTED_STATUSES = frozenset({403, 406})
"""判定「站点拦下此指纹」的状态码: 站点按 TLS 指纹放行时给出的典型响应;
404 表示资源不存在, 换指纹无意义."""

_MAX_IMPERSONATE_ROTATIONS = 3
"""单次请求最多轮换的指纹数. 与重试预算分开计, 因此被拦时单次调用最多发出 4 次请求."""

_RESPONSE_CACHE_MAX_BODY = 2 * 1024**2
"""单条响应缓存正文上限 (字节); 超过不缓存, 图片/视频等大响应天然排除."""

_CACHABLE_CONTENT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml")
"""只缓存文本类内容 (html/json/xml/纯文本) 的 2xx 响应."""

_ADAPT_INTERVAL_S = 1.0
"""同一 host 两次调速的最小间隔 (秒); 429 是站点显式限流信号, 不受此约束."""

_ADAPT_FAST_LATENCY_S = 0.5
"""低于此延迟视为快响应, 累计到 ``_ADAPT_FAST_STREAK`` 次后向配置速率恢复一步."""

_ADAPT_SLOW_LATENCY_S = 3.0
"""高于此延迟视为慢响应, 减速一步."""

_ADAPT_FAST_STREAK = 5
"""恢复所需的连续快响应次数 (防单次幸运命中就把速率拉满)."""

_ADAPT_BACKOFF_429 = 0.5
_ADAPT_BACKOFF_STRESS = 0.85
_ADAPT_BACKOFF_SLOW = 0.9
_ADAPT_RECOVER_STEP = 1.15
_ADAPT_RECOVER_BONUS = 0.05
_ADAPT_MIN_RATE = 0.05
"""绝对速率下限 (次/秒), 防极低配置速率被乘出病理性的请求间隔."""

_ADAPT_BACKOFF_STATUSES = frozenset({408, 500, 502, 503, 504})
"""服务端应激状态: 温和减速. 即 ``_RETRYABLE_STATUS_CODES`` 去掉单独走立即减半的 429."""

_PROXY_BLAME_CURL_CODES = frozenset({5, 6, 7})
"""归咎代理的 curl 错误码: 5 解析代理失败, 6 解析主机失败, 7 无法连接代理.

其余连接错误 (28 超时、35 TLS 握手中断、52 空响应、56 接收失败等) 仅记录 ``host:`` 锁.
单个站点不响应不足以判定代理不可用; 若计入 ``proxy:`` 锁, 一次站点超时即使共享代理开路,
经由该代理的所有站点随之短路 (实测: official 超时开路后 dmm/javdb/javbus 全部短路,
333 个失败任务中 321 个未发出任何请求即判定无数据)."""


def _response_cache_key(
    method: str,
    url: str,
    proxy: str | None,
    fingerprint: str | None,
    headers: dict[str, str] | None,
    data: Any | None,
    json: Any | None,
) -> tuple | None:
    """可缓存的请求才产出 key; 非 GET / 带请求体 / 带 ``Range`` (分片下载) 一律不缓存.

    键含 method+url+代理+指纹+请求头: 请求头影响响应内容 (站点按 Referer/UA 放行), 必须进键,
    否则会把 A 头的响应错发给 B 请求. 指纹与请求头在同一 host 上稳定, 因此跨番号仍能命中.
    """
    if method != "GET" or data is not None or json is not None:
        return None
    if headers and any(k.lower() == "range" for k in headers):
        return None
    return (
        "GET",
        url,
        proxy or "",
        fingerprint or "",
        tuple(sorted((k.lower(), v) for k, v in (headers or {}).items())),
    )


class _FingerprintPicker:
    """按 host 选 TLS 指纹: 换过的指纹对该 host 保持, 后续请求不再试探原指纹.

    host 比来源更精确 (同一来源的 HTML 与图片可能落在不同 host), 比 URL 更稳定 (图片 URL 各不相同).
    """

    def __init__(self, options: tuple[BrowserTypeLiteral, ...]) -> None:
        self._options = options
        self._default: BrowserTypeLiteral = random.choice(options)
        self._chosen: dict[str, BrowserTypeLiteral] = {}
        self._rejected: dict[str, set[BrowserTypeLiteral]] = {}

    @property
    def default(self) -> BrowserTypeLiteral:
        return self._default

    def select(self, host: str) -> BrowserTypeLiteral:
        return self._chosen.get(host, self._default)

    def rotate(self, host: str) -> BrowserTypeLiteral | None:
        """把该 host 当前用的指纹记为拒绝项, 返回下一个未试过的; 全部试过返回 None."""
        rejected = self._rejected.setdefault(host, set())
        rejected.add(self.select(host))
        for option in self._options:
            if option not in rejected:
                self._chosen[host] = option
                return option
        return None


class _AdaptiveLimiter:
    """单 host 的自适应平滑限流器 (P2-⑥, 类 Scrapy AutoThrottle).

    契约 (改造前后都不变的部分): 桶容量 1 的**严格平滑**限速, 不允许突发 ——
    突发会触发反爬检测 (见 ``docs/dev/crawlers.md``). 速率切换时替换内层桶, 容量仍为 1.

    自适应边界: 速率只在 ``[floor_rate, max_rate]`` 内浮动. ``max_rate`` 即该 host 的
    **配置速率**, 是运营者设定的礼貌上限, **永不超过**; ``floor_rate`` 为减速下限,
    保证再悲观也不会把站点限成"几乎不可用".

    观测信号 (每次 ``WebClient.request()`` 结局上报一次):
    - ``429``: 站点显式限流 → 立即减半, 不受适应间隔约束 (再慢一步就可能被封).
    - ``5xx/408`` 或连接级错误 (``status is None``): 服务端应激 → 每个适应间隔减速 15%.
    - 慢响应 (>= ``_ADAPT_SLOW_LATENCY_S``): 减速 10%.
    - 连续 ``_ADAPT_FAST_STREAK`` 次快响应 (<= ``_ADAPT_FAST_LATENCY_S``): 向配置速率恢复一步.
    - 其它状态 (404/403/406/3xx...): 不调速.
    """

    __slots__ = (
        "_adaptive",
        "_clock",
        "_fast_streak",
        "_floor_rate",
        "_last_adapt_at",
        "_limiter",
        "_lock",
        "_max_rate",
        "_rate",
    )

    def __init__(
        self,
        rate: float,
        *,
        adaptive: bool = True,
        floor_ratio: float = 0.25,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_rate = rate
        self._floor_rate = max(rate * floor_ratio, _ADAPT_MIN_RATE)
        self._rate = rate
        self._adaptive = adaptive
        self._clock = clock
        self._limiter = AsyncLimiter(1, 1 / rate)
        self._lock = asyncio.Lock()
        self._last_adapt_at: float | None = None
        self._fast_streak = 0

    @property
    def time_period(self) -> float:
        """当前请求间隔 (秒); 兼容改造前对 ``AsyncLimiter.time_period`` 的既有断言."""
        return self._limiter.time_period

    @property
    def rate(self) -> float:
        """当前速率 (次/秒), 介于 ``floor_rate`` 与配置速率之间."""
        return self._rate

    @property
    def floor_rate(self) -> float:
        """减速下限 (次/秒)."""
        return self._floor_rate

    async def acquire(self) -> None:
        await self._limiter.acquire()

    async def observe(self, latency_s: float | None, status: int | None) -> None:
        """上报一次请求结局 (最终状态码 + 总耗时), 驱动本 host 调速.

        ``latency_s`` 为 ``None`` 时只按状态决策 (429 分支不依赖延迟);
        ``status`` 为 ``None`` 表示连接级失败 (代理拒连 / 超时).
        """
        if not self._adaptive:
            return
        async with self._lock:
            now = self._clock()
            if status == 429:
                # 站点显式限流: 立即减半, 不受适应间隔约束.
                self._last_adapt_at = now
                self._fast_streak = 0
                self._apply(max(self._floor_rate, self._rate * _ADAPT_BACKOFF_429))
                return
            if self._last_adapt_at is not None and now - self._last_adapt_at < _ADAPT_INTERVAL_S:
                return
            if status is None or status in _ADAPT_BACKOFF_STATUSES:
                # 连接错误/超时或服务端应激: 温和减速 (连接级失败另有熔断兜底, 这里只补礼貌).
                self._fast_streak = 0
                self._apply(max(self._floor_rate, self._rate * _ADAPT_BACKOFF_STRESS))
            elif status < 400 and latency_s is not None:
                if latency_s <= _ADAPT_FAST_LATENCY_S:
                    self._fast_streak += 1
                    if self._fast_streak >= _ADAPT_FAST_STREAK:
                        self._fast_streak = 0
                        self._apply(min(self._max_rate, self._rate * _ADAPT_RECOVER_STEP + _ADAPT_RECOVER_BONUS))
                else:
                    self._fast_streak = 0
                    if latency_s >= _ADAPT_SLOW_LATENCY_S:
                        self._apply(max(self._floor_rate, self._rate * _ADAPT_BACKOFF_SLOW))
            self._last_adapt_at = now

    def _apply(self, rate: float) -> None:
        """钳制到 ``[floor, max]`` 后替换内层桶; 无变化时不动 (避免无意义的对象 churn)."""
        rate = min(max(rate, self._floor_rate), self._max_rate)
        if rate == self._rate:
            return
        self._rate = rate
        self._limiter = AsyncLimiter(1, 1 / rate)


class RateLimiters:
    """按 host 的自适应平滑限速 (P2-⑥).

    每个 host 一个 ``_AdaptiveLimiter``: 配置速率是礼貌上限, 观测延迟/状态在
    ``[floor, 配置]`` 内浮动. ``adaptive=False`` 时退化为改造前的固定速率严格平滑桶.

    与熔断 (P0-①) 的分工: 熔断管"连不上就短路", 本类管"连得上但别太吵".
    """

    _BUILTIN_HOSTS = frozenset({"127.0.0.1", "localhost"})
    _LOCALHOST_RATE = 300.0

    def __init__(
        self,
        default_rate: float = 5,
        *,
        adaptive: bool = True,
        floor_ratio: float = 0.25,
    ):
        self._default_rate = default_rate
        self._adaptive = adaptive
        self._floor_ratio = floor_ratio
        self._limiters: dict[str, _AdaptiveLimiter] = {
            "127.0.0.1": _make_limiter(self._LOCALHOST_RATE, adaptive=adaptive, floor_ratio=floor_ratio),
            "localhost": _make_limiter(self._LOCALHOST_RATE, adaptive=adaptive, floor_ratio=floor_ratio),
        }

    @classmethod
    def from_config(
        cls,
        network_rate_limits: Mapping[str, float],
        site_configs: Mapping[str, SiteConfig],
        site_urls: Mapping[str, list[str]],
        *,
        source_rates: Mapping[str, float | None] | None = None,
        default_rate: float = 5,
        adaptive: bool = True,
        floor_ratio: float = 0.25,
    ) -> RateLimiters:
        """优先级: 全局 network.rate_limits > site_config.rate_limit > 默认."""
        instance = cls(default_rate=default_rate, adaptive=adaptive, floor_ratio=floor_ratio)

        # site_config.rate_limit (低优先级)
        for site_name, cfg in site_configs.items():
            if cfg.rate_limit is None:
                continue
            base_urls = list(site_urls.get(str(site_name), []))
            if cfg.base_url:
                base_urls.append(cfg.base_url)
            for url in base_urls:
                host = httpx.URL(url).host
                if host:
                    instance._limiters[host] = _make_limiter(cfg.rate_limit, adaptive=adaptive, floor_ratio=floor_ratio)

        # Plugin descriptors can provide a source-level default without being
        # forced into the core SiteConfig model.
        for source_name, rate in (source_rates or {}).items():
            if rate is None:
                continue
            for url in site_urls.get(str(source_name), []):
                host = httpx.URL(url).host
                if host and host not in instance._limiters:
                    instance._limiters[host] = _make_limiter(rate, adaptive=adaptive, floor_ratio=floor_ratio)

        # network.rate_limits (高优先级, 覆盖上一层)
        for host, rate in network_rate_limits.items():
            instance._limiters[host] = _make_limiter(rate, adaptive=adaptive, floor_ratio=floor_ratio)

        configured = len(instance._limiters) - len(cls._BUILTIN_HOSTS)
        if configured:
            logger.info("rate_limiters.created", configured_hosts=configured, adaptive=adaptive)

        return instance

    def get(self, host: str, *, rate: float | None = None) -> _AdaptiveLimiter:
        if host not in self._limiters:
            self._limiters[host] = _make_limiter(
                rate or self._default_rate, adaptive=self._adaptive, floor_ratio=self._floor_ratio
            )
        return self._limiters[host]

    def set_rate(self, host: str, rate: float) -> None:
        """配置变更 (热重载): 换全新限流器, 自适应状态一并复位到新配置速率."""
        self._limiters[host] = _make_limiter(rate, adaptive=self._adaptive, floor_ratio=self._floor_ratio)

    async def observe(self, host: str | None, latency_s: float | None, status: int | None) -> None:
        """上报一次 ``WebClient.request()`` 结局; ``host=None`` / 未预创建 / 关闭自适应均为无操作."""
        if host is None:
            return
        limiter = self._limiters.get(host)
        if limiter is not None:
            await limiter.observe(latency_s, status)


def _make_limiter(rate: float, *, adaptive: bool = True, floor_ratio: float = 0.25) -> _AdaptiveLimiter:
    """容量 1 的平滑限流器; ``adaptive`` 开启时速率在 ``[rate*floor_ratio, rate]`` 内随观测浮动."""
    return _AdaptiveLimiter(rate, adaptive=adaptive, floor_ratio=floor_ratio)


def site_proxy_overrides(
    site_configs: Mapping[str, SiteConfig],
    site_urls: Mapping[str, list[str]],
) -> dict[str, str | None]:
    """按 host 汇总站点级代理: 值 ``None`` 表示该 host 直连 (站点关闭了代理).

    host 取自来源 profile 的 URL 与配置的 ``base_url``, 与 ``RateLimiters.from_config`` 是同一份集合;
    未出现在结果里的 host 使用全局 ``network.proxy``. 多个站点共享 host 时按遍历顺序最后一次写入生效.
    """
    overrides: dict[str, str | None] = {}
    for site, cfg in site_configs.items():
        if cfg.use_proxy and cfg.proxy is None:
            continue
        for url in (*site_urls.get(str(site), []), cfg.base_url):
            host = httpx.URL(url).host if url else None
            if host:
                overrides[host] = cfg.proxy if cfg.use_proxy else None
    return overrides


def _failure_body(resp: Response | None) -> bytes | None:
    if resp is None:
        return None
    try:
        content = resp.content
    except Exception:
        return None
    return content[:_FAILURE_BODY_LIMIT]


# 失败响应正文保留上限 (防大响应驻留内存)
_FAILURE_BODY_LIMIT = 64 * 1024


def _with_same_origin_referer(
    host: str | None, headers: dict[str, str] | None, hosts: frozenset[str]
) -> dict[str, str] | None:
    """host 命中 ``hosts`` 且调用方未给出 Referer 时补同源 Referer; 已有则保持原值.

    站点按 Referer 前缀匹配, 结尾斜杠属于匹配条件, 不可省略. 不修改传入的字典.
    """
    if host is None or host not in hosts:
        return headers
    if headers is not None and any(k.lower() == "referer" for k in headers):
        return headers
    return {**(headers or {}), "Referer": f"https://{host}/"}


class WebClient:
    """出站 HTTP 通道. 代理按 host 解析 (站点级覆盖全局, 或全局走多代理池), 指纹按 host 选用, 被拦后换下一个."""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        proxy_overrides: Mapping[str, str | None] | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        max_clients: int = 50,
        limiters: RateLimiters,
        same_origin_referer_hosts: frozenset[str] = frozenset(),
        chunked_threshold: int = 2 * 1024**2,
        chunk_size: int = 1 * 1024**2,
        download_concurrency: int = 10,
        connectivity: ConnectivityBreaker | None = None,
        response_cache: ResponseCache | None = None,
        proxy_pool: ProxyPool | None = None,
    ):
        self._proxy = proxy
        self._proxy_overrides = dict(proxy_overrides or {})
        self._timeout = timeout
        self._max_retries = max_retries
        self._limiters = limiters
        self._same_origin_referer_hosts = same_origin_referer_hosts
        self._chunked_threshold = chunked_threshold
        self._chunk_size = chunk_size
        self._download_concurrency = download_concurrency
        self._connectivity = connectivity
        self._response_cache = response_cache
        # P2-⑦ 多代理池: 非空时 (network.proxies 已配置) 取代全局单值 proxy 的角色;
        # 站点级代理覆盖仍优先于池. None = 未配置池, 行为与改造前完全一致.
        self._proxy_pool = proxy_pool
        self._fingerprints = _FingerprintPicker(_IMPERSONATE_OPTIONS)
        self._session = AsyncSession(
            max_clients=max_clients,
            verify=False,
            max_redirects=20,
            timeout=timeout,
            impersonate=self._fingerprints.default,
        )
        # 日志给出进程默认指纹, 便于判断某站点是否按 TLS 指纹放行.
        logger.info("web client created", impersonate=self._fingerprints.default)

    async def acquire(self, url: str) -> None:
        """按 host 取得限速许可. 供不经 ``request`` 的通道 (浏览器渲染 / solver) 复用同一限速."""
        await self._limiters.get(httpx.URL(url).host).acquire()

    def _proxy_for(self, host: str | None, *, exclude: frozenset[str] | None = None) -> str | None:
        """该 host 实际使用的代理: 站点级设定优先, 含显式直连 (值为 ``None``).

        多代理池 (P2-⑦): 配置了 ``network.proxies`` 且该 host 无站点级覆盖时, 从池内按
        健康态轮选 (跳过被熔断开路的); ``exclude`` 供单次请求内的连接失败轮换排除已试过的.
        池内全部开路/被排除时返回 ``None``, 由 ``request`` 短路 (不静默回退单值代理).
        """
        if host in self._proxy_overrides:
            return self._proxy_overrides[host]
        if self._proxy_pool is not None:
            return self._proxy_pool.pick(exclude=exclude)
        return self._proxy

    def _rotate_proxy(self, use_pool: bool, tried: set[str]) -> tuple[str, str] | None:
        """连接级失败后从池内选下一个未试过的健康代理; 非池模式/无剩余代理返回 ``None``.

        返回 ``(proxy, proxy_key)``; 与指纹轮换一样**不占重试预算** —— 死代理的连接 Attempt
        已被熔断器计数, 换个代理重发是同一逻辑请求的延续, 不是新的重试.
        """
        if not use_pool or self._proxy_pool is None:
            return None
        nxt = self._proxy_pool.pick(exclude=frozenset(tried))
        if nxt is None:
            return None
        tried.add(nxt)
        return nxt, f"proxy:{nxt}"

    async def request(
        self,
        method: HttpMethod,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        data: Any | None = None,
        json: Any | None = None,
        use_proxy: bool = True,
        timeout: float | None = None,
        allow_redirects: bool = True,
        ok_statuses: frozenset[int] | None = None,
        max_attempts: int | None = None,
        bypass_circuit: bool = False,
        bypass_cache: bool = False,
    ) -> Response:
        """``ok_statuses`` 额外视为成功 (例如 RSS 304), 不重试、不当失败. 重试用尽后抛 ``RequestError``.

        ``max_retries`` 是首次请求之外的**重试次数** (``2`` → 最多发 3 次请求); ``max_attempts`` 是
        **总尝试次数** 上限, 供一次性的探测向下覆盖 (探测传 1 表示只发一次). 两者都至少发一次请求,
        配置 0 表示不重试而不是一次都不发.

        ``use_proxy=False`` 强制直连 (本机 solver 服务用), 否则按 host 应用站点级代理, 未配置时用全局
        ``network.proxy``; 配置了多代理池 (``network.proxies``) 且该 host 无站点级覆盖时, 代理从池内
        健康态轮选. 代理解析在进入重试循环前一次性完成 (缓存键/熔断键/首次尝试绑定同一个代理).

        403/406 另按「站点拦下此指纹」处理: 换下一个未试过的指纹重发, 换过的指纹对该 host 保持.
        轮换不占重试次数, 单次请求最多换 ``_MAX_IMPERSONATE_ROTATIONS`` 次; 该 host 的指纹全部被拒后
        不再轮换, 按原状态失败.

        多代理池轮换 (P2-⑦): 连接级失败 (``CurlError``/``TimeoutError``) 且代理来自池时, 换池内下一个
        未试过的健康代理重发, 同样不占重试预算; 池内全部试过/开路则按最终失败退出.
        """
        host = httpx.URL(url).host
        headers = _with_same_origin_referer(host, headers, self._same_origin_referer_hosts)
        await self._limiters.get(host).acquire()

        key = host or url
        t0 = time.monotonic()

        # 代理解析上移到循环外 (P2-⑦): 缓存键/熔断键/首次尝试必须绑定同一个代理;
        # 循环内仅在连接级失败时轮换到池内下一个健康代理 (见 _rotate_proxy).
        use_pool = use_proxy and self._proxy_pool is not None and host not in self._proxy_overrides
        proxy = self._proxy_for(host) if use_proxy else None
        tried_proxies: set[str] = {proxy} if proxy is not None else set()
        proxy_key = f"proxy:{proxy}" if proxy else None

        # 跨任务 HTTP 响应缓存 (P2-⑤): 命中即返回, 不触网、不进熔断统计 —— 因此熔断开路时也能用缓存兜底.
        # 放在熔断检查之前是有意为之: 代理/host 开路不等于「没有可用数据」, 刚缓存的热页仍应服务.
        # 诊断探测 (bypass_circuit) 与强制刷新 (bypass_cache) 一律不走缓存.
        # 池模式下代理分量归一为空串: 池内代理是运营者声明等价的多出口, 按代理分会把缓存打散成
        # N 份 (轮询导致同 URL  consecutive 请求交替 miss); 归一后任一代理取到的响应全池共享.
        # 取一次本地引用: 属性在多次读取间可能被热重载换掉, 且这样类型收窄才对 cache_key 的判定成立.
        cache = self._response_cache
        cache_key = None
        if cache is not None and cache.enabled and not bypass_circuit and not bypass_cache:
            cache_key = _response_cache_key(
                method,
                url,
                "" if use_pool else proxy,
                self._fingerprints.select(key),
                headers,
                data,
                json,
            )
        if cache is not None and cache_key is not None:
            cached = cache.get(cache_key)
            if cached is not None:
                self._record_exchange(method, url, resp=cached, error=None, t0=t0, attempts=1)
                # CachedResponse 是 Response 的鸭子类型替身 (curl 的 Response 绑定 easy handle,
                # 无法离线构造), 调用方用到的字段完全一致, 故按 Response 返回.
                return cast(Response, cached)

        # 连接健康熔断: 代理或 host 已被连续失败判定为不可用 → 直接短路, 不再支付「重试 + 镜像 + 超时」预算.
        # 诊断类一次性探测传 bypass_circuit=True, 永远反映真实状态, 不被自身开路阻断 (见 design doc 4.4).
        host_key = f"host:{host}" if host else None
        if use_pool and proxy is None:
            # 池内代理全部被熔断开路: 与单代理开路同样短路, 不白付连接超时预算.
            # 放在缓存查询之后: 与单代理语义一致 —— 开路不等于没有可用数据, 缓存热页仍服务.
            raise RequestError(
                url,
                RequestFailure(
                    kind=FailureKind.CURL,
                    reason=FailureReason.CIRCUIT_OPEN,
                    message="proxy pool exhausted: every proxy circuit open",
                ),
            )
        if self._connectivity is not None and not bypass_circuit:
            # 池模式下 proxy_key 不在此复查: pick 已做过健康判定并消耗掉半开探针名额,
            # 这里再查一次 is_open 会把「冷却后放行的探针」误判为阻塞, 自动恢复就断了.
            for ck in (host_key,) if use_pool else (proxy_key, host_key):
                if ck is not None and self._connectivity.is_open(ck):
                    raise RequestError(
                        url,
                        RequestFailure(
                            kind=FailureKind.CURL,
                            reason=FailureReason.CIRCUIT_OPEN,
                            message=f"circuit open: {ck}",
                        ),
                    )

        total_attempts = 1 + self._max_retries
        attempts = max(1, total_attempts if max_attempts is None else min(max_attempts, total_attempts))
        failure: RequestFailure | None = None
        resp: Response | None = None
        sent = 0
        retries = 0
        rotations = 0
        while True:
            sent += 1
            resp = None
            impersonate = self._fingerprints.select(key)
            should_retry = False
            try:
                resp = await self._session.request(
                    method,
                    url,
                    headers=headers,
                    cookies=cookies,
                    data=data,
                    json=json,
                    proxy=proxy,
                    timeout=timeout or self._timeout,
                    allow_redirects=allow_redirects,
                    impersonate=impersonate,
                )
                extra_ok = ok_statuses or frozenset()
                if (
                    resp.status_code < 300
                    or resp.status_code in extra_ok
                    or (resp.status_code in (301, 302, 307, 308) and resp.headers.get("Location"))
                ):
                    # 只缓存 2xx 文本类 GET 响应; 下载/分片/错误响应不进缓存.
                    if cache_key is not None and resp.status_code < 300:
                        self._store_response(cache_key, url, resp)
                    self._record_exchange(method, url, resp=resp, error=None, t0=t0)
                    await self._connectivity_success(host_key, proxy_key)
                    # 自适应限速 (P2-⑥): 成功结局也上报, 驱动恢复/减速决策.
                    await self._limiters.observe(host, time.monotonic() - t0, resp.status_code)
                    return resp

                failure = RequestFailure(
                    kind=FailureKind.HTTP_STATUS,
                    status=resp.status_code,
                    message=f"HTTP {resp.status_code}",
                    body=_failure_body(resp),
                )
                should_retry = resp.status_code in _RETRYABLE_STATUS_CODES
                # 拿到 HTTP 响应即证明链路可用, 复位两把锁. 4xx/5xx 属站点侧状态, 与连接无关;
                # 不复位会使半开探针停留在 HALF_OPEN (该状态的 is_open 恒为 True), 该键此后一直短路.
                await self._connectivity_success(host_key, proxy_key)

            except CurlError as e:
                failure = RequestFailure(kind=FailureKind.CURL, message=f"curl error: {e}")
                # 连接级错误 (代理拒连 / host 不可达): 立即 fail-fast, 不支付重试预算;
                # 由上游熔断开路, 后续请求在毫秒级短路.
                should_retry = False
                # 仅代理侧错误码连带记录 proxy 锁; 站点不响应 (28) 只记录 host 锁, 不影响其它站点.
                await self._connectivity_failure(host_key, proxy_key if e.code in _PROXY_BLAME_CURL_CODES else None)
                # 多代理池 (P2-⑦): 该代理来自池且池内还有未试过的健康代理 → 更换后重发
                # (与指纹轮换一致, 不占重试预算; 失效代理的熔断计数已在上一步累加).
                rotated = self._rotate_proxy(use_pool, tried_proxies)
                if rotated is not None:
                    proxy, proxy_key = rotated
                    logger.warning("proxy rotated", url=url, proxy=proxy, error=failure.message)
                    continue
            except TimeoutError:
                failure = RequestFailure(kind=FailureKind.TIMEOUT, message="timeout")
                # 超时保留重试 (可能为慢服务器), 同时记录失败以便持续超时时开路.
                # 超时仅记录 host 锁: 单个站点不响应不足以判定代理不可用.
                should_retry = True
                await self._connectivity_failure(host_key, None)
                # 超时同样可能是代理质量问题: 池内有备用代理时换一个再试.
                rotated = self._rotate_proxy(use_pool, tried_proxies)
                if rotated is not None:
                    proxy, proxy_key = rotated
                    logger.warning("proxy rotated", url=url, proxy=proxy, error="timeout")
                    continue
            except Exception as e:
                failure = RequestFailure(kind=FailureKind.UNEXPECTED, message=f"unexpected: {type(e).__name__}: {e}")

            if resp is not None and resp.status_code in _IMPERSONATE_REJECTED_STATUSES:
                # 先记录拒绝项并选出下一个: 预算用尽时它留给该 host 的下一次请求, 不重复试探同一个指纹.
                rotated = self._fingerprints.rotate(key)
                if rotated is None:
                    logger.warning("host rejected every fingerprint", url=url, status=resp.status_code)
                elif rotations < _MAX_IMPERSONATE_ROTATIONS:
                    rotations += 1
                    logger.warning(
                        "impersonate rotated",
                        url=url,
                        status=resp.status_code,
                        rejected=impersonate,
                        impersonate=rotated,
                    )
                    # 轮换重发是同一 host 的额外请求, 与首次请求一样先取限速许可: 严格平滑桶不允许突发.
                    await self._limiters.get(host).acquire()
                    continue

            if not should_retry or retries >= attempts - 1:
                break
            wait = retries * 3 + 2 + random.uniform(-1, 1)
            retries += 1
            logger.warning(
                "request retry",
                method=method,
                url=url,
                attempt=retries,
                max_retries=attempts,
                error=failure.message if failure else None,
                retry_in=wait,
            )
            await asyncio.sleep(wait)

        log_failed = logger.debug if get_bound_http_recorder() is not None else logger.error
        log_failed(
            "request failed",
            method=method,
            url=url,
            error=failure.message if failure else None,
            attempts=sent,
            duration_s=round(time.monotonic() - t0, 2),
        )
        self._record_exchange(method, url, resp=resp, error=failure.message if failure else None, t0=t0, attempts=sent)
        # 自适应限速 (P2-⑥): 429/5xx/连接错误在此汇总成一次观测 (重试过程中的中间状态见日志).
        await self._limiters.observe(host, time.monotonic() - t0, failure.status if failure else None)
        raise RequestError(url, failure)

    def _store_response(self, cache_key: tuple, url: str, resp: Response) -> None:
        """把 2xx 文本类 GET 响应写入缓存; 大响应/二进制/异常一律跳过 (下载与分片不占缓存)."""
        cache = self._response_cache
        if cache is None:
            return
        try:
            content = resp.content
        except Exception:
            return
        if content is None or len(content) > _RESPONSE_CACHE_MAX_BODY:
            return
        content_type = (resp.headers.get("Content-Type") or resp.headers.get("content-type") or "").lower()
        if not any(content_type.startswith(prefix) for prefix in _CACHABLE_CONTENT_TYPES):
            return
        raw_headers: dict[str, str] = {}
        try:
            # 头值可能是 None (缺值头), 缓存里只保留有值的项.
            raw_headers = {str(k): str(v) for k, v in dict(resp.headers).items() if v is not None}
        except Exception:
            raw_headers = {}
        final_url = str(resp.url) if resp.url else url
        cache.put(
            cache_key,
            CachedResponse(
                status_code=resp.status_code,
                headers=raw_headers,
                content=content,
                url=final_url,
            ),
        )

    async def _connectivity_success(self, host_key: str | None, proxy_key: str | None) -> None:
        """成功出站后复位对应 key 的失败计数 (开路 → 关闭, 健康保持)."""
        breaker = self._connectivity
        if breaker is None:
            return
        if host_key:
            await breaker.record_success(host_key)
        if proxy_key:
            await breaker.record_success(proxy_key)

    async def _connectivity_failure(self, host_key: str | None, proxy_key: str | None) -> None:
        """连接级失败后累加对应 key 的失败计数; 达阈值由熔断器开路."""
        breaker = self._connectivity
        if breaker is None:
            return
        if host_key:
            await breaker.record_failure(host_key)
        if proxy_key:
            await breaker.record_failure(proxy_key)

    def _record_exchange(
        self,
        method: str,
        url: str,
        *,
        resp: Response | CachedResponse | None,
        error: str | None,
        t0: float,
        attempts: int | None = None,
    ) -> None:
        rec = get_bound_http_recorder()
        if rec is None:
            return
        content_type: str | None = None
        status: int | None = None
        body: bytes | None = None
        if resp is not None:
            status = resp.status_code
            content_type = resp.headers.get("Content-Type") or resp.headers.get("content-type")
            try:
                body = resp.content
            except Exception:
                body = None
        rec.record_http(
            method=str(method),
            url=url,
            status=status,
            error=error,
            content_type=content_type,
            body=body,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            attempts=attempts,
        )

    async def get_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        encoding: str = "utf-8",
        use_proxy: bool = True,
    ) -> str:
        resp = await self.request("GET", url, headers=headers, cookies=cookies, use_proxy=use_proxy)
        try:
            resp.encoding = encoding
            return resp.text
        except Exception as e:
            raise RequestError(url, RequestFailure(kind=FailureKind.UNEXPECTED, message=f"decode error: {e}")) from e

    async def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        use_proxy: bool = True,
    ) -> Any:
        resp = await self.request("GET", url, headers=headers, cookies=cookies, use_proxy=use_proxy)
        try:
            return resp.json()
        except Exception as e:
            raise RequestError(
                url, RequestFailure(kind=FailureKind.UNEXPECTED, message=f"JSON parse error: {e}")
            ) from e

    async def get_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        use_proxy: bool = True,
    ) -> bytes:
        with skip_body_recording():
            resp = await self.request("GET", url, headers=headers, cookies=cookies, use_proxy=use_proxy)
        return resp.content

    async def post_text(
        self,
        url: str,
        *,
        data: Any | None = None,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        encoding: str = "utf-8",
        use_proxy: bool = True,
    ) -> str:
        resp = await self.request(
            "POST", url, data=data, json=json, headers=headers, cookies=cookies, use_proxy=use_proxy
        )
        try:
            resp.encoding = encoding
            return resp.text
        except Exception as e:
            raise RequestError(url, RequestFailure(kind=FailureKind.UNEXPECTED, message=f"decode error: {e}")) from e

    async def post_json(
        self,
        url: str,
        *,
        data: Any | None = None,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        use_proxy: bool = True,
    ) -> Any:
        resp = await self.request(
            "POST", url, data=data, json=json, headers=headers, cookies=cookies, use_proxy=use_proxy
        )
        try:
            return resp.json()
        except Exception as e:
            raise RequestError(
                url, RequestFailure(kind=FailureKind.UNEXPECTED, message=f"JSON parse error: {e}")
            ) from e

    async def _probe(self, url: str, *, use_proxy: bool) -> tuple[int | None, str]:
        """HEAD 探测 Content-Length 与重定向终址; 探测失败时终址回退为 ``url``."""
        try:
            resp = await self.request("HEAD", url, use_proxy=use_proxy)
        except RequestError:
            return None, url
        final_url = str(resp.url) if resp.url else url
        if resp.status_code >= 400:
            return None, final_url
        cl = resp.headers.get("Content-Length")
        try:
            return (int(cl) if cl else None), final_url
        except ValueError, TypeError:
            return None, final_url

    async def get_filesize(self, url: str, *, use_proxy: bool = True) -> int | None:
        size, _ = await self._probe(url, use_proxy=use_proxy)
        return size

    async def resolve_final_url(self, url: str, *, use_proxy: bool = True) -> str:
        """跟随重定向后的终址; 探测失败时返回 ``url``.

        调用方据此判定上游是否改派了别的资源 (如占位图).
        """
        _, final_url = await self._probe(url, use_proxy=use_proxy)
        return final_url

    async def download(self, url: str, dest: Path, *, use_proxy: bool = True) -> bool:
        """下载. 分块阈值、分片大小与并发数取自 ``network`` 配置. 失败返回 False, 并按状态与原因记日志."""
        file_size, _ = await self._probe(url, use_proxy=use_proxy)

        if file_size and file_size > self._chunked_threshold:
            return await self._download_chunked(url, dest, file_size, use_proxy=use_proxy)

        try:
            content = await self.get_bytes(url, use_proxy=use_proxy)
        except RequestError as e:
            logger.error(
                "download failed",
                url=url,
                status=e.failure.status if e.failure else None,
                reason=e.reason,
                error=e.message,
            )
            return False

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(dest, "wb") as f:
                await f.write(content)
            return True
        except Exception as e:
            logger.error("file write failed", path=str(dest), error=str(e))
            return False

    async def _download_chunked(self, url: str, dest: Path, file_size: int, *, use_proxy: bool = True) -> bool:
        chunk_size = self._chunk_size
        parts = [(s, min(s + chunk_size - 1, file_size - 1)) for s in range(0, file_size, chunk_size)]

        logger.info("chunked download started", url=url, chunks=len(parts), size=file_size)

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(dest, "wb") as f:
                await f.truncate(file_size)
        except Exception as e:
            logger.error("file create failed", path=str(dest), error=str(e))
            return False

        semaphore = asyncio.Semaphore(self._download_concurrency)

        async def _fetch_chunk(start: int, end: int) -> RequestFailure | None:
            async with semaphore:
                try:
                    resp = await self.request(
                        "GET", url, headers={"Range": f"bytes={start}-{end}"}, use_proxy=use_proxy
                    )
                except RequestError as e:
                    return e.failure or RequestFailure(kind=FailureKind.UNEXPECTED, message=e.message)
                # 不校验长度与 Content-Range: 上游忽略 Range 时返回整份内容, 这里按偏移写入, 落盘字节数与分片不符.
                async with aiofiles.open(dest, "rb+") as f:
                    await f.seek(start)
                    await f.write(resp.content)
                return None

        results = await asyncio.gather(*[_fetch_chunk(s, e) for s, e in parts], return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error("chunk download failed", chunk=i, url=url, error=str(result))
                return False
            if result is not None:
                logger.error("chunk download failed", chunk=i, url=url, status=result.status, error=result.message)
                return False

        logger.info("chunked download complete", url=url)
        return True

    async def close(self) -> None:
        try:
            await self._session.close()
        except Exception as e:
            logger.debug("session close error (ignored)", error=str(e))
