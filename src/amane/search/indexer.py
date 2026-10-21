"""索引与业务写入之间的单向同步: 非阻塞队列 + 启动回填.

写路径只做 ``notify_*`` (同步入队, 不 await), 索引与嵌入推理都在后台消费者里做 ——
嵌入一条动辄数十毫秒, 让写事务等它不仅拖慢接口, 还会把模型故障放大成写失败.

索引是**派生数据**: 队列满、嵌入失败、进程崩溃都只意味着索引落后, 由 ``backfill`` 修复,
不回滚业务写入, 也不向调用方抛错.
"""

import asyncio
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, NamedTuple

import structlog

from ..config.manager import SearchConfig
from .embedding import EmbeddingProvider
from .store import IndexEntry, IndexStats, SearchStore

if TYPE_CHECKING:
    from ..db.models import Metadata
    from ..db.repository import Repository

logger = structlog.get_logger()

#: 单批索引条数. 受嵌入批大小与写事务长度共同约束: 太小则事务频繁, 太大则单次写入长时间持锁.
BATCH_SIZE = 32

#: 回填时每批读取的影片条数. 与 ``BATCH_SIZE`` 分开: 读库与嵌入的合适粒度不同.
BACKFILL_CHUNK = 200


def entry_text(entry: IndexEntry) -> str:
    """嵌入用的文本. 与 FTS 各列同源, 但不含译文 —— 跨语言由向量模型自身能力承担.

    顺序固定: 模型对开头更敏感, 番号与标题必须在最前.
    """
    parts: list[str] = [entry.number, entry.title or "", entry.plot or ""]
    parts.extend(entry.actors)
    parts.extend(entry.tags)
    parts.extend([entry.series or "", entry.studio or "", entry.publisher or ""])
    return " ".join(part for part in parts if part)


def entry_of(metadata: Metadata) -> IndexEntry:
    """``Metadata`` 行 → 索引条目 (不含向量)."""
    if metadata.id is None:
        raise ValueError("未落库的 Metadata 不能进索引")
    return IndexEntry(
        metadata_id=metadata.id,
        number=metadata.number,
        title=metadata.title,
        plot=metadata.plot,
        actors=list(metadata.actors or []),
        tags=list(metadata.tags or []),
        series=metadata.series,
        studio=metadata.studio,
        publisher=metadata.publisher,
    )


class IndexJob(NamedTuple):
    """一条待同步的索引变更. 写入与删除走同一条队列, 否则删除要等下一次写入才能被处理."""

    metadata_id: int
    drop: bool = False


