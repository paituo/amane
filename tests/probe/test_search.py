"""向量库自检: 开关、引擎、维度与索引规模的判据都只反映配置事实; 真实检索段才碰真栈."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from amane.config import EmbeddingConfig, EmbeddingProviderName, HotSettings
from amane.enums import SearchMode
from amane.probe import search as probe_module
from amane.probe.base import ProbeLevel
from amane.probe.search import probe_search
from amane.search import SearchResult

DIM = 8


class _FakeProvider:
    """够用的假件: 有 embed 与 model_id, 刻意不实现 WarmableProvider (走"复用运行实例"分支)."""

    model_id = "fake:model"
    dim = DIM

    def __init__(self, out_dim: int = DIM) -> None:
        self._out_dim = out_dim
        self.closed = False

    async def embed(self, texts):
        return [[0.1] * self._out_dim for _ in texts]

    async def aclose(self) -> None:
        self.closed = True


def _vector_hot() -> HotSettings:
    hot = HotSettings()
    hot.search.vector_enabled = True
    hot.search.embedding.dim = DIM
    return hot


@pytest.mark.asyncio(loop_scope="function")
async def test_both_switches_off_warns_and_stops(make_ctx):
    hot = HotSettings()
    hot.search.keyword_enabled = False
    hot.search.vector_enabled = False

    events = [event async for event in probe_search(make_ctx(hot))]

    assert events[-1].level is ProbeLevel.WARN
    assert "LIKE" in (events[-1].detail or "")


@pytest.mark.asyncio(loop_scope="function")
async def test_missing_fastembed_is_an_error_not_a_silent_degrade(make_ctx, monkeypatch):
    monkeypatch.setattr(probe_module, "fastembed_available", lambda: False)

    events = [event async for event in probe_search(make_ctx(_vector_hot()))]

    assert events[-1].level is ProbeLevel.ERROR
    assert "fastembed" in events[-1].message


@pytest.mark.asyncio(loop_scope="function")
async def test_dimension_mismatch_is_caught_by_a_real_embedding(make_ctx, monkeypatch):
    provider = _FakeProvider(out_dim=DIM * 2)
    monkeypatch.setattr(probe_module, "_acquire_provider", lambda ctx: (provider, False))

    events = [event async for event in probe_search(make_ctx(_vector_hot()))]

    assert events[-1].level is ProbeLevel.ERROR
    assert f"配置 {DIM}" in events[-1].message
    assert str(DIM * 2) in events[-1].message


@pytest.mark.asyncio(loop_scope="function")
async def test_happy_path_reports_reuse_and_dimension(make_ctx, monkeypatch):
    provider = _FakeProvider()
    monkeypatch.setattr(probe_module, "_acquire_provider", lambda ctx: (provider, False))

    events = [event async for event in probe_search(make_ctx(_vector_hot()))]

    messages = [event.message for event in events]
    assert events[-1].level is ProbeLevel.OK
    assert any("复用" in message for message in messages)
    assert any(f"维度 {DIM}" in message and "一致" in message for message in messages)
    assert provider.closed is False, "复用来的 provider 不能被测试关掉"


@pytest.mark.asyncio(loop_scope="function")
async def test_owned_provider_is_closed_even_on_failure(make_ctx, monkeypatch):
    provider = _FakeProvider(out_dim=DIM * 3)
    monkeypatch.setattr(probe_module, "_acquire_provider", lambda ctx: (provider, True))

    events = [event async for event in probe_search(make_ctx(_vector_hot()))]

    assert events[-1].level is ProbeLevel.ERROR
    assert provider.closed is True


@pytest.mark.asyncio(loop_scope="function")
async def test_openai_compatible_rejects_non_http_base_url(make_ctx):
    hot = _vector_hot()
    hot.search.embedding = EmbeddingConfig(
        provider=EmbeddingProviderName.OPENAI_COMPATIBLE, base_url="127.0.0.1:11434", dim=DIM
    )

    events = [event async for event in probe_search(make_ctx(hot))]

    assert events[-1].level is ProbeLevel.ERROR
    assert "http(s)" in (events[-1].detail or "")


class _Item:
    def __init__(self, id_: int, number: str, title: str | None) -> None:
        self.id = id_
        self.number = number
        self.title = title


class _Repo:
    def __init__(self, items: list[_Item]) -> None:
        self._items = items

    async def list_metadata(self, *, ids: list[int] | None = None, limit: int = 20, **kwargs: object):
        items = self._items if ids is None else [item for item in self._items if item.id in ids]
        return items[:limit], len(items)


class _Service:
    def __init__(self, result: SearchResult) -> None:
        self.result = result
        self.query: str | None = None

    async def search(self, query: str, *, limit: int) -> SearchResult:
        self.query = query
        return self.result


def _runtime(result: SearchResult, items: list[_Item]) -> tuple[object, _Service]:
    service = _Service(result)
    return SimpleNamespace(search=SimpleNamespace(service=service), repo=_Repo(items)), service


@pytest.mark.asyncio(loop_scope="function")
async def test_real_retrieval_lists_hits_in_rank_order(make_ctx, monkeypatch):
    """配置判据全过后要真查一次: 查询词取库内标题, 命中按检索给出的顺序列出来."""
    provider = _FakeProvider()
    monkeypatch.setattr(probe_module, "_acquire_provider", lambda ctx: (provider, False))
    runtime, service = _runtime(
        SearchResult(mode=SearchMode.KEYWORD, ids=[7, 3], total=2),
        [_Item(7, "ABP-123", "テスト作品"), _Item(3, "SSIS-001", "別の作品")],
    )

    events = [event async for event in probe_search(make_ctx(_vector_hot(), None, runtime))]

    assert events[-1].level is ProbeLevel.OK
    assert "真实检索成功" in events[-1].message
    detail = events[-1].detail or ""
    assert "命中 2 条" in detail
    assert detail.index("ABP-123") < detail.index("SSIS-001"), "顺序要跟检索一致, 不能按库里的排序"
    assert service.query == "テスト作品", "查询词应取自库内真实标题"


@pytest.mark.asyncio(loop_scope="function")
async def test_real_retrieval_without_hits_warns_and_names_the_reason(make_ctx, monkeypatch):
    """没有命中是可见的 warn, 而且要说清是索引空还是查询词不对."""
    provider = _FakeProvider()
    monkeypatch.setattr(probe_module, "_acquire_provider", lambda ctx: (provider, False))
    runtime, _ = _runtime(SearchResult(mode=SearchMode.KEYWORD, ids=[], total=0), [])

    events = [event async for event in probe_search(make_ctx(_vector_hot(), None, runtime))]

    assert events[-1].level is ProbeLevel.WARN
    assert "真实检索没有命中" in events[-1].message
    assert "刮削" in (events[-1].detail or "")
