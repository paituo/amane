import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from ..enums import ActorGender
from ..net.connectivity import ConnectivityOutcome
from ..net.errors import FailureReason, RequestError
from ..plugins.models import SourceCapability, SourceDescriptor, SourceTrait
from .http import HttpClient

if TYPE_CHECKING:
    from ..aggregate.engine import CrawlerLike
    from ..config import SiteConfig
    from ..enums import SiteName
    from .connectivity import ConnectivityProbe
    from .models import FetchOptions, MediaMetadata, SearchQuery


@dataclass
class RequestContext:
    base_url: str
    cookies: dict[str, str]


@dataclass
class CrawlerProfile:
    name: SiteName | str
    base_url: str
    mirrors: list[str] = field(default_factory=list)
    """同一后端的备用入口 (官方轮换域名). 仅在连接层失败时按顺序轮换, 页面结构须与 ``base_url`` 一致."""
    urls: list[str] = field(default_factory=list)
    cookies: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    # 该站静态资源校验同源 Referer 时置 True: 下载图片按 host 注入 ``https://{host}/``.
    same_origin_referer: bool = False
    # 空则影片爬虫视为 film_metadata; 演员爬虫必须显式声明 profile / image.
    capabilities: frozenset[SourceCapability] = field(default_factory=frozenset)
    # 行为开关: 引擎读到某个取值就改变调度或调用方式 (排段 / 多语言 / 文件指纹), 取值见 ``SourceTrait``.
    # 与 capabilities (来源提供什么) 分开.
    traits: frozenset[SourceTrait] = field(default_factory=frozenset)
    genders: frozenset[ActorGender] | None = None

    def effective_capabilities(self) -> frozenset[SourceCapability]:
        return self.capabilities or frozenset({SourceCapability.FILM_METADATA})

    def to_descriptor(self) -> SourceDescriptor:
        """引擎与配置读取的声明性事实都在这里产出, 新增 profile 字段时同步此处."""
        return SourceDescriptor(
            id=str(self.name),
            name=str(self.name),
            version="builtin",
            capabilities=frozenset(self.effective_capabilities()),
            urls=(*self.mirrors, *self.urls, self.base_url),
            traits=frozenset(str(trait) for trait in self.traits),
        )


_SWITCHABLE_REASONS = frozenset({FailureReason.NETWORK, FailureReason.TIMEOUT})
"""换入口值得重试的失败原因: 只有连接层不可达才换.

拦截页 / 年龄验证 / 4xx 换域无意义 (同一后端的镜像会给同样的回应), 换掉反而掩盖真实原因.
"""


def _switchable(exc: RequestError) -> bool:
    return exc.reason in _SWITCHABLE_REASONS


class Crawler(ABC):
    """默认 ``_search`` → ``_scrape``. 无状态: SiteConfig 经方法参数注入, 不经构造函数.

    特殊源可直接 override ``fetch()``.
    """

    @classmethod
    @abstractmethod
    def profile(cls) -> CrawlerProfile: ...

    def __init__(self, client: HttpClient, config: SiteConfig | None = None):
        self._profile = self.profile()
        self.name = self._profile.name
        self.client = client
        self.config = config

        self.base_url = self._profile.base_url
        self.cookies = dict(self._profile.cookies)
        self.headers = dict(self._profile.headers)
        self._resolve_config()
        self._origins = self._build_origins()
        self._origin_index = 0

    def _build_origins(self) -> tuple[str, ...]:
        """可用入口: 当前 ``base_url`` 在前, 备用入口按声明顺序; 去重并去掉尾斜杠."""
        seen: set[str] = set()
        origins: list[str] = []
        for url in (self.base_url, *self._profile.mirrors):
            normalized = url.rstrip("/")
            if normalized and normalized not in seen:
                seen.add(normalized)
                origins.append(normalized)
        return tuple(origins)

    def _resolve_config(self):
        if self.config is None:
            return

        if self.config.base_url:
            self.base_url = self.config.base_url.rstrip("/")

        for k, v in self.config.cookie.items():
            self.cookies[k] = v

    @property
    def logger(self) -> structlog.stdlib.BoundLogger:
        try:
            return self._logger
        except AttributeError:
            self._logger = structlog.get_logger(f"amane.crawlers.{self.name}")
            return self._logger

    async def fetch(self, query: SearchQuery, options: FetchOptions | None = None) -> MediaMetadata | None:
        """按可用入口取一次; 连接层失败时换下一个入口, 并记住成功的那个.

        记住成功入口是必要的: 否则每个文件都要先撞一次主域超时 (javdb 主域被墙时是 37 s/文件).
        """
        # 未走 __init__ 的 duck-typed 爬虫 (测试替身) 没有入口信息: 退化为单入口, 行为与无镜像一致.
        origins = getattr(self, "_origins", None) or ()
        attempts = 0
        while True:
            if origins:
                index = getattr(self, "_origin_index", 0)
                if index >= len(origins):
                    index = 0
                self.base_url = origins[index]
            try:
                return await self._fetch_once(query, options)
            except RequestError as exc:
                attempts += 1
                if not origins or attempts >= len(origins) or not _switchable(exc):
                    raise
                self._origin_index = (index + 1) % len(origins)
                self.logger.warning(
                    "mirror unreachable, trying next",
                    number=query.number,
                    failed=origins[index],
                    next=origins[self._origin_index],
                    error=exc.message,
                )

    async def _fetch_once(self, query: SearchQuery, options: FetchOptions | None = None) -> MediaMetadata | None:
        number = query.number
        lang = options.language if options else None
        t0 = time.monotonic()

        url = await self._search(query, options)
        if not url:
            self.logger.warning("search miss", number=number, language=lang)
            return None
        self.logger.info("search hit", number=number, url=url, language=lang)

        result = await self._scrape(url, options)
        elapsed = round(time.monotonic() - t0, 2)
        if result is None:
            self.logger.warning("scrape failed", number=number, url=url, duration_s=elapsed)
        else:
            self.logger.info("scrape ok", number=number, title=result.title, duration_s=elapsed)
        return result

    async def check_connectivity(self) -> ConnectivityOutcome:
        """连通性自检: 缺省 GET ``base_url``, 用与刮削相同的视图 (渲染来源经浏览器, 其余直接请求).

        实际入口与 ``base_url`` 不同的来源覆盖本方法 (探测真实 API 主机, 或按前置条件报 ``skipped``).
        """
        return await self.client.check(self.base_url, cookies=self.cookies, headers=self.headers)

    @abstractmethod
    async def _search(self, query: SearchQuery, options: FetchOptions | None = None) -> str | None: ...

    @abstractmethod
    async def _scrape(self, url: str, options: FetchOptions | None = None) -> MediaMetadata | None: ...


if TYPE_CHECKING:
    # 结构性协议没有运行期检查: 在此静态断言实例满足聚合引擎与连通性编排声明的协议.
    _crawler_like: type[CrawlerLike] = Crawler
    _connectivity_probe: type[ConnectivityProbe] = Crawler
