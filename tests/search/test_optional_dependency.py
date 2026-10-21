"""可选依赖缺席时的降级: 内嵌嵌入来自 extra ``vector``, 不含它的分发仍须可用.

判据不是"能不能搜到", 而是**进程能否起来**: 一个可以降级的能力不该让模块导入或启动失败.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from amane.app.runtime import build_embedding_provider
from amane.config.manager import EmbeddingConfig, EmbeddingProviderName, HotSettings, SearchConfig
from amane.search import EmbeddingError, FastEmbedProvider, OpenAICompatibleProvider
from amane.search import embedding as embedding_module

DIM = 8


def _hot(*, provider: EmbeddingProviderName = EmbeddingProviderName.FAST_EMBED) -> HotSettings:
    """向量路开着的最小配置: 覆盖"配了但实现缺席"这条路径."""
    return HotSettings(
        search=SearchConfig(
            vector_enabled=True,
            embedding=EmbeddingConfig(provider=provider, dim=DIM),
        )
    )


def test_missing_fastembed_raises_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """直接构造 provider 时也要给出可执行的下一步, 而不是一句 ImportError."""
    monkeypatch.setattr(embedding_module, "_engine_cls", None)
    provider = FastEmbedProvider(model="stub", dim=DIM)
    with pytest.raises(EmbeddingError, match=r"amane\[vector\]"):
        provider._ensure_engine()  # pyright: ignore[reportPrivateUsage]


def test_provider_is_absent_when_fastembed_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """组合根不构造 provider: 搜索栈据此标 degraded, 而不是让启动失败."""
    monkeypatch.setattr(embedding_module, "_engine_cls", None)
    assert build_embedding_provider(_hot()) is None
    # HTTP 实现不依赖 fastembed, 缺失时照常构造.
    assert isinstance(
        build_embedding_provider(_hot(provider=EmbeddingProviderName.OPENAI_COMPATIBLE)),
        OpenAICompatibleProvider,
    )


def test_import_chain_does_not_require_fastembed() -> None:
    """API 与检索包必须能在没有 fastembed 的解释器里导入.

    子进程里把 fastembed 置为不可导入, 再导入路由: 模块级导入若仍是硬的, 这里会直接失败.
    ``search`` 包内的 provider 类仍可取用, 但不能在 import 阶段解引用 fastembed.
    """
    code = (
        "import sys\n"
        "sys.modules['fastembed'] = None\n"
        "from amane.api.routes.metadata import router\n"
        "from amane.enums import SearchMode\n"
        "from amane.search import fastembed_available\n"
        "assert router.prefix == '/metadata'\n"
        "assert not fastembed_available()\n"
        "assert SearchMode.LIKE == 'like'\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
