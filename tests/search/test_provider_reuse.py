"""改检索配置时的 provider 接管: 嵌入子配置没变就不该重新加载模型.

判据三条:
1. ``detach_provider`` 把 provider 与预热任务整体交出, 旧栈 ``aclose`` 之后不再关它;
2. ``build_search(reuse=...)`` 直接用接管来的实例, 不构造新 provider;
3. 只有 ``embedding`` 子配置逐字段相等、且向量路仍启用时才允许接管.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amane.app.runtime import AppRuntime, ReusedEmbedding, SearchStack, build_search
from amane.config import EmbeddingConfig, HotSettings, SearchConfig

pytestmark = pytest.mark.asyncio

DIM = 8


class _Closer:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _Provider:
    model_id = "fake:model"
    dim = DIM

    def __init__(self) -> None:
        self.closed = False

    async def embed(self, texts):
        return [[0.1] * DIM for _ in texts]

    async def aclose(self) -> None:
        self.closed = True


def _stack(provider: _Provider | None, embedding: EmbeddingConfig) -> SearchStack:
    stack = SearchStack.__new__(SearchStack)
    stack.provider = provider
    stack.embedding_key = embedding
    stack._warm_task = None
    stack.indexer = _Closer()  # pyright: ignore[reportAttributeAccessIssue]
    stack.store = _Closer()  # pyright: ignore[reportAttributeAccessIssue]
    return stack


def _hot(dim: int = DIM) -> HotSettings:
    hot = HotSettings()
    hot.search.vector_enabled = True
    hot.search.embedding = EmbeddingConfig(dim=dim)
    return hot


async def test_detach_hands_over_provider_and_skips_close() -> None:
    provider = _Provider()
    stack = _stack(provider, EmbeddingConfig(dim=DIM))
    task = object()
    stack._warm_task = task  # pyright: ignore[reportAttributeAccessIssue]

    reused = stack.detach_provider()

    assert isinstance(reused, ReusedEmbedding)
    assert reused.provider is provider
    assert reused.warm_task is task
    assert stack.provider is None
    assert stack._warm_task is None

    await stack.aclose()
    assert provider.closed is False, "接管走的 provider 不能由旧栈关掉"


async def test_detach_without_provider_returns_none() -> None:
    stack = _stack(None, EmbeddingConfig(dim=DIM))
    assert stack.detach_provider() is None


async def test_build_search_reuses_provider_instead_of_building(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args, **kwargs):
        raise AssertionError("已有可接管的 provider 时不该重新构造")

    monkeypatch.setattr("amane.app.runtime.build_embedding_provider", _boom)
    provider = _Provider()
    task = object()

    stack = build_search(
        _hot(),
        None,  # pyright: ignore[reportArgumentType] - 构造期不用 repo
        tmp_path / "amane.db",
        tmp_path,
        reuse=ReusedEmbedding(provider=provider, warm_task=task),  # pyright: ignore[reportArgumentType]
    )

    assert stack is not None
    assert stack.provider is provider
    assert stack._warm_task is task
    assert stack.embedding_key == EmbeddingConfig(dim=DIM)


async def test_take_reusable_requires_identical_embedding_config() -> None:
    runtime = AppRuntime.__new__(AppRuntime)
    provider = _Provider()
    runtime.search = _stack(provider, EmbeddingConfig(dim=DIM))

    same = SearchConfig(vector_enabled=True, embedding=EmbeddingConfig(dim=DIM))
    taken = runtime._take_reusable_embedding(same)
    assert taken is not None and taken.provider is provider

    # 已接管, 第二次就没有了
    assert runtime._take_reusable_embedding(same) is None


async def test_take_reusable_rejects_changed_embedding() -> None:
    runtime = AppRuntime.__new__(AppRuntime)
    runtime.search = _stack(_Provider(), EmbeddingConfig(dim=DIM))

    changed_model = SearchConfig(vector_enabled=True, embedding=EmbeddingConfig(dim=DIM, model="other"))
    assert runtime._take_reusable_embedding(changed_model) is None

    changed_dim = SearchConfig(vector_enabled=True, embedding=EmbeddingConfig(dim=DIM * 2))
    assert runtime._take_reusable_embedding(changed_dim) is None


async def test_take_reusable_rejects_when_vector_disabled() -> None:
    runtime = AppRuntime.__new__(AppRuntime)
    runtime.search = _stack(_Provider(), EmbeddingConfig(dim=DIM))

    disabled = SearchConfig(vector_enabled=False, keyword_enabled=True, embedding=EmbeddingConfig(dim=DIM))
    assert runtime._take_reusable_embedding(disabled) is None


async def test_take_reusable_without_stack_returns_none() -> None:
    runtime = AppRuntime.__new__(AppRuntime)
    runtime.search = None
    assert runtime._take_reusable_embedding(SearchConfig(vector_enabled=True)) is None
