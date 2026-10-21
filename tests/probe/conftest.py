"""探针测试的公共夹具: 构造一个不依赖运行时的 ``ProbeContext``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from amane.config import HotSettings
from amane.probe import ProbeContext, Recorder

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def make_ctx(tmp_path: Path):
    """(hot, saved, runtime) → ProbeContext; 目录都落在 tmp_path, 探针不会写到别处."""

    def _make(hot: HotSettings | None = None, saved: HotSettings | None = None, runtime: object = None) -> ProbeContext:
        resolved = hot if hot is not None else HotSettings()
        return ProbeContext(
            hot=resolved,
            saved=saved if saved is not None else resolved,
            runtime=runtime,  # pyright: ignore[reportArgumentType] - 单测里只用到 search 属性
            data_dir=tmp_path,
            db_path=tmp_path / "amane.db",
            rec=Recorder(),
        )

    return _make
