import asyncio
from typing import TYPE_CHECKING

from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from . import repos

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from .indexing import MetadataIndexSink


class Repository(
    repos.AgentRepoMixin,
    repos.FacetsRepoMixin,
    repos.FeedsRepoMixin,
    repos.LibrariesRepoMixin,
    repos.MediaRepoMixin,
    repos.MetadataRepoMixin,
    repos.SchedulesRepoMixin,
    repos.TasksRepoMixin,
):
    def __init__(self, engine: AsyncEngine):
        self._engine = engine
        # SQLite DEFERRED: 两个 session 的 SELECT 都在写锁前看到空表. 入队互斥检查与 INSERT 同锁.
        self._task_insert_lock = asyncio.Lock()
        # 检索索引的写入通知; 检索关闭时为 None (见 docs/dev/search.md).
        self.index_sink: MetadataIndexSink | None = None

    def set_index_sink(self, sink: MetadataIndexSink | None) -> None:
        """由组合根注入 / 撤销; 撤销后写入不再产生索引副作用."""
        self.index_sink = sink

    async def initialize(self) -> None:
        """``create_all`` 仅补缺表; schema 演进须经 Alembic."""
        async with self._engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    def _session(self) -> AsyncSession:
        return AsyncSession(self._engine, expire_on_commit=False)
