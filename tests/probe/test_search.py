"""向量库自检: 开关、引擎、维度与索引规模的判据都只反映配置事实, 不依赖真模型."""

from __future__ import annotations

import pytest

from amane.config import EmbeddingConfig, EmbeddingProviderName, HotSettings
from amane.probe import search as probe_module
from amane.probe.base import ProbeLevel
from amane.probe.search import probe_search

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
