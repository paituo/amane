"""批次级「来源可用性」缓存: 把连通性探测结果按 TTL 进程级缓存, 供 REFRESH / RESCRAPE / Feed 扇出时一次性判定哪些来源不可用.

与 :class:`~amane.net.connectivity_breaker.ConnectivityBreaker` 的关系:

- 熔断器是**请求级**短路 (某个代理 / host 连续失败即开路, 后续请求毫秒级短路). 它是「运行时被动学习」.
- 本服务是**扇出级**预探测 (主动探一遍当前配置真正会请求的全部来源), 把结论缓存放进每个派生 SCRAPE 的
  ``ScrapePayload.disabled_sources``, 让死站在建图阶段就被跳过, 连「第一个失败请求」都不发.

两者互补: 即便本服务的缓存尚未建立 (进程刚启动 / 上轮探测已过期), 熔断器也会在首批请求里把代理 / host 开路,
因此不会回到「每个任务各付一遍重试」的旧状. 缓存 TTL 选得比熔断器冷却短, 避免来源恢复后过久仍被跳过.

**探测刻意不阻塞扇出路径**: 一轮探测要并发访问全部来源, 健康网络下约 1~3s, 慢网 / 无网时可达数十秒.
让 REFRESH 任务等它, 等于把健康检查的成本转嫁给用户可见任务 (空目录刷新也会突然变慢). 因此
:meth:`SourceAvailability.disabled_sources` 只做非阻塞读 —— 缓存过期时返回**旧结论** (死站大概率仍死),
同时在后台刷新; 从未探测过时才返回空集, 由请求级熔断兜底首批请求.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Protocol

import structlog

from .crawlers.connectivity import ConnectivityChecker
from .net.connectivity import ConnectivityStatus

if TYPE_CHECKING:
    from .config import HotSettings
    from .crawlers.factory import CrawlerFactory
    from .plugins.manager import PluginManager

logger = structlog.get_logger()

# 缓存 TTL: 比熔断器 proxy 冷却 (120s) 略长, 但远短于 host 冷却 (300s), 平衡「少探测」与「恢复后不过久跳过」.
_DEFAULT_TTL_S = 180.0

# 探测异常 (取实例失败 / 检查器抛错) 后的重试间隔: 避免每次扇出都重启一轮注定失败的探测.
_RETRY_BACKOFF_S = 30.0


class DisabledSourcesProbe(Protocol):
    """扇出路径只需要这一点能力: 读当前被判不可用的 source_id 集合.

    用结构化类型而不是绑定 :class:`SourceAvailability`, 是因为构造后者需要爬虫工厂与热配置;
    REFRESH / RESCRAPE 的派生逻辑只关心「哪些来源该跳过」, 测试注入极简替身即可.
    """

    async def disabled_sources(self) -> frozenset[str]: ...


class SourceAvailability:
    """进程级单例 (挂在 NetworkStack 上), 内存态, 不落库.

    探测范围与 ``ConnectivityChecker`` 一致 = 当前热配置真正会请求的全部来源 (各类型路由并集 + 演员来源),
    因此结论不会把没在用的站点混进来. 探测结果中仅 ``FAILED`` 的来源被判不可用; ``SKIPPED`` (来源声明不探测)
    与 ``OK`` 都不计入 —— 声明不探测的来源仍可能经常规 fetch 取到数据.
    """

    def __init__(
        self,
        factory: CrawlerFactory,
        hot: HotSettings,
        plugin_manager: PluginManager | None = None,
        ttl_seconds: float = _DEFAULT_TTL_S,
    ) -> None:
        self._checker = ConnectivityChecker(factory, hot, plugin_manager)
        self._ttl = ttl_seconds
        self._cache: frozenset[str] | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None
        self._retry_at = 0.0

    async def disabled_sources(self) -> frozenset[str]:
        """非阻塞读当前被判不可用的 source_id 集合.

        缓存新鲜 → 直接返回. 过期 → 在后台刷新, 立即返回旧结论 (从未探测过则空集).
        """
        now = time.monotonic()
        if self._cache is not None and now < self._expires_at:
            return self._cache
        self._spawn_background_refresh(now)
        return self._cache if self._cache is not None else frozenset()

    async def wait_idle(self) -> frozenset[str] | None:
        """等待在飞的探测结束并返回其结论; 没有在飞任务返回 ``None``. 测试与优雅关闭用."""
        task = self._refresh_task
        if task is None or task.done():
            return None
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 后台包装器已记录并安排退避; 这里只把结论交回去 (可能是 None = 探测未产出).
            return None
        return self._cache

    def invalidate(self) -> None:
        """配置变动 / 代理切换后清空缓存, 迫使下次扇出重新探测."""
        self._cache = None
        self._expires_at = 0.0

    def _spawn_background_refresh(self, now: float) -> None:
        """确保只有一个后台探测在飞; 异常退避期内不重启."""
        if now < self._retry_at:
            return
        task = self._refresh_task
        if task is not None and not task.done():
            return
        try:
            self._refresh_task = asyncio.create_task(self._background_refresh())
        except RuntimeError:
            # 事件循环正在关闭 (关停最后一刻): 没有循环可挂任务, 本次扇出退回熔断兜底.
            logger.debug("availability refresh skipped: no running loop")
            return
        self._refresh_task.add_done_callback(self._on_refresh_done)

    async def _background_refresh(self) -> None:
        """后台探测包装: 一切异常都在这里终结, 绝不变成未取回的 task 异常."""
        try:
            async with self._lock:
                await self._probe_and_cache()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("availability background probe failed")
            self._retry_at = time.monotonic() + _RETRY_BACKOFF_S

    async def _probe_and_cache(self) -> frozenset[str]:
        checks = await self._safe_check()
        disabled = frozenset(c.source_id for c in checks if c.outcome.status is ConnectivityStatus.FAILED)
        self._cache = disabled
        self._expires_at = time.monotonic() + self._ttl
        self._retry_at = 0.0
        return disabled

    def _on_refresh_done(self, task: asyncio.Task[None]) -> None:
        if self._refresh_task is task:
            self._refresh_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            # 双保险: _background_refresh 本应已捕获, 到这里说明包装本身出了问题.
            logger.exception("availability background probe crashed", error=repr(exc))

    async def _safe_check(self) -> list:
        """探测接缝: 默认委托 ConnectivityChecker; 单元测试可替换以注入假条目."""
        return await self._checker.check()
