"""预热: 模型加载发生在配置启用时, 而不是首个请求上.

判据四条:
1. 构造 provider 不加载模型;
2. ``warmup()`` 加载一次, 之后的 embed 复用同一个引擎;
3. 加载未完成时 ``ensure_ready`` 有界返回 False, 任务继续跑完;
4. 加载失败不抛给调用方, 且下一次 embed 能自愈.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from amane.app.runtime import SearchStack
from amane.search import EmbeddingProvider, FastEmbedProvider
from amane.search.embedding import OpenAICompatibleProvider

pytestmark = pytest.mark.asyncio

DIM = 8
MODEL = "fake-model"


class _Engine:
    """替身推理引擎, 记录被构造了几次."""

    instances = 0
    vectors = 1

    def __init__(self, *, model_name: str, cache_dir: str | None = None, threads: int | None = None) -> None:
        # 计数挂在基类上: 子类 (_SlowEngine 等) 也要算进同一个计数.
        _Engine.instances += 1
        self.model_name = model_name

    def embed(self, texts):
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]


class _SlowEngine(_Engine):
    """构造时阻塞, 用来模拟下载耗时."""

    delay = 0.3

    def __init__(self, **kwargs) -> None:
        time.sleep(self.delay)
        super().__init__(**kwargs)


class _BrokenEngine(_Engine):
    def __init__(self, **kwargs) -> None:
        raise RuntimeError("权重下载失败")


@pytest.fixture(autouse=True)
def _reset_engine_count() -> None:
    _Engine.instances = 0


def _patch_engine(monkeypatch: pytest.MonkeyPatch, engine: type[_Engine]) -> None:
    monkeypatch.setattr("amane.search.embedding._engine_cls", engine)


async def test_warmup_loads_once_and_embed_reuses_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_engine(monkeypatch, _Engine)
    provider = FastEmbedProvider(model=MODEL, dim=DIM, cache_dir=tmp_path)

    # 构造不加载: 开关刚被打开时不该先付一次同步加载.
    assert provider.ready is False
    assert _Engine.instances == 0

    await provider.warmup()

    assert provider.ready is True
    assert _Engine.instances == 1
    await provider.embed(["人妻"])
    assert _Engine.instances == 1, "embed 不能重复构造引擎"


async def test_ensure_ready_is_bounded_but_load_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_engine(monkeypatch, _SlowEngine)
    provider = FastEmbedProvider(model=MODEL, dim=DIM, cache_dir=tmp_path)

    assert await provider.ensure_ready(0.02) is False
    assert provider.ready is False

    # 加载任务没有被超时打断: 第二次等它就能就绪.
    assert await provider.ensure_ready(5.0) is True
    assert provider.ready is True


async def test_warmup_and_embed_share_one_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_engine(monkeypatch, _SlowEngine)
    provider = FastEmbedProvider(model=MODEL, dim=DIM, cache_dir=tmp_path)

    await asyncio.gather(provider.warmup(), provider.embed(["护士"]), provider.embed(["人妻"]))

    assert provider.ready is True
    assert _Engine.instances == 1


async def test_failed_load_does_not_raise_and_retries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_engine(monkeypatch, _BrokenEngine)
    provider = FastEmbedProvider(model=MODEL, dim=DIM, cache_dir=tmp_path)

    # 预热失败只记日志: 配置启用这个动作不该因为下载失败而报错.
    await provider.warmup()
    assert provider.ready is False
    assert await provider.ensure_ready(0.1) is False

    # 自愈: 条件恢复后下一次 embed 重新加载成功.
    _patch_engine(monkeypatch, _Engine)
    vectors = await provider.embed(["人妻"])
    assert provider.ready is True
    assert vectors and len(vectors[0]) == DIM


async def test_openai_warmup_marks_ready_and_swallows_failure() -> None:
    provider = OpenAICompatibleProvider(base_url="http://localhost:9/v1", model=MODEL, dim=DIM)

    async def _ok(batch: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (DIM - 1) for _ in batch]

    async def _broken(batch: list[str]) -> list[list[float]]:
        raise RuntimeError("connect refused")

    provider._embed_batch = _ok  # type: ignore[method-assign]
    await provider.warmup()
    assert provider.ready is True
    await provider.aclose()

    failing = OpenAICompatibleProvider(base_url="http://localhost:9/v1", model=MODEL, dim=DIM)
    failing._embed_batch = _broken  # type: ignore[method-assign]
    await failing.warmup()
    assert failing.ready is False
    await failing.aclose()


class _RecordingProvider:
    """只记录 warmup 是否被调用."""

    model_id = MODEL
    dim = DIM

    def __init__(self) -> None:
        self.warmed = False

    @property
    def ready(self) -> bool:
        return self.warmed

    async def warmup(self) -> None:
        self.warmed = True

    async def ensure_ready(self, timeout: float) -> bool:
        return self.warmed

    async def embed(self, texts):
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]

    async def aclose(self) -> None:
        return None


def _bare_stack(provider: EmbeddingProvider | None) -> SearchStack:
    """只走预热这一条路径, store/service/indexer 不参与."""
    stack = SearchStack.__new__(SearchStack)
    stack.provider = provider
    stack._warm_task = None
    return stack


async def test_stack_warmup_starts_task_at_enable_time() -> None:
    provider = _RecordingProvider()
    stack = _bare_stack(provider)

    stack.start_warmup()
    task = stack._warm_task
    assert task is not None
    await task
    assert provider.warmed is True


async def test_stack_warmup_is_noop_without_warmable_provider() -> None:
    stack = _bare_stack(None)
    stack.start_warmup()
    assert stack._warm_task is None
