from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2

from ..db.models import DEFAULT_SESSION_TITLE, AgentSession, AgentSessionStatus
from .bridge import AgentRuntimeBridge
from .cache import ResultCache
from .executor import QueryExecutor
from .limits import AgentRateLimiter
from .naming import generate_title
from .rows import CancelledRow, no_approvals
from .runtime import build_agent, build_http_client, build_model, parse_session_thinking
from .sql import ReadonlySqlSandbox
from .tools import AgentDeps
from .trace import SessionStore, delete_session_dir, session_dir

if TYPE_CHECKING:
    from pydantic_ai import Agent, DeferredToolRequests
    from pydantic_ai.models import Model

    from ..config import AgentConfig, AgentThinkingMode
    from ..db.repository import Repository


_MAX_RETIRED_CLIENTS = 8
"""热重载时保留的旧传输客户端上限.

保留是必要的: ``rebuild`` 由"保存设置"触发, 那一刻可能还有回合在跑 (另一个标签页在对话).
就地 ``aclose`` 会把在飞的请求打断, 现象是"改完设置对话突然失败". 上限只为避免反复保存时
无限堆对象 —— 被挤出去的那个已经空闲很久了.
"""


@dataclass
class AgentService:
    db_path: Path
    data_dir: Path
    repo: Repository
    cache: ResultCache
    config: AgentConfig
    network_proxy: str | None = None
    """全局代理 (``network.proxy``) 的当前值, 供 ``agent.use_proxy`` 未填专用地址时回退.

    由装配处传入而不是让本对象去读 ``HotSettings``: 助理只该知道自己那份配置加一个回退值,
    否则配置对象之间会互相引用, 热重载顺序也变得要紧.
    """
    sandbox: ReadonlySqlSandbox = field(init=False)
    executor: QueryExecutor = field(init=False)
    agent: Agent[AgentDeps, str | DeferredToolRequests] | None = field(init=False, default=None)
    naming_model: Model | None = field(init=False, default=None)
    bridge: AgentRuntimeBridge = field(default_factory=AgentRuntimeBridge)
    _stores: dict[int, SessionStore] = field(default_factory=dict)
    _turn_tasks: dict[int, asyncio.Task[None]] = field(default_factory=dict)
    _limiter: AgentRateLimiter | None = field(init=False, default=None)
    _limiter_key: tuple[float, int | None, float, float, float, float] = field(
        init=False, default=(0.0, None, 0.0, 0.0, 0.0, 0.0)
    )
    _http_client: httpx2.AsyncClient | None = field(init=False, default=None)
    _retired: list[httpx2.AsyncClient] = field(init=False, default_factory=list)
    _closing: set[asyncio.Task[None]] = field(init=False, default_factory=set)

    def __post_init__(self) -> None:
        self.sandbox = ReadonlySqlSandbox(self.db_path)
        self.executor = QueryExecutor(self.sandbox, self.cache)
        self.rebuild(self.config, self.network_proxy)

    def rebuild(self, config: AgentConfig, network_proxy: str | None = None) -> None:
        """按新配置重建模型与 agent. ``network_proxy`` 为 ``hot.network.proxy`` 的当前值.

        代理在这里解析: ``agent.use_proxy`` 是总闸, ``agent.proxy`` 是覆盖, 都空才回退全局.
        客户端自建且 ``trust_env=False`` (见 ``runtime.build_http_client``) —— pydantic-ai 托管的
        默认客户端只认环境变量, 设置页里配的代理对它无效.
        """
        self.config = config
        self.network_proxy = network_proxy
        self.cache.configure(ttl_s=config.result_cache_ttl_s, max_entries=config.result_cache_max_entries)
        # 回合与会话标题共用同一个桶: 首条用户输入时两者并行, 上游按并发计量算两次
        limiter = self.request_limiter()
        if config.api_key:
            client = build_http_client(config.resolved_proxy(network_proxy))
            # 两者打同一个端点, 共用一个客户端; 各建一个只是多一份连接池
            self.agent = build_agent(config, limiter, http_client=client)
            self.naming_model = build_model(config, limiter, http_client=client)
        else:
            client = None
            self.agent = None
            self.naming_model = None
        self._replace_client(client)

    def _replace_client(self, client: httpx2.AsyncClient | None) -> None:
        """换成新客户端; 旧的先进退休队列, 不就地关闭.

        ``rebuild`` 是"保存设置"同步触发的, 那一刻可能还有回合在跑 (另一个标签页). 就地
        ``aclose`` 会让在飞的请求抛 "client has been closed", 表现为"改完设置对话就失败".
        队列超过 ``_MAX_RETIRED_CLIENTS`` 才关最老的那个 —— 它早已空闲.
        """
        previous, self._http_client = self._http_client, client
        if previous is not None and previous is not client:
            self._retired.append(previous)
        while len(self._retired) > _MAX_RETIRED_CLIENTS:
            self._schedule_close(self._retired.pop(0))

    def _schedule_close(self, client: httpx2.AsyncClient) -> None:
        """同步调用点没有 await 的机会: 有事件循环就排任务, 没有 (启动期) 交 GC.

        启动期没有旧客户端 (``previous`` 为 None), 走不到这里, 因此不会留下未关闭的连接池.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(client.aclose())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def aclose(self) -> None:
        """关闭助理持有的全部传输客户端. 进程退出时由 ``AppSession.aclose`` 调用."""
        current, self._http_client = self._http_client, None
        retired, self._retired = self._retired, []
        for client in [*retired, current]:
            if client is not None:
                await client.aclose()
        closing, self._closing = self._closing, set()
        for task in closing:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def request_limiter(self) -> AgentRateLimiter:
        """助理模型请求的限速器: 频率桶 / token 预算 / 限流冷却共用一份状态, 全部会话与标题请求共享.

        按配置惰性构造并缓存; 任一项变化时换新实例, 否则旧桶一直沿用旧值 (改配置后不重建
        就等于没改). 共用是必要条件: 每回合各建一个时, 并发会话各自放行, 上游看到的速率
        是配置值的数倍.
        """
        key = (
            self.config.rate_limit,
            self.config.tpm_limit,
            self.config.rate_limit_cooldown_s,
            self.config.tpm_window_s,
            self.config.rate_limit_backoff_factor,
            self.config.rate_limit_max_cooldown_s,
        )
        if self._limiter is None or self._limiter_key != key:
            self._limiter = AgentRateLimiter(
                rate=self.config.rate_limit,
                tpm=self.config.tpm_limit,
                cooldown_s=self.config.rate_limit_cooldown_s,
                window_s=self.config.tpm_window_s,
                backoff_factor=self.config.rate_limit_backoff_factor,
                max_cooldown_s=self.config.rate_limit_max_cooldown_s,
            )
            self._limiter_key = key
        return self._limiter

    def store_for(self, session_id: int) -> SessionStore:
        store = self._stores.get(session_id)
        if store is None:
            store = SessionStore(session_dir(self.data_dir, session_id))
            self._stores[session_id] = store
        return store

    def is_turn_running(self, session_id: int) -> bool:
        task = self._turn_tasks.get(session_id)
        if task is not None and not task.done():
            return True
        return self.store_for(session_id).turn_running

    def track_turn(self, session_id: int, task: asyncio.Task[None]) -> None:
        """登记后台回合任务; 取消与删除会话据此终止它."""
        self._turn_tasks[session_id] = task

        def _clear(finished: asyncio.Task[None]) -> None:
            if self._turn_tasks.get(session_id) is finished:
                self._turn_tasks.pop(session_id, None)

        task.add_done_callback(_clear)

    async def create_session(self, title: str = DEFAULT_SESSION_TITLE) -> AgentSession:
        session = await self.repo.create_agent_session(title=title)
        assert session.id is not None
        store = self.store_for(session.id)
        store.write_meta(
            {
                "session_id": session.id,
                "title": session.title,
                "turn_running": False,
                "thinking": None,
            }
        )
        return session

    async def name_session(self, session_id: int, prompt: str) -> str:
        """按首条用户输入生成标题并写回. 调用方与主回合并行执行, 不等待回合结束."""
        title = await generate_title(self.naming_model, prompt)
        if await self.repo.update_agent_session(session_id, title=title) is None:
            return title
        store = self.store_for(session_id)
        meta = store.read_meta()
        meta["title"] = title
        store.write_meta(meta)
        return title

    def session_thinking(self, session_id: int) -> AgentThinkingMode | None:
        return parse_session_thinking(self.store_for(session_id).read_meta().get("thinking"))

    def set_session_thinking(self, session_id: int, thinking: AgentThinkingMode | None) -> None:
        """thinking=None 表示继承全局默认."""
        store = self.store_for(session_id)
        meta = store.read_meta()
        meta["thinking"] = thinking if thinking is not None else None
        store.write_meta(meta)

    async def delete_session(self, session_id: int) -> bool:
        """回合进行中则先取消其后台任务."""
        task = self._turn_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # 回合已取消, 不会再新增未保留预设; 这些行随会话删除且删除路径不回传 id, 故先快照供缓存失效
        ephemeral_ids = [
            q.id
            for q in await self.repo.list_saved_queries(session_id=session_id)
            if q.id is not None and not q.persisted
        ]
        ok = await self.repo.delete_agent_session(session_id)
        if ok:
            for query_id in ephemeral_ids:
                self.cache.invalidate(query_id)
            self._stores.pop(session_id, None)
            delete_session_dir(self.data_dir, session_id)
        return ok

    def _make_deps(self, session_id: int) -> AgentDeps:
        return AgentDeps(
            repo=self.repo,
            executor=self.executor,
            session_id=session_id,
            sql_timeout_ms=self.config.sql_timeout_ms,
            bridge=self.bridge,
        )

    async def cancel_turn(self, session_id: int) -> bool:
        """显式终止后台回合; 断连不会进入此路径."""
        session = await self.repo.get_agent_session(session_id)
        if session is None:
            raise KeyError(f"session {session_id} 不存在")
        if not self.is_turn_running(session_id):
            return False
        task = self._turn_tasks.get(session_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        else:
            # 无任务 (进程异常态): 补写终态并清除 turn_running
            await self._write_cancelled(session_id)
        return True

    async def _write_cancelled(self, session_id: int) -> None:
        store = self.store_for(session_id)
        await store.append_row(CancelledRow(type="cancelled"))
        await store.append_row(no_approvals())
        await self.repo.update_agent_session(session_id, status=AgentSessionStatus.ACTIVE)
        store.set_turn_running(False)
