from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ..crawlers.models import FetchOptions, MediaMetadata, SearchQuery
from ..enums import SiteName
from ..net.connectivity import ConnectivityOutcome, SkipReason, probe_get
from ..plugins.api import FilmSourceProvider, PluginContext
from ..plugins.manager import PluginManager
from ..plugins.models import PluginConfig, SourceDescriptor
from .actor import ActorFetcher, GFriendsActorCrawler, MinnanoActorCrawler, actor_registry
from .base import Crawler
from .registry import registry
from .sites import R18DevCrawler

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ..aggregate import CrawlerLike
    from ..config import SiteConfig
    from .actor import ActorCrawler
    from .connectivity import ConnectivityProbe
    from .http import HttpClient
    from .r18dev import R18Database

logger = structlog.get_logger()


async def _warm_actor(name: str, crawler: ActorCrawler) -> None:
    """执行来源预热; 失败只记录, 不冒泡 (预热是尽力而为的加速, 不影响来源可用性)."""
    try:
        await crawler.warmup()
    except Exception:
        logger.exception("actor warmup failed", name=name)


class _PluginProviderAdapter(FilmSourceProvider):
    """Validate plugin fetch results as MediaMetadata; errors bubble to invoke_source."""

    def __init__(
        self,
        provider: FilmSourceProvider,
        *,
        http_client: HttpClient,
        descriptor: SourceDescriptor | None = None,
    ) -> None:
        self._provider = provider
        self._http = http_client
        self._descriptor = descriptor

    async def fetch(self, query: SearchQuery, options: FetchOptions | None = None) -> MediaMetadata | None:
        result = await self._provider.fetch(query, options)
        if result is None or isinstance(result, MediaMetadata):
            return result
        return MediaMetadata.model_validate(result)

    async def check_connectivity(self) -> ConnectivityOutcome:
        """插件可自定探测入口; 未声明 (``None``) 时按 descriptor 的首个 URL 探测."""
        outcome = await self._provider.check_connectivity()
        if outcome is not None:
            return outcome
        urls = self._descriptor.urls if self._descriptor is not None else ()
        if not urls:
            return ConnectivityOutcome.skipped(SkipReason.NO_URL)
        return await probe_get(self._http.web_client, urls[0])


if TYPE_CHECKING:
    # 结构性协议没有运行期检查: 在此静态断言适配器满足工厂下游声明的协议.
    _adapter_crawler_like: type[CrawlerLike] = _PluginProviderAdapter
    _adapter_connectivity_probe: type[ConnectivityProbe] = _PluginProviderAdapter


