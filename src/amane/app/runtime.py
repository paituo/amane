"""经 ``app.state.runtime`` / ``RuntimeDep`` 注入. ``apply_rebuild()`` 在 HotSettings 变更时重建依赖热配置的对象."""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx2 as httpx
import structlog

from ..config import EmbeddingProviderName, R18Config
from ..crawlers import actor_registry, registry
from ..crawlers.base import CrawlerProfile
from ..crawlers.factory import CrawlerFactory
from ..crawlers.http import HttpClient
from ..crawlers.r18dev import R18Database
from ..db.models import TaskType
from ..enums import BrowserBackendName, BrowserMode, SiteName
from ..handlers import (
    ActorScrapeHandler,
    CleanupHandler,
    DeleteHandler,
    LibraryTaskLocks,
    OrganizeHandler,
    R18ImportHandler,
    RefreshHandler,
    RescrapeHandler,
    ScanInvalidHandler,
    ScrapeHandler,
    UpscaleHandler,
)
from ..library import InventoryStore
from ..llm import TranslationCache, build_translator
from ..llm.guest_token import DEVICE_ID_FILENAME
from ..media.watermarks import user_watermark_dir
from ..net.browser import BrowserPool
from ..net.connectivity_breaker import ConnectivityBreaker
from ..net.http import RateLimiters, WebClient, site_proxy_overrides
from ..net.proxy_pool import ProxyPool
from ..net.response_cache import ResponseCache
from ..playback import PlaybackFactory, PlaybackState
from ..plugins.manager import PluginManager
from ..plugins.packaging import install_plugin_path, install_plugin_zip, uninstall_plugin_tree
from ..scheduler.worker import MAIN_LOOP_STOP_TIMEOUT, AsyncWorker
from ..scrape_availability import SourceAvailability
from ..search import (
    EmbeddingProvider,
    FastEmbedProvider,
    MetadataIndexer,
    OpenAICompatibleProvider,
    SearchService,
    SearchStore,
    WarmableProvider,
    fastembed_available,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..agent import AgentService
    from ..config import BrowserConfig, ConfigManager, EmbeddingConfig, HotSettings, SearchConfig
    from ..db.repository import Repository
    from ..events import EventBus
    from ..handlers.protocol import TaskHandler
    from ..media import ResourceStore
    from ..release import ReleaseChecker
    from ..scheduler.feeds import FeedService
    from ..scheduler.service import WatcherService
    from .proxy_failure_cache import ProxyFailureCache

logger = structlog.get_logger()


@dataclass
class NetworkStack:
    """limiters → web_client → http_client → factory 的一次构造结果."""

    web_client: WebClient
    http_client: HttpClient
    factory: CrawlerFactory
    browser: BrowserPool
    availability: SourceAvailability
    """批次级来源可用性缓存 (TTL), 供 REFRESH / RESCRAPE / Feed 扇出时一次性判定死站."""


def build_network_stack(
    hot: HotSettings,
    r18_db: R18Database | None = None,
    *,
    data_dir: Path | None = None,
    plugin_manager: PluginManager | None = None,
    browser: BrowserPool | None = None,
) -> NetworkStack:
    """bootstrap 与热重载共用. r18_db 为会话级只读引擎, 热重载时复用同一实例, 不随配置重建.

    ``browser`` 由热重载传入未变化的浏览器池以保留已解决的会话; 复用时就地同步新的 HTTP 通道
    (solver 经它出站). 不传则按当前配置构造.
    """
    site_urls: dict[str, list[str]] = {}
    referer_hosts: set[str] = set()
    site_config = hot.scraping.site_config

    def _register_site(site: str, profile: CrawlerProfile) -> None:
        urls = [*profile.urls, profile.base_url]
        site_urls[site] = urls
        if not profile.same_origin_referer:
            return
        # 用户配置的镜像域同样纳入, 否则图片仍按裸请求发出.
        configured = site_config.get(site)
        for raw in (configured.base_url if configured else None, *urls):
            host = httpx.URL(raw).host if raw else None
            if host is not None:
                referer_hosts.add(host)

    for site in registry.sites():
        crawler_cls = registry.get(site)
        if crawler_cls:
            _register_site(site, crawler_cls.profile())
    for name in actor_registry.sites():
        crawler_cls = actor_registry.get(name)
        if crawler_cls:
            _register_site(str(SiteName(name)), crawler_cls.profile())

    plugin_rates: dict[str, float | None] = {}
    if plugin_manager is not None:
        for descriptor in plugin_manager.descriptors():
            if descriptor.id not in site_urls:
                site_urls[descriptor.id] = list(descriptor.urls)
                plugin_rates[descriptor.id] = descriptor.rate_limit

    limiters = RateLimiters.from_config(
        hot.network.rate_limits,
        hot.scraping.site_config,
        site_urls,
        source_rates=plugin_rates,
        default_rate=hot.network.default_rate_limit,
        adaptive=hot.network.adaptive_rate_limit,
        floor_ratio=hot.network.adaptive_rate_floor_ratio,
    )
    # P2-⑦ 多代理池: 仅在 network.proxies 非空时启用; 池与熔断器共享 proxy:<url> 键,
    # 开路代理不被选中, 冷却后半开探针自动回池. 未配置池时行为与改造前完全一致.
    # 阈值经 network.* 下发 (默认值与历史实现一致), 便于依据代理质量与站点响应情况调整.
    breaker = ConnectivityBreaker(
        proxy_fail_max=hot.network.proxy_circuit_fail_max,
        proxy_cooldown_s=hot.network.proxy_circuit_cooldown_s,
        host_fail_max=hot.network.host_circuit_fail_max,
        host_cooldown_s=hot.network.host_circuit_cooldown_s,
    )
    proxy_pool = ProxyPool(hot.network.proxies, breaker) if hot.network.proxies else None
    if proxy_pool is not None:
        logger.info("proxy_pool.enabled", proxies=list(hot.network.proxies))
    web_client = WebClient(
        proxy=hot.network.proxy,
        proxy_overrides=site_proxy_overrides(hot.scraping.site_config, site_urls),
        timeout=hot.network.timeout,
        max_retries=hot.network.max_retries,
        max_clients=hot.network.max_clients,
        limiters=limiters,
        same_origin_referer_hosts=frozenset(referer_hosts),
        chunked_threshold=hot.network.chunked_threshold,
        chunk_size=hot.network.chunk_size,
        download_concurrency=hot.network.concurrency,
        connectivity=breaker,
        response_cache=ResponseCache(hot.network.response_cache_ttl_s, hot.network.response_cache_max_entries),
        proxy_pool=proxy_pool,
    )
    if browser is None:
        browser = BrowserPool(
            default_backend=hot.network.browser.backend,
            solver_url=hot.network.browser.solver_url,
            proxy=hot.network.proxy,
            web_client=web_client,
            timeout_ms=hot.network.browser.timeout,
        )
    else:
        browser.rebind_web_client(web_client)
    _warn_disabled_browser_sources(hot, browser)
    http_client = HttpClient(web=web_client, browser=browser, browser_timeout=hot.network.browser.timeout)
    factory = CrawlerFactory(
        http_client,
        site_configs=hot.scraping.site_config,
        r18_db=r18_db,
        data_dir=data_dir,
        gfriends_repo=hot.actor_scraping.gfriends_repo,
        plugin_manager=plugin_manager,
        plugin_configs=hot.plugins,
    )

    # 批次级来源可用性缓存: 用当前网络栈探测, 结论与运行时配置一致.
    availability = SourceAvailability(factory, hot, plugin_manager)

    return NetworkStack(
        web_client=web_client,
        http_client=http_client,
        factory=factory,
        browser=browser,
        availability=availability,
    )


def _warn_disabled_browser_sources(hot: HotSettings, browser: BrowserPool) -> None:
    """一律使用浏览器却没有可用后端的来源: 获取时必然失败, 在构造期给出一次明确告警.

    ``auto`` 在无后端时退化为直连, ``browser_backend=off`` 是显式禁用, 均不在此告警.
    """
    for site, config in hot.scraping.site_config.items():
        if config.use_browser is not BrowserMode.ALWAYS or config.browser_backend is BrowserBackendName.OFF:
            continue
        if browser.resolve(config.browser_backend) is None:
            logger.warning("browser rendering enabled without backend", site=str(site))


def build_r18_db(r18: R18Config) -> R18Database | None:
    """从 r18 配置构建只读引擎. dsn 未配置或建连失败时返回 None (数据源静默禁用).

    引擎构造是同步的 (create_async_engine 仅初始化连接池, 不立即连接), 故可在 _rebuild() 内调用.
    """
    if not r18.enabled:
        return None
    try:
        return R18Database(r18.read_url())
    except Exception:
        structlog.get_logger().warning("r18 read engine not created", exc_info=True)
        return None


@dataclass(eq=False)
class R18Handle:
    """r18 引擎的所有权标记. 引擎对象本身仍以 ``R18Database`` 注入 factory / crawler."""

    engine: R18Database
    closed: bool = False


def build_embedding_provider(hot: HotSettings, cache_dir: Path | None = None) -> EmbeddingProvider | None:
    """按配置构造嵌入 provider; 向量路关闭或实现缺席时返回 None.

    fastembed 是可选 extra: 该分发不含它时**不构造**而不是抛错 —— 搜索栈据此把向量路标成
    ``degraded``, 关键词路照常可用; 让启动失败于一个可以降级的能力是不划算的.
    """
    if not hot.search.vector_enabled:
        return None
    embedding = hot.search.embedding
    if embedding.provider is EmbeddingProviderName.OPENAI_COMPATIBLE:
        return OpenAICompatibleProvider(
            base_url=embedding.base_url,
            model=embedding.model,
            dim=embedding.dim,
            api_key=embedding.api_key,
            batch_size=embedding.batch_size,
            timeout_s=embedding.timeout_s,
        )
    if not fastembed_available():
        logger.warning(
            "内嵌嵌入不可用, 向量路降级",
            reason="当前分发未包含 fastembed",
            hint="安装 amane[vector] 或把 search.embedding.provider 改为 openai_compatible",
        )
        return None
    return FastEmbedProvider(
        model=embedding.model,
        dim=embedding.dim,
        cache_dir=cache_dir,
        threads=embedding.threads,
    )


@dataclass(frozen=True, slots=True)
class ReusedEmbedding:
    """从旧栈接管来的 provider 及其预热任务.

    重载模型是唯一昂贵的部分 (热 1.8s, 冷要下权重). 改融合参数、改词索引开关都不该付这笔钱,
    所以 ``embedding`` 子配置没变时把 provider 整体移交, 只换掉依赖配置的 service / indexer.
    """

    provider: EmbeddingProvider
    warm_task: asyncio.Task[None] | None = None


@dataclass(eq=False)
class SearchStack:
    """检索的会话级装配. 开关全关时组合根不构造它, 写入与查询都退回旧路径."""

    store: SearchStore
    provider: EmbeddingProvider | None
    service: SearchService
    indexer: MetadataIndexer
    embedding_key: EmbeddingConfig
    """构造本栈时用的嵌入配置. 判断 provider 能否跨重建复用, 只看这一段是否相等."""
    _warm_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def start_warmup(self) -> None:
        """在"配置启用"这一刻开始预热模型, 而不是等首个搜索请求.

        冷启动要下约 150MB 权重 (实测 53s), 挂在请求上就是一个必然超时的查询. 预热是后台任务:
        不阻塞配置保存, 未就绪期间的查询由 ``SearchService`` 有界降级.
        """
        provider = self.provider
        if provider is None or not isinstance(provider, WarmableProvider):
            return
        if self._warm_task is not None and not self._warm_task.done():
            # 从旧栈接管的任务还在跑: 再起一个只会并发加载同一份权重.
            return
        self._warm_task = asyncio.create_task(provider.warmup(), name="search-warmup")

    def detach_provider(self) -> ReusedEmbedding | None:
        """把 provider 与预热任务交出去, 之后 ``aclose`` 不再碰它们. 没有可交的就返回 ``None``."""
        provider = self.provider
        if provider is None:
            return None
        task, self._warm_task = self._warm_task, None
        self.provider = None
        return ReusedEmbedding(provider=provider, warm_task=task)

    async def aclose(self) -> None:
        task, self._warm_task = self._warm_task, None
        if task is not None:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("检索预热任务异常结束", exc_info=True)
        await self.indexer.aclose()
        await self.store.aclose()
        if self.provider is not None:
            await self.provider.aclose()


def build_search(
    hot: HotSettings,
    repo: Repository,
    db_path: Path,
    data_dir: Path,
    *,
    reuse: ReusedEmbedding | None = None,
) -> SearchStack | None:
    """构造检索栈并注册写入 sink.

    同步构造: ``SearchStore`` 只记路径, 建连在 ``open()`` 里; provider 亦然, 模型在首次使用时加载.
    向量表是派生缓存, 与业务库分文件, 原因见 ``docs/dev/search.md``.
    内嵌模型权重落在 ``data_dir/models``, 与 resources / translations.db 同处一地, 便于整体迁移与打包.
    ``reuse`` 来自旧栈: 嵌入配置未变时直接接管, 不重新加载模型.
    """
    if not hot.search.keyword_enabled and not hot.search.vector_enabled:
        return None
    provider = reuse.provider if reuse is not None else build_embedding_provider(hot, data_dir / "models")
    store = SearchStore(
        db_path,
        dim=hot.search.embedding.dim,
        model=provider.model_id if provider is not None else f"none:{hot.search.embedding.model}",
    )
    service = SearchService(store=store, provider=provider, config=hot.search)
    indexer = MetadataIndexer(store=store, repo=repo, config=hot.search, provider=provider)
    stack = SearchStack(
        store=store,
        provider=provider,
        service=service,
        indexer=indexer,
        embedding_key=hot.search.embedding.model_copy(deep=True),
    )
    stack._warm_task = reuse.warm_task if reuse is not None else None
    return stack


@dataclass(eq=False)
class RetiringWorker:
    """已退役、正在排空的 worker 及其使用的 r18 句柄与浏览器池."""

    worker: AsyncWorker
    r18: R18Handle | None
    browser: BrowserPool | None


@dataclass
class AppRuntime:
    repo: Repository
    config: ConfigManager
    worker: AsyncWorker
    web_client: WebClient
    http_client: HttpClient
    factory: CrawlerFactory
    event_bus: EventBus
    release_checker: ReleaseChecker
    resource_store: ResourceStore
    proxy_failure_cache: ProxyFailureCache
    watcher_service: WatcherService | None = None
    feed_service: FeedService | None = None
    safe_dirs: list[Path] | None = field(default_factory=list)
    api_token: str | None = None
    translation_cache: TranslationCache | None = None
    r18_db: R18Database | None = None
    agent_service: AgentService | None = None
    plugin_manager: PluginManager | None = None
    playback_factory: PlaybackFactory | None = None
    playback_state: PlaybackState = field(default_factory=PlaybackState)
    library_locks: LibraryTaskLocks = field(default_factory=LibraryTaskLocks)
    inventory_store: InventoryStore = field(default_factory=InventoryStore)
    browser: BrowserPool | None = None
    r18_handle: R18Handle | None = None
    search: SearchStack | None = None

    _r18_config: R18Config | None = field(default=None, repr=False)
    _browser_key: tuple[BrowserConfig, str | None] | None = field(default=None, repr=False)
    _search_key: SearchConfig | None = field(default=None, repr=False)
    _rebuild_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _retiring: list[RetiringWorker] = field(default_factory=list, repr=False)
    _retire_tasks: set[asyncio.Task[None]] = field(default_factory=set, repr=False)
    _closing: bool = field(default=False, repr=False)
    _stopped: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self._r18_config is None:
            self._r18_config = self.config.hot.r18.model_copy(deep=True)
        if self.r18_handle is None and self.r18_db is not None:
            self.r18_handle = R18Handle(self.r18_db)
        self._browser_key = self._current_browser_key()
        if self._search_key is None:
            self._search_key = self.config.hot.search.model_copy(deep=True)

    def _current_browser_key(self) -> tuple[BrowserConfig, str | None]:
        """浏览器池的生命周期键: 任一变化都需要换新引擎 (代理与超时参与启动与单次渲染)."""
        network = self.config.hot.network
        return (network.browser.model_copy(deep=True), network.proxy)

    def _rebuild(self) -> None:
        """重建依赖热配置的对象. 调用方必须持有 ``_rebuild_lock`` (见 ``apply_rebuild``).

        r18 只读引擎随 hot.r18 变更而重建; 引擎与浏览器池的旧实例由退役流程在引用它们的 worker 排空后释放.
        rebuild 是同步的, 不能 await.
        """
        hot = self.config.hot
        paused = self.worker.is_paused

        # 日志级别即时生效, 无需重建
        logging.getLogger("amane").setLevel(hot.logging.level)

        # r18 配置变更时重建只读引擎与新句柄; 旧句柄随退役 worker 释放
        if hot.r18 != self._r18_config:
            self.r18_db = build_r18_db(hot.r18)
            self.r18_handle = R18Handle(self.r18_db) if self.r18_db is not None else None
            self._r18_config = hot.r18.model_copy(deep=True)

        # 浏览器池只在其生命周期键变化时重建; 复用同一实例保留已解决的挑战会话
        browser_key = self._current_browser_key()
        reused_browser = None if browser_key != self._browser_key else self.browser
        self._browser_key = browser_key

        stack = build_network_stack(
            hot,
            r18_db=self.r18_db,
            data_dir=self.config.cold.data_dir,
            plugin_manager=self.plugin_manager,
            browser=reused_browser,
        )
        self.web_client = stack.web_client
        self.http_client = stack.http_client
        self.factory = stack.factory
        self.browser = stack.browser
        if self.feed_service is not None:
            self.feed_service.set_web_client(self.web_client)
            self.feed_service.set_availability(stack.availability)

        # 使用新处理器与并发数重建 worker
        self.worker = AsyncWorker(
            repo=self.repo,
            handlers=build_handlers(
                self.repo,
                self.factory,
                self.web_client,
                self.resource_store,
                hot,
                self.safe_dirs,
                self.translation_cache,
                self.config.cold.data_dir,
                self.plugin_manager,
                library_locks=self.library_locks,
                inventory_store=self.inventory_store,
                availability=stack.availability,
            ),
            concurrency=hot.worker.concurrency,
            poll_interval=hot.worker.poll_interval,
            shutdown_timeout=hot.worker.shutdown_timeout,
            event_bus=self.event_bus,
            log_dir=self.config.cold.log_dir,
            get_hot=lambda: self.config.hot,
        )
        self.worker.set_paused(paused)

        if self.agent_service is not None:
            self.agent_service.rebuild(hot.agent)

        previous_playback = self.playback_factory
        # 新的 Factory 构造时丢弃解析结果缓存 (配置改动可能更换凭据); token 表与探测缓存仍在
        # ``playback_state`` 里, 只有插件集合变化才 reset().
        current_playback = PlaybackFactory(
            plugin_manager=self.plugin_manager,
            plugin_configs=hot.plugins,
            http_client=self.http_client,
            web_client=self.web_client,
            data_dir=self.config.cold.data_dir,
            proxy=hot.network.proxy,
            state=self.playback_state,
        )
        if previous_playback is not None and set(previous_playback.playback_source_ids()) != set(
            current_playback.playback_source_ids()
        ):
            # 插件集合变化 (安装 / 卸载 / 重载 / 启停): 已签发的 token 与探测缓存必须失效.
            self.playback_state.reset()
        self.playback_factory = current_playback

    async def apply_rebuild(self) -> None:
        """串行化 rebuild 与 worker 替换; 旧资源由退役流程在 worker 排空后释放."""
        async with self._rebuild_lock:
            self._ensure_open()
            await self._apply_rebuild_unlocked()

    async def reload_plugins(self) -> PluginManager:
        """Rediscover drop-ins under ``plugins/sources`` and rebuild the scrape stack."""
        async with self._rebuild_lock:
            self._ensure_open()
            self._replace_plugin_manager(PluginManager.discover(self.config.cold.data_dir))
            await self._apply_rebuild_unlocked()
            return self._require_plugin_manager()

    async def install_plugin_archive(self, payload: bytes) -> PluginManager:
        """Install a zip of ``plugin.py`` into ``plugins/sources`` and rebuild."""
        async with self._rebuild_lock:
            self._ensure_open()
            plugin_id = install_plugin_zip(self.config.cold.data_dir, payload)
            self._replace_plugin_manager(PluginManager.discover(self.config.cold.data_dir))
            await self._apply_rebuild_unlocked()
            logger.info("source plugin installed", plugin_id=plugin_id)
            return self._require_plugin_manager()

    async def install_plugin_from_path(self, source: Path) -> PluginManager:
        """Copy a server path (directory or zip) into ``plugins/sources`` and rebuild."""
        async with self._rebuild_lock:
            self._ensure_open()
            plugin_id = install_plugin_path(self.config.cold.data_dir, source)
            self._replace_plugin_manager(PluginManager.discover(self.config.cold.data_dir))
            await self._apply_rebuild_unlocked()
            logger.info("source plugin installed", plugin_id=plugin_id)
            return self._require_plugin_manager()

    async def uninstall_plugin_tree(self, plugin_id: str) -> None:
        """Remove ``plugins/sources/<plugin_id>`` and rediscover sources."""
        async with self._rebuild_lock:
            self._ensure_open()
            manager = self._require_plugin_manager()
            if manager.get(plugin_id) is None:
                raise KeyError(plugin_id)
            uninstall_plugin_tree(self.config.cold.data_dir, plugin_id)
            self._replace_plugin_manager(PluginManager.discover(self.config.cold.data_dir))
            await self._apply_rebuild_unlocked()

    async def _apply_rebuild_unlocked(self) -> None:
        """必须持有 ``_rebuild_lock``."""
        previous_search = self.search
        await self._replace_search_if_needed()
        old_playback = self.playback_factory
        old_worker = self.worker
        old_r18 = self.r18_handle
        old_browser = self.browser
        self._rebuild()
        old_worker.retire()
        self.worker.start()
        self._retire_worker(old_worker, old_r18, old_browser)
        if self._retiring:
            logger.warning("workers retiring", count=len(self._retiring))
        if old_playback is not None:
            await old_playback.aclose()
        if previous_search is not None and previous_search is not self.search:
            # 新栈的 sink 已就位才关旧栈: 中间不留没人在听的窗口.
            await previous_search.aclose()

    async def _replace_search_if_needed(self) -> None:
        """``hot.search`` 变了才重建检索栈. 旧栈由调用方在 close 段关闭.

        不放进同步的 ``_rebuild``: 建连与加载都要 await, 而 ``_rebuild`` 不允许 await.

        嵌入配置没变时把 provider (连同一个还在跑的预热任务) 移交给新栈: 否则改一个
        ``fusion.zgate`` 也要重新加载模型, 代价远大于重建本身.
        """
        desired = self.config.hot.search
        if desired == self._search_key:
            return
        reuse = self._take_reusable_embedding(desired)
        stack = build_search(
            self.config.hot,
            self.repo,
            self.config.cold.db_path,
            self.config.cold.data_dir,
            reuse=reuse,
        )
        if stack is not None:
            await stack.store.open()
            # 开关刚被打开就是"配置启用时": 模型下载从这里开始, 不等首个查询.
            stack.start_warmup()
            stack.indexer.start()
            stack.indexer.start_backfill()
        self.search = stack
        self.repo.set_index_sink(stack.indexer if stack is not None else None)
        self._search_key = desired.model_copy(deep=True)
        logger.info(
            "search stack rebuilt",
            keyword=desired.keyword_enabled,
            vector=desired.vector_enabled,
            engine=str(desired.vector_engine),
            reused_embedding=reuse is not None,
        )

    def _take_reusable_embedding(self, desired: SearchConfig) -> ReusedEmbedding | None:
        """能接管就接管. 判据只有一条: 嵌入子配置逐字段相等 (含 model/dim/provider/threads).

        向量路被关掉时不接管: 新栈的 provider 本来就该是 ``None``, 留着模型占内存没有意义.
        """
        stack = self.search
        if stack is None or not desired.vector_enabled:
            return None
        if stack.embedding_key != desired.embedding:
            return None
        return stack.detach_provider()

    def _retire_worker(self, worker: AsyncWorker, r18: R18Handle | None, browser: BrowserPool | None) -> None:
        entry = RetiringWorker(worker=worker, r18=r18, browser=browser)
        self._retiring.append(entry)
        task = asyncio.create_task(self._drain_and_close(entry))
        self._retire_tasks.add(task)

    async def _drain_and_close(self, entry: RetiringWorker) -> None:
        """等退役 worker 排空后释放其 r18 句柄与浏览器池; 不获取 ``_rebuild_lock``."""
        try:
            await entry.worker.drain()
            self._retiring.remove(entry)
            if entry.r18 is not None:
                await self._release_r18(entry.r18)
            if entry.browser is not None:
                await self._release_browser(entry.browser)
        except Exception:
            logger.exception("retiring worker cleanup failed")
        finally:
            self._retire_tasks.discard(asyncio.current_task())
        logger.info("worker retired", active_count=entry.worker.active_count)

    async def _release_r18(self, handle: R18Handle) -> None:
        """句柄不再被当前或任何退役 worker 使用时关闭; 判定与置位在同一同步段."""
        if handle.closed or handle is self.r18_handle:
            return
        if any(entry.r18 is handle for entry in self._retiring):
            return
        handle.closed = True
        await handle.engine.close()

    async def _release_browser(self, browser: BrowserPool) -> None:
        """池不再被当前或任何退役 worker 使用时关闭."""
        if browser is self.browser:
            return
        if any(entry.browser is browser for entry in self._retiring):
            return
        await browser.close()

    async def cancel_task(self, task_id: int) -> bool:
        """当前 worker 与退役 worker 都能命中."""
        if await self.worker.cancel_task(task_id):
            return True
        for entry in list(self._retiring):
            if await entry.worker.cancel_task(task_id):
                return True
        return False

    async def stop_workers(self, *, closing: bool = True) -> None:
        """关闭编排: 停全部 worker 的认领, 限时处置活跃任务, 单次清扫. 幂等."""
        async with self._rebuild_lock:
            if self._stopped:
                return
            if closing:
                self._closing = True
            workers = [self.worker, *(entry.worker for entry in self._retiring)]
            self._retire_worker(self.worker, self.r18_handle, self.browser)
            for worker in workers:
                worker.retire()
            for worker in workers:
                try:
                    await asyncio.wait_for(worker.wait_stopped(), timeout=MAIN_LOOP_STOP_TIMEOUT)
                except TimeoutError:
                    logger.warning("worker main loop stuck, cancelling", timeout=MAIN_LOOP_STOP_TIMEOUT)
                    worker.cancel_main_loop()
                    await worker.wait_stopped()
            for worker in workers:
                await worker.shutdown_active()
            failed = await self.repo.fail_all_running_tasks()
            for task in list(self._retire_tasks):
                with suppress(Exception):
                    await task
            if self.r18_handle is not None and not self.r18_handle.closed:
                self.r18_handle.closed = True
                with suppress(Exception):
                    await self.r18_handle.engine.close()
            self._stopped = True
            logger.info("workers stopped", marked_failed=failed)

    def _ensure_open(self) -> None:
        if self._closing:
            raise RuntimeError("运行时正在关闭")

    def _replace_plugin_manager(self, discovered: PluginManager) -> None:
        discovered.validate_hot_settings(self.config.hot, require_available=False)
        self.plugin_manager = discovered
        for failure in discovered.failures:
            logger.warning(
                "source plugin unavailable",
                plugin=failure.name,
                path=failure.value,
                error=failure.error,
            )

    def _require_plugin_manager(self) -> PluginManager:
        manager = self.plugin_manager
        if manager is None:
            raise RuntimeError("来源插件目录未初始化")
        return manager


def build_handlers(
    repo: Repository,
    factory: CrawlerFactory,
    web_client: WebClient,
    resource_store: ResourceStore,
    hot: HotSettings,
    safe_dirs: Sequence[Path] | None = (),
    translation_cache: TranslationCache | None = None,
    state_dir: Path | None = None,
    plugin_manager: PluginManager | None = None,
    library_locks: LibraryTaskLocks | None = None,
    inventory_store: InventoryStore | None = None,
    availability: SourceAvailability | None = None,
) -> dict[TaskType, TaskHandler[Any, Any]]:
    # 未启用、或密钥模式下缺密钥时 translator 为 None, ScrapeHandler 跳过翻译.
    # 经 _rebuild() 热重载; 代理沿用 network.proxy.
    # 译文缓存是会话级, 热重载时复用同一实例.
    translator = build_translator(
        enabled=hot.llm.enabled,
        api_type=hot.llm.api_type,
        auth_mode=hot.llm.auth_mode,
        api_key=hot.llm.api_key,
        base_url=hot.llm.base_url,
        model=hot.llm.model,
        rate_limit=hot.llm.rate_limit,
        proxy=hot.network.proxy,
        device_id_path=None if state_dir is None else state_dir / DEVICE_ID_FILENAME,
        system_prompt=hot.llm.system_prompt,
        field_prompts=hot.llm.field_prompts,
        cache=translation_cache,
    )
    if library_locks is None:
        library_locks = LibraryTaskLocks()
    # 缺省自建只服务精简构造 (测试); 生产必须传入 AppRuntime 的那一份, 否则扫描写进的清单
    # 不在面板读取的存放里, 删除任务只会得到「清单不存在」.
    if inventory_store is None:
        inventory_store = InventoryStore()
    handlers: dict[TaskType, TaskHandler[Any, Any]] = {
        TaskType.REFRESH: RefreshHandler(repo, hot.watcher.media_extensions, inventory_store, availability),
        TaskType.SCRAPE: ScrapeHandler(
            repo,
            factory,
            resource_store,
            hot,
            web_client,
            translator,
            plugin_manager.descriptors() if plugin_manager is not None else None,
        ),
        TaskType.ACTOR_SCRAPE: ActorScrapeHandler(repo, factory, resource_store, hot, web_client),
        TaskType.ORGANIZE: OrganizeHandler(
            repo,
            hot,
            resource_store,
            web_client,
            safe_dirs,
            watermark_dir=user_watermark_dir(state_dir) if state_dir is not None else None,
            library_locks=library_locks,
        ),
        TaskType.SCAN_INVALID: ScanInvalidHandler(repo, hot, inventory_store),
        TaskType.DELETE: DeleteHandler(repo, inventory_store, hot, library_locks=library_locks),
        TaskType.CLEANUP: CleanupHandler(repo=repo, resource_store=resource_store),
        TaskType.UPSCALE: UpscaleHandler(resource_store, hot),
        TaskType.RESCRAPE: RescrapeHandler(repo, availability),
    }
    # state_dir 缺省回退 cwd/data (精简构造场景).
    handlers[TaskType.R18_IMPORT] = R18ImportHandler(
        config=hot.r18, web_client=web_client, state_dir=state_dir if state_dir is not None else Path("./data")
    )
    return handlers
