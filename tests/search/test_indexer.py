"""索引同步: 写路径入队、删除同步、回填对齐、模型变更重建."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlmodel import SQLModel

from amane.config.manager import EmbeddingConfig, SearchConfig
from amane.db.engine import create_async_engine_from_path
from amane.db.repository import Repository
from amane.search import FastEmbedProvider, IndexEntry, MetadataIndexer, SearchService, SearchStore
from amane.search.embedding import l2_normalize

pytestmark = pytest.mark.asyncio

DIM = 8
MODEL = "test:fake"


class StubProvider:
    """定长假向量: 只要入参相同就产出相同向量, 便于断言"重建后仍能召回"."""

    def __init__(self, model_id: str = MODEL, dim: int = DIM) -> None:
        self._model_id = model_id
        self._dim = dim
        self.calls = 0

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts):
        self.calls += 1
        out: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self._dim
            # 用文本长度散列到某一维: 稳定且彼此可分.
            vector[sum(map(ord, text)) % self._dim] = 1.0
            out.append(l2_normalize(vector))
        return out

    async def aclose(self) -> None:
        return None


async def _repo(tmp_path: Path) -> tuple[Repository, Path]:
    path = tmp_path / "amane.db"
    engine = await create_async_engine_from_path(path)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    return Repository(engine), path


async def _stack(tmp_path: Path, *, vector: bool = False, provider: StubProvider | None = None):
    repo, db_path = await _repo(tmp_path)
    config = SearchConfig(vector_enabled=vector, embedding=EmbeddingConfig(dim=DIM))
    provider = provider if provider is not None else (StubProvider() if vector else None)
    store = SearchStore(db_path, dim=DIM, model=provider.model_id if provider else MODEL)
    await store.open()
    indexer = MetadataIndexer(store=store, repo=repo, config=config, provider=provider)
    repo.set_index_sink(indexer)
    service = SearchService(store=store, provider=provider, config=config)
    return repo, store, indexer, service


async def test_write_is_indexed_without_blocking(tmp_path: Path) -> None:
    """落库后不 await 索引; 排空队列即可搜到."""
    repo, store, indexer, service = await _stack(tmp_path)
    indexer.start()
    await repo.upsert_metadata("SSIS-001", title="人妻の記録", actors=["葵つかさ"], tags=["人妻"])
    assert (await store.stats()).keyword_docs == 0, "写入路径不应等待索引"
    await indexer.drain()
    result = await service.search("SSIS-001", limit=5)
    assert result.mode is service.mode
    assert len(result.ids) == 1
    await indexer.aclose()
    await store.aclose()


async def test_update_refreshes_index(tmp_path: Path) -> None:
    repo, store, indexer, service = await _stack(tmp_path)
    indexer.start()
    meta = await repo.upsert_metadata("ABC-123", title="旧标题")
    await indexer.drain()
    await repo.update_metadata(meta.id or 0, title="全新的标题")
    await indexer.drain()
    assert (await service.search("全新的标题", limit=5)).ids == [meta.id]
    assert (await service.search("旧标题", limit=5)).ids == []
    await indexer.aclose()
    await store.aclose()


async def test_wait_idle_covers_job_already_taken(tmp_path: Path) -> None:
    """消费者已取走任务但尚未落盘时, ``wait_idle`` 不能判"空闲".

    队列在这一瞬间是空的 (任务离开了队列), 但还没 ``task_done``. 按 ``empty()`` 判空闲会让
    搜索侧的有界等待白等: 返回后索引仍没有这一行, 读己之写失效.
    """
    repo, store, indexer, _ = await _stack(tmp_path)
    indexer.start()
    meta = await repo.upsert_metadata("WAIT-001", title="等索引")
    assert meta.id is not None
    # 让消费者取走任务并进入写盘阶段 (此刻队列空, 任务未完成).
    await asyncio.sleep(0)
    assert await indexer.wait_idle(5.0)
    assert await store.indexed_ids() == {meta.id}
    await indexer.aclose()
    await store.aclose()


async def test_delete_removes_index(tmp_path: Path) -> None:
    repo, store, indexer, service = await _stack(tmp_path)
    indexer.start()
    meta = await repo.upsert_metadata("DEL-001", title="待删除")
    await indexer.drain()
    assert (await service.search("待删除", limit=5)).ids == [meta.id]
    assert await repo.delete_metadata(meta.id or 0)
    await indexer.drain()
    assert (await service.search("待删除", limit=5)).ids == []
    assert (await store.stats()).keyword_docs == 0
    await indexer.aclose()
    await store.aclose()


async def test_batch_delete_removes_index(tmp_path: Path) -> None:
    repo, store, indexer, _ = await _stack(tmp_path)
    indexer.start()
    first = await repo.upsert_metadata("B-1", title="甲")
    second = await repo.upsert_metadata("B-2", title="乙")
    await indexer.drain()
    deleted, missing = await repo.batch_delete_metadata([first.id or 0, second.id or 0, 9999])
    assert (deleted, missing) == (2, 1)
    await indexer.drain()
    assert (await store.stats()).keyword_docs == 0
    await indexer.aclose()
    await store.aclose()


async def test_backfill_indexes_existing_rows(tmp_path: Path) -> None:
    """先落库再开检索: 回填把存量补齐, 并清掉指向已删行的孤儿索引."""
    repo, db_path = await _repo(tmp_path)
    await repo.upsert_metadata("OLD-001", title="存量条目")
    store = SearchStore(db_path, dim=DIM, model=MODEL)
    await store.open()
    # 造一条孤儿: 索引里有、业务表没有
    await store.upsert([IndexEntry(metadata_id=424242, number="GHOST-1", title="幽灵")])
    config = SearchConfig()
    indexer = MetadataIndexer(store=store, repo=repo, config=config)
    await indexer.backfill()
    indexed = await store.indexed_ids()
    assert 424242 not in indexed
    assert len(indexed) == 1
    service = SearchService(store=store, provider=None, config=config)
    assert (await service.search("存量条目", limit=5)).ids == list(indexed)
    await store.aclose()


async def test_backfill_fills_missing_vectors(tmp_path: Path) -> None:
    provider = StubProvider()
    repo, store, indexer, service = await _stack(tmp_path, vector=True, provider=provider)
    await repo.upsert_metadata("VEC-001", title="需要向量的条目")
    await indexer.backfill()
    stats = await store.stats()
    assert stats.vector_docs == 1
    assert stats.vec_index_docs == 1
    assert (await service.search("需要向量的条目", limit=5)).ids
    await store.aclose()


async def test_backfill_rebuilds_on_model_change(tmp_path: Path) -> None:
    """模型换掉后旧向量不可比: 丢弃并按新模型重算."""
    repo, store, indexer, _ = await _stack(tmp_path, vector=True, provider=StubProvider("model-a"))
    await repo.upsert_metadata("M-1", title="甲")
    await indexer.backfill()
    assert (await store.stats()).vector_model == "model-a"

    new_provider = StubProvider("model-b")
    rebuilt = MetadataIndexer(store=store, repo=repo, config=SearchConfig(vector_enabled=True), provider=new_provider)
    await rebuilt.backfill()
    stats = await store.stats()
    assert stats.vector_model == "model-b"
    assert stats.vector_docs == 1
    await store.aclose()


async def test_failed_embedding_degrades_without_raising(tmp_path: Path) -> None:
    """嵌入失败只让索引落后, 不能把异常抛回写路径."""

    class Broken(StubProvider):
        async def embed(self, texts):
            raise RuntimeError("端点不可用")

    repo, store, indexer, presentation = await _stack(tmp_path, vector=True, provider=Broken())
    indexer.start()
    meta = await repo.upsert_metadata("BRK-001", title="标题")
    await indexer.drain()
    assert indexer.failed >= 1
    # 词索引照旧可用 (降级而不是整体失败)
    assert (await presentation.search("标题", limit=5)).ids == [meta.id]
    assert meta.id is not None
    await indexer.aclose()
    await store.aclose()


async def test_write_to_missing_row_is_dropped(tmp_path: Path) -> None:
    """队列里的 id 在消费前被删掉: 不留孤儿索引."""
    _repo, store, indexer, _ = await _stack(tmp_path)
    indexer.notify_write([777])
    await asyncio.sleep(0)
    assert await store.upsert([]) == 0
    indexer.start()
    await indexer.drain()
    assert await store.indexed_ids() == set()
    await indexer.aclose()
    await store.aclose()


async def test_entry_text_order_is_stable() -> None:
    """嵌入文本以番号开头: 模型对开头权重更高, 标题与番号必须在最前."""
    from amane.search import entry_text

    entry = IndexEntry(
        metadata_id=1,
        number="SSIS-001",
        title="标题",
        plot="简介",
        actors=["演员"],
        tags=["标签"],
        series="系列",
        studio="厂牌",
        publisher="发行",
    )
    assert entry_text(entry) == "SSIS-001 标题 简介 演员 标签 系列 厂牌 发行"


async def test_fastembed_provider_is_lazy() -> None:
    """构造不加载模型; 首次 embed 才加载 (避免启动时同步阻塞)."""
    provider = FastEmbedProvider(model="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", dim=384)
    assert provider.dim == 384
    assert provider.model_id.startswith("fastembed:")
