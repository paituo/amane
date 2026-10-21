"""索引存储与融合服务: 开关矩阵、融合口径、降级与非法输入."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from amane.config.manager import EmbeddingConfig, FusionConfig, SearchConfig, VectorEngine
from amane.enums import SearchMode
from amane.search import (
    EmbeddingError,
    EmbeddingProvider,
    IndexEntry,
    IndexStats,
    SearchService,
    SearchStore,
)
from amane.search.embedding import l2_normalize

pytestmark = pytest.mark.asyncio

DIM = 8
MODEL = "test:fake"

#: 令牌化后的期望向量: 用维度 8 的稀疏向量表达"语义相近".
VECTORS: dict[str, list[float]] = {
    "护士": [1, 0, 0, 0, 0, 0, 0, 0],
    "看護師": [0.98, 0.1, 0, 0, 0, 0, 0, 0],
    "人妻": [0, 1, 0, 0, 0, 0, 0, 0],
    "海": [0, 0, 1, 0, 0, 0, 0, 0],
}

CORPUS = [
    IndexEntry(
        metadata_id=1,
        number="SSIS-001",
        title="新人看護師の優しい手ほどき",
        plot="病院で働く看護師の物語",
        actors=["三上悠亜"],
        tags=["単体作品"],
        embedding=[1, 0, 0, 0, 0, 0, 0, 0],
    ),
    IndexEntry(
        metadata_id=2,
        number="SSIS-497",
        title="巨乳人妻の濃厚な性交",
        plot="人妻が溺れていく",
        actors=["深田えいみ"],
        tags=["人妻"],
        embedding=[0, 1, 0, 0, 0, 0, 0, 0],
    ),
    IndexEntry(
        metadata_id=3,
        number="FC2-PPV-1234567",
        title="素人娘 湘南の海で",
        plot="海辺のひと夏",
        actors=["天海つばさ"],
        tags=["素人"],
        embedding=[0, 0, 1, 0, 0, 0, 0, 0],
    ),
]


class StubProvider:
    """按关键词查表的假 provider; key 缺失时给一个与任何语料都无关的向量."""

    model_id = MODEL
    dim = DIM

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls = 0

    async def embed(self, texts):
        self.calls += 1
        if self._fail:
            raise EmbeddingError("端点不可用")
        out: list[list[float]] = []
        for text in texts:
            for key, vector in VECTORS.items():
                if key in text:
                    out.append(l2_normalize(vector))
                    break
            else:
                out.append(l2_normalize([0, 0, 0, 0, 0, 0, 0, 1]))
        return out

    async def aclose(self) -> None:
        return None


def _make_db(path: Path) -> None:
    """LIKE 回退要读业务表, 因此建一份最小 metadata."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE metadata(id INTEGER PRIMARY KEY, number TEXT, title TEXT, plot TEXT, actors TEXT, tags TEXT)"
    )
    conn.executemany(
        "INSERT INTO metadata VALUES (?,?,?,?,?,?)",
        [
            (1, "SSIS-001", "新人看護師の優しい手ほどき", "病院で働く看護師", "三上悠亜", "単体作品"),
            (2, "SSIS-497", "巨乳人妻の濃厚な性交", "人妻が溺れていく", "深田えいみ", "人妻"),
            (3, "FC2-PPV-1234567", "素人娘 湘南の海で", "海辺のひと夏", "天海つばさ", "素人"),
        ],
    )
    conn.commit()
    conn.close()


async def _store(tmp_path: Path) -> SearchStore:
    path = tmp_path / "amane.db"
    _make_db(path)
    store = SearchStore(path, dim=DIM, model=MODEL)
    await store.open()
    await store.upsert(CORPUS)
    return store


def _service(store: SearchStore, provider: EmbeddingProvider | None, **overrides) -> SearchService:
    config = SearchConfig(
        embedding=EmbeddingConfig(dim=DIM),
        fusion=FusionConfig(zgate=0.5, word_weight=2.0),
        **overrides,
    )
    return SearchService(store=store, provider=provider, config=config)


async def test_vec0_engine_available(tmp_path: Path) -> None:
    """sqlite-vec 是默认引擎; 加载失败时测试应以回退路径继续, 但这里要求真的可用."""
    store = await _store(tmp_path)
    assert store.vector_engine_ready
    await store.aclose()