class CrawlerFactory:
    """延迟创建并缓存爬虫. 全部共用一个 HttpClient.

    SiteConfig 在构造期注入; 热重载时工厂与缓存实例一并重建.
    R18Database 为会话级, 不随热重载重建.
    """

    def __init__(
        self,
        http_client: HttpClient,
        site_configs: Mapping[str | SiteName, SiteConfig] | None = None,
        r18_db: R18Database | None = None,
        *,
        data_dir: Path | None = None,
        gfriends_repo: str | None = None,
        plugin_manager: PluginManager | None = None,
        plugin_configs: dict[str, PluginConfig] | None = None,
    ):
        self._http = http_client
        self._site_configs: dict[str, SiteConfig] = {str(k): v for k, v in (site_configs or {}).items()}
        self._r18_db = r18_db
        self._data_dir = data_dir
        self._gfriends_repo = gfriends_repo
        self._plugin_manager = plugin_manager
        self._plugin_configs = plugin_configs or {}
        self._instances: dict[str, Crawler | FilmSourceProvider] = {}
        self._actor_instances: dict[str, ActorCrawler] = {}
        # 启动期预热任务引用: 避免 fire-and-forget 任务被 GC, 也便于关闭时回收.
        self._actor_warm_tasks: dict[str, asyncio.Task[None]] = {}

    async def get(self, name: str) -> Crawler | FilmSourceProvider | None:
        """内置来源与插件来源共用一份实例缓存; 分派只在此处, 下游只依赖结构性协议."""
        cached = self._instances.get(name)
        if cached is not None:
            return cached

        cls = registry.get(name)
        if cls is not None:
            site_config = self._site_configs.get(name)
            client = self._http.for_source(name, site_config)
            # R18DevCrawler 额外注入只读 DB.
            if cls is R18DevCrawler:
                instance: Crawler = R18DevCrawler(client=client, config=site_config, db=self._r18_db)
            else:
                instance = cls(client=client, config=site_config)
            self._instances[name] = instance
            return instance

        manager = self._plugin_manager
        if manager is None or not manager.has_film_plugin(name):
            logger.error("crawler not registered", name=name)
            return None
        return await self._get_plugin(manager, name)

    async def _get_plugin(self, manager: PluginManager, name: str) -> FilmSourceProvider | None:
        # 插件禁用返回 None; data_dir 缺失抛 RuntimeError.
        config = self._plugin_configs.get(name, PluginConfig())
        if not config.enabled:
            logger.info("source plugin disabled", source=name)
            return None
        if self._data_dir is None:
            raise RuntimeError("data_dir is required for source plugins")
        plugin_dir = self._data_dir / "plugins" / name
        plugin_dir.mkdir(parents=True, exist_ok=True)
        provider = manager.build_plugin_provider(
            name,
            context=PluginContext(
                source_id=name,
                http_client=self._http,
                web_client=self._http.web_client,
                data_dir=plugin_dir,
            ),
            config=config,
        )
        adapter = _PluginProviderAdapter(
            provider,
            http_client=self._http,
            descriptor=manager.descriptor(name),
        )
        self._instances[name] = adapter
        return adapter

    async def get_crawlers(self, names: Iterable[str]) -> dict[str, CrawlerLike]:
        result: dict[str, CrawlerLike] = {}
        for name in names:
            try:
                crawler = await self.get(name)
            except Exception:
                logger.exception("crawler construction failed", name=name)
                crawler = None
            if crawler is not None:
                result[name] = crawler
        return result

    async def get_actor(self, name: str) -> ActorCrawler | None:
        if name in self._actor_instances:
            return self._actor_instances[name]

        cls = actor_registry.get(name)
        if cls is None:
            logger.error("actor crawler not registered", name=name)
            return None

        site_config = self._site_configs.get(name)
        client = self._http.for_source(name, site_config)
        if cls is GFriendsActorCrawler:
            instance: ActorCrawler = GFriendsActorCrawler(
                client=client,
                config=site_config,
                data_dir=self._data_dir,
                repo_url=self._gfriends_repo,
            )
        elif cls is MinnanoActorCrawler:
            # 女优一览索引落盘, 避免每次进程重建都重抓全站.
            instance = MinnanoActorCrawler(client=client, config=site_config, data_dir=self._data_dir)
        else:
            instance = cls(client=client, config=site_config)
        self._actor_instances[name] = instance
        # 预热不在这里触发: get_actor 被连通性探测复用, 探测必须无副作用
        # (否则点一次「检测连通性」就会在后台掀起一次全站抓取). 见 get_actor_crawlers.
        return instance

    async def get_actor_crawlers(self, names: Iterable[str]) -> dict[str, ActorFetcher]:
        result: dict[str, ActorFetcher] = {}
        for name in names:
            crawler = await self.get_actor(name)
            if crawler is not None:
                result[name] = crawler
                self._warm(crawler)
        return result

    def _warm(self, crawler: ActorCrawler) -> None:
        """刮削前预热: 索引类来源 (如 minnano) 后台建/刷新索引, 不阻塞本次刮削.

        已有索引时立即兜底, 重建在后台进行, 因此这里只改变「何时开始准备」, 不改变响应时延.
        任务引用必须留存, 否则可能被 GC 提前回收; 异常在 _warm_actor 内消化.
        """
        name = str(crawler.name)
        running = self._actor_warm_tasks.get(name)
        if running is not None and not running.done():
            return
        self._actor_warm_tasks[name] = asyncio.create_task(_warm_actor(name, crawler), name=f"actor-warmup-{name}")