class MetadataIndexer:
    """写路径的索引副作用与全量回填. 由 ``Repository`` 以 sink 身份持有."""

    def __init__(
        self,
        *,
        store: SearchStore,
        repo: Repository,
        config: SearchConfig,
        provider: EmbeddingProvider | None = None,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self._store = store
        self._repo = repo
        self._config = config
        self._provider = provider
        self._batch_size = batch_size
        self._queue: asyncio.Queue[IndexJob] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._backfill_task: asyncio.Task[None] | None = None
        self._closing = False
        self._flushed = asyncio.Event()
        self._flushed.set()
        self.indexed = 0
        self.removed = 0
        self.failed = 0

    # ---------------------------------------------------------------- 写路径接口

    def notify_write(self, metadata_ids: Iterable[int]) -> None:
        """同步入队. 队列无界: 宁可让索引落后, 也不阻塞写事务."""
        if self._closing:
            return
        queued = False
        for metadata_id in metadata_ids:
            self._queue.put_nowait(IndexJob(metadata_id))
            queued = True
        if queued:
            self._flushed.clear()

    def notify_delete(self, metadata_ids: Iterable[int]) -> None:
        if self._closing:
            return
        queued = False
        for metadata_id in metadata_ids:
            self._queue.put_nowait(IndexJob(metadata_id, drop=True))
            queued = True
        if queued:
            self._flushed.clear()

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._consume())

    def start_backfill(self) -> asyncio.Task[None]:
        """回填放后台: 万级影片的嵌入要数分钟, 不能挡住启动."""
        if self._backfill_task is None or self._backfill_task.done():
            self._backfill_task = asyncio.create_task(self.backfill())
        return self._backfill_task

    async def wait_backfill(self) -> None:
        """测试与关闭路径用; 不等则可能读到半完成的索引."""
        if self._backfill_task is not None:
            await asyncio.gather(self._backfill_task, return_exceptions=True)

    async def drain(self) -> None:
        """等队列排空. 供测试与关闭使用."""
        await self._flushed.wait()

    async def wait_idle(self, timeout: float) -> bool:
        """有限等待已入队的变更落盘, 返回是否真的落完了.

        读路径用它换取"读己之写": 写入是异步的, 刚落库的行若立刻搜索会漏. 但等待必须有界 ——
        批量刮削可能一次塞进数百条, 搜索不能陪着它把嵌入跑完.

        判据是"入库完成"而不是"队列为空": 任务被取出后到 ``task_done`` 之前队列同样是空的,
        按 ``empty()`` 判空闲会在这个窗口里立刻返回, 让等待白等.
        """
        if self._flushed.is_set():
            return True
        try:
            await asyncio.wait_for(self._flushed.wait(), timeout=timeout)
        except TimeoutError:
            logger.debug("索引仍有积压, 搜索按现有索引返回", pending=self._queue.qsize(), timeout=timeout)
            return False
        return True

    async def aclose(self) -> None:
        # 先置位再排空: 关闭期间的写入不再入队, 由下一次启动的回填补齐.
        self._closing = True
        if self._backfill_task is not None and not self._backfill_task.done():
            self._backfill_task.cancel()
            await asyncio.gather(self._backfill_task, return_exceptions=True)
        if self._task is not None:
            await self._queue.join()
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._apply_pending()

    # ---------------------------------------------------------------- 消费

    async def _consume(self) -> None:
        while True:
            job = await self._queue.get()
            batch = [job]
            while len(batch) < self._batch_size:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self._apply(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()
                if self._queue.empty():
                    self._flushed.set()

    async def _apply_pending(self) -> None:
        """把已入队但未消费的变更一并落盘 (关闭时最后一段)."""
        pending: list[IndexJob] = []
        while True:
            try:
                pending.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not pending:
            return
        await self._apply(pending)
        for _ in pending:
            self._queue.task_done()

    async def _apply(self, jobs: Sequence[IndexJob]) -> None:
        """一次批量落盘: 先删, 再按现存行写词索引与向量."""
        deletes = sorted({job.metadata_id for job in jobs if job.drop})
        write_ids = list(dict.fromkeys(job.metadata_id for job in jobs if not job.drop))
        try:
            if deletes:
                self.removed += await self._store.remove(deletes)
            if not write_ids:
                return
            rows = await self._repo.get_metadata_many(write_ids)
            entries = [entry_of(row) for row in rows.values()]
            # 队列里的 id 可能已被删除: 按删除处理, 不留孤儿索引.
            gone = [metadata_id for metadata_id in write_ids if metadata_id not in rows]
            if gone:
                await self._store.remove(gone)
            if not entries:
                return
            await self._attach_embeddings(entries)
            self.indexed += await self._store.upsert(entries, model=self._model_id)
        except Exception as e:
            # 索引失败一律吞掉: 派生数据落后可回填, 不能让它冒泡到业务写路径.
            self.failed += 1
            logger.warning("索引更新失败, 待回填修复", error=str(e), ids=len(write_ids), deleted=len(deletes))
        finally:
            if self._queue.empty():
                self._flushed.set()

    async def _attach_embeddings(self, entries: list[IndexEntry]) -> None:
        """嵌入失败只让向量落后, 词索引照旧写入 —— 否则一个坏模型会把检索整个拖垮."""
        provider = self._provider
        if provider is None or not self._config.vector_enabled:
            return
        try:
            vectors = await provider.embed([entry_text(entry) for entry in entries])
        except Exception as e:
            self.failed += 1
            logger.warning("嵌入失败, 本批只更新词索引", error=str(e), count=len(entries))
            return
        for entry, vector in zip(entries, vectors, strict=True):
            entry.embedding = vector

    @property
    def _model_id(self) -> str:
        """入库标注要跟随**当前** provider, 不能沿用 store 构造时的名字: 换模型后若仍标旧名,
        下次启动会误判"模型没变"而跳过重建, 留下两个模型的向量混在一张表里."""
        return self._provider.model_id if self._provider is not None else self._store.model

    # ---------------------------------------------------------------- 回填

    async def backfill(self) -> None:
        """把索引对齐到业务表. 幂等, 可反复执行.

        三种落后情形一起处理: 缺词索引、缺向量、向量由旧模型产生 (模型或维度变了).
        """
        try:
            await self._reconcile_vectors()
            known = await self._store.indexed_ids()
            all_ids = await self._repo.all_metadata_ids()
            missing = [metadata_id for metadata_id in all_ids if metadata_id not in known]
            stale = sorted(known - set(all_ids))
            if stale:
                # 业务表已没有、索引还留着的行: 删除任务失败或直接改库造成.
                await self._store.remove(stale)
                self.removed += len(stale)
            if missing:
                logger.info("检索索引回填开始", missing=len(missing), total=len(all_ids))
            await self._backfill_ids(missing)
            await self._backfill_vectors()
            if missing or stale:
                logger.info("检索索引回填完成", indexed=len(missing), removed=len(stale))
        except Exception as e:
            logger.warning("检索索引回填中断, 下次启动重试", error=str(e))

    async def _reconcile_vectors(self) -> None:
        """模型或维度变化: 现有向量不再可比, 整批丢弃后由回填重算."""
        provider = self._provider
        if provider is None or not self._config.vector_enabled:
            return
        stats = await self._store.stats()
        model_changed = stats.vector_model is not None and stats.vector_model != provider.model_id
        dim_changed = stats.vector_dim is not None and stats.vector_dim != provider.dim
        if model_changed or dim_changed:
            logger.warning(
                "嵌入模型变更, 丢弃旧向量",
                was_model=stats.vector_model,
                was_dim=stats.vector_dim,
                now_model=provider.model_id,
                now_dim=provider.dim,
            )
            await self._store.drop_vectors()

    async def _backfill_ids(self, ids: Sequence[int]) -> None:
        for start in range(0, len(ids), BACKFILL_CHUNK):
            chunk = list(ids[start : start + BACKFILL_CHUNK])
            rows = await self._repo.get_metadata_many(chunk)
            entries = [entry_of(row) for row in rows.values()]
            if not entries:
                continue
            await self._attach_embeddings(entries)
            self.indexed += await self._store.upsert(entries, model=self._model_id)

    async def _backfill_vectors(self) -> None:
        """补上"有词索引但没向量"的行; 向量路关闭时跳过."""
        provider = self._provider
        if provider is None or not self._config.vector_enabled:
            return
        pending = sorted(await self._store.missing_vector_ids())
        if not pending:
            return
        logger.info("向量回填开始", pending=len(pending))
        await self._backfill_ids(pending)

    async def index_stats(self) -> IndexStats:
        return await self._store.stats()