async def test_stats_roundtrip(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    stats = await store.stats()
    assert isinstance(stats, IndexStats)
    assert stats.keyword_docs == len(CORPUS)
    assert stats.vector_docs == len(CORPUS)
    assert stats.vector_model == MODEL
    assert stats.vector_dim == DIM
    await store.aclose()


#: (查询, 期望命中的 id 集合)
KEYWORD_CASES: list[tuple[str, set[int]]] = [
    ("SSIS-001", {1}),
    ("ssis001", {1}),
    ("SSIS 001", {1}),
    ("FC2-PPV-1234567", {3}),
    ("人妻", {2}),
    ("看護師", {1}),
    ("湘南", {3}),
    ("001", {1}),
]


@pytest.mark.parametrize(("query", "expected"), KEYWORD_CASES)
async def test_keyword_search(tmp_path: Path, query: str, expected: set[int]) -> None:
    store = await _store(tmp_path)
    hits = await store.search_keyword(query, limit=10)
    assert {hit.metadata_id for hit in hits} == expected
    await store.aclose()


async def test_single_char_falls_back_to_like(tmp_path: Path) -> None:
    """单字查询 FTS5 表达不了, 走子串扫描并命中所有含该字的文本列."""
    store = await _store(tmp_path)
    hits = await store.search_keyword("海", limit=10)
    assert {hit.metadata_id for hit in hits} == {3}
    await store.aclose()


async def test_keyword_search_empty_and_blank(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    assert await store.search_keyword("", limit=10) == []
    assert await store.search_keyword("   ", limit=10) == []
    assert await store.search_keyword("人妻", limit=0) == []
    await store.aclose()


async def test_remove_drops_from_both_indexes(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await store.remove([2])
    stats = await store.stats()
    assert stats.keyword_docs == 2
    assert stats.vector_docs == 2
    assert await store.search_keyword("人妻", limit=10) == []
    await store.aclose()


async def test_upsert_overwrites_previous_version(tmp_path: Path) -> None:
    """同一 id 重新索引时旧 token 必须消失, 否则会命中已改掉的内容."""
    store = await _store(tmp_path)
    await store.upsert(
        [IndexEntry(metadata_id=1, number="SSIS-001", title="無関係な題名", embedding=[1, 0, 0, 0, 0, 0, 0, 0])]
    )
    assert {hit.metadata_id for hit in await store.search_keyword("看護師", limit=10)} == set()
    assert {hit.metadata_id for hit in await store.search_keyword("無関係", limit=10)} == {1}
    assert (await store.stats()).keyword_docs == len(CORPUS)
    await store.aclose()


async def test_dim_mismatch_rejected(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    with pytest.raises(Exception, match="维度"):
        await store.upsert([IndexEntry(metadata_id=1, number="X-1", embedding=[1.0, 2.0])])
    await store.aclose()


async def test_vector_search_respects_dim(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    with pytest.raises(Exception, match="维度"):
        await store.search_vector([1.0, 2.0], k=3, use_vec0=True)
    await store.aclose()


#: (配置覆盖, 期望 mode)
MODE_CASES: list[tuple[dict[str, object], SearchMode]] = [
    ({"keyword_enabled": False, "vector_enabled": False}, SearchMode.LIKE),
    ({"keyword_enabled": True, "vector_enabled": False}, SearchMode.KEYWORD),
    ({"keyword_enabled": False, "vector_enabled": True}, SearchMode.VECTOR),
    ({"keyword_enabled": True, "vector_enabled": True}, SearchMode.HYBRID),
]


@pytest.mark.parametrize(("overrides", "expected"), MODE_CASES)
async def test_mode_matrix(tmp_path: Path, overrides: dict[str, object], expected: SearchMode) -> None:
    store = await _store(tmp_path)
    service = _service(store, StubProvider(), **overrides)
    assert service.mode is expected
    result = await service.search("人妻", limit=5)
    assert result.mode is expected
    await store.aclose()


async def test_like_mode_returns_no_ids(tmp_path: Path) -> None:
    """全关时 service 不代劳旧路径: 返回空 id 并标记 LIKE, 由调用方走原查询."""
    store = await _store(tmp_path)
    service = _service(store, None, keyword_enabled=False, vector_enabled=False)
    result = await service.search("人妻", limit=5)
    assert result.ids == []
    assert result.mode is SearchMode.LIKE
    await store.aclose()


async def test_vector_mode_survives_keyword_miss(tmp_path: Path) -> None:
    """跨语言: "护士" 关键词 0 命中, 向量路必须能召回日文"看護師"条目."""
    store = await _store(tmp_path)
    service = _service(store, StubProvider(), vector_enabled=True, keyword_enabled=True)
    result = await service.search("护士", limit=5)
    assert result.mode is SearchMode.HYBRID
    assert result.ids[0] == 1
    await store.aclose()


async def test_keyword_exact_number_not_lost_by_gate(tmp_path: Path) -> None:
    """番号在语义上不突出, 但必须凭词命中加分进入结果."""
    store = await _store(tmp_path)
    service = _service(store, StubProvider(), vector_enabled=True)
    result = await service.search("SSIS-001", limit=5)
    assert 1 in result.ids
    await store.aclose()


async def test_provider_failure_degrades_to_keyword(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    service = _service(store, StubProvider(fail=True), vector_enabled=True)
    result = await service.search("人妻", limit=5)
    assert result.degraded
    assert result.mode is SearchMode.KEYWORD
    assert result.ids == [2]
    await store.aclose()


async def test_missing_provider_counts_as_degraded(tmp_path: Path) -> None:
    """配了 vector_enabled 却没接 provider: 对外必须是降级, 不能表现成"关键词就是全部"."""
    store = await _store(tmp_path)
    service = _service(store, None, vector_enabled=True)
    result = await service.search("人妻", limit=5)
    assert result.mode is SearchMode.KEYWORD
    assert result.degraded
    await store.aclose()


async def test_exact_number_probe_is_separate_from_keyword_ranking(tmp_path: Path) -> None:
    """FTS5 不回报命中了哪一列, 精确番号由独立探针给出, 不掺进 bm25 排序."""
    store = await _store(tmp_path)
    exact = await store.exact_number_ids("SSIS-001", limit=10)
    assert exact == [1]
    # 纯文本命中不产生精确编号: 探针只认落在 number 列的归一原子.
    assert await store.exact_number_ids("看護師", limit=10) == []
    await store.aclose()


class _FixedVectorProvider:
    """任意输入都给同一个向量, 用来把语义近邻的相似度顶到最高."""

    model_id = MODEL
    dim = DIM

    def __init__(self, vector: list[float]) -> None:
        self._vector = l2_normalize(vector)

    async def embed(self, texts):
        return [list(self._vector) for _ in texts]

    async def aclose(self) -> None:
        return None


async def test_exact_number_tier_outranks_semantic_neighbor(tmp_path: Path) -> None:
    """词命中加分是常数, z 分数上界随候选数增长 —— 番号精确命中必须自成一层.

    这里把 word_weight 压到 0.5, 模拟大语料下"固定加分必然被 z 压过"的情形:
    若只靠加权求和, 语义近邻 (id=2) 会排在精确番号 (id=1) 前面.
    """
    store = await _store(tmp_path)
    config = SearchConfig(
        embedding=EmbeddingConfig(dim=DIM),
        fusion=FusionConfig(zgate=0.5, word_weight=0.5),
        vector_enabled=True,
    )
    service = SearchService(store=store, provider=_FixedVectorProvider([0, 1, 0, 0, 0, 0, 0, 0]), config=config)
    result = await service.search("SSIS-001", limit=5)
    assert result.ids[0] == 1
    await store.aclose()


async def test_vector_only_still_puts_exact_number_first(tmp_path: Path) -> None:
    """纯向量模式下精确番号层不能缺席.

    曾经的缺陷: 番号探针长在 ``search_keyword`` 里, 关掉关键词开关就一并关掉了,
    于是搜 ``SSIS-001`` 先给同厂牌语义近邻. 现在探针与开关无关.
    """
    store = await _store(tmp_path)
    # 固定向量把 id=2 的相似度顶到最高, 模拟"语义上更像"的同厂牌近邻.
    provider = _FixedVectorProvider([0, 1, 0, 0, 0, 0, 0, 0])
    service = _service(store, provider, vector_enabled=True, keyword_enabled=False)
    result = await service.search("SSIS-001", limit=5)
    assert result.mode is SearchMode.VECTOR
    assert result.ids[0] == 1
    await store.aclose()


class _NotReadyProvider:
    """声明了预热能力但始终未就绪; 用来验证服务侧有界降级而不是干等."""

    model_id = MODEL
    dim = DIM

    def __init__(self) -> None:
        self.embed_calls = 0

    @property
    def ready(self) -> bool:
        return False

    async def warmup(self) -> None:
        return None

    async def ensure_ready(self, timeout: float) -> bool:
        return False

    async def embed(self, texts):
        self.embed_calls += 1
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]

    async def aclose(self) -> None:
        return None


async def test_unready_provider_degrades_without_embedding(tmp_path: Path) -> None:
    """预热没完成时直接降级, 不去调 embed —— 调了就是排在那次下载后面."""
    store = await _store(tmp_path)
    provider = _NotReadyProvider()
    service = _service(store, provider, vector_enabled=True)
    result = await service.search("人妻", limit=5)
    assert result.degraded
    assert result.mode is SearchMode.KEYWORD
    assert provider.embed_calls == 0
    await store.aclose()


async def test_python_engine_matches_vec0(tmp_path: Path) -> None:
    """两个向量引擎对同一查询给出同一顺序."""
    store = await _store(tmp_path)
    provider = StubProvider()
    vec0 = _service(store, provider, vector_enabled=True)
    py = _service(store, provider, vector_enabled=True, vector_engine=VectorEngine.PYTHON)
    first = await vec0.search("护士", limit=5)
    second = await py.search("护士", limit=5)
    assert first.ids == second.ids
    await store.aclose()


async def test_blank_query_returns_empty(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    service = _service(store, StubProvider(), vector_enabled=True)
    for query in ("", "   "):
        result = await service.search(query, limit=5)
        assert result.ids == []
    await store.aclose()


async def test_reopen_reuses_index(tmp_path: Path) -> None:
    """重开连接不重建也不丢数据."""
    store = await _store(tmp_path)
    await store.aclose()
    path = tmp_path / "amane.db"
    again = SearchStore(path, dim=DIM, model=MODEL)
    await again.open()
    assert (await again.stats()).keyword_docs == len(CORPUS)
    assert {hit.metadata_id for hit in await again.search_keyword("人妻", limit=10)} == {2}
    await again.aclose()
