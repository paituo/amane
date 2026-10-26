from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..db.models import Library
    from ..enums import SearchField
    from ..search import SearchResult


class LibraryWatcher(Protocol):
    def add_library(
        self,
        path: str,
        library_id: int,
        recursive: bool = True,
        patterns: list[str] | None = None,
        skip_patterns: Sequence[str | None] | None = None,
        min_file_size: int = 0,
    ) -> None: ...

    def remove_library(self, library_id: int) -> None: ...

    def sync_library(self, lib: Library) -> None: ...


class SearchGateway(Protocol):
    """检索服务入口 (``search.SearchService``).

    经桥接取用而不是直接持有: 检索栈随 ``hot.search`` 热重建, 助理必须每次取当前实例,
    否则会一直用着被 ``aclose`` 掉的旧栈.
    """

    async def search(
        self,
        query: str,
        *,
        limit: int,
        fields: Sequence[SearchField] | None = None,
    ) -> SearchResult: ...


@dataclass
class AgentRuntimeBridge:
    safe_dirs: list[Path] | None = field(default_factory=list)
    watcher: LibraryWatcher | None = None
    cancel_running_task: Callable[[int], Awaitable[bool]] | None = None
    poll_feed: Callable[[int], Awaitable[None]] | None = None
    search: Callable[[], SearchGateway | None] | None = None
    """当前检索实例. 未装配或开关全关时为 None, 工具据此退回 SQL 并说明原因."""
