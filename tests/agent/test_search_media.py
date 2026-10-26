"""search_media 工具: 语义检索入口与保序交付.

检索本身由 ``SearchService`` 负责 (见 ``tests/search``), 这里只覆盖工具层: 命中如何呈现给
模型、开关未启用如何回执、交付的预设能否复现相关度顺序.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio
from pydantic_ai import ApprovalRequired, RunContext

from amane.agent.cache import ResultCache
from amane.agent.executor import QueryExecutor
from amane.agent.sql import ReadonlySqlSandbox, SqlResult
from amane.agent.tools import (
    APPROVED_SQL_TIMEOUT_MS,
    SEARCH_LIMIT_MAX,
    AgentDeps,
    build_explore_toolset,
    identifier_hint,
    sql_failure_receipt,
)
from amane.db.models import SavedQueryEntity
from amane.db.repository import Repository
from amane.enums import SearchMode
from amane.search import SearchResult


class StubSearch:
    """检索桩: 记录入参, 返回固定命中."""

    def __init__(
        self,
        ids: list[int],
        *,
        mode: SearchMode = SearchMode.HYBRID,
        degraded: bool = False,
    ) -> None:
        self.ids = ids
        self.mode = mode
        self.degraded = degraded
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, *, limit: int) -> SearchResult:
        self.calls.append((query, limit))
        return SearchResult(mode=self.mode, ids=self.ids[:limit], total=len(self.ids), degraded=self.degraded)


def _search_fn() -> Callable[..., Awaitable[dict[str, Any]]]:
    toolset = build_explore_toolset()
    return cast(Callable[..., Awaitable[dict[str, Any]]], toolset.tools["search_media"].function)


def _tool_fn(name: str) -> Callable[..., Awaitable[dict[str, Any]]]:
    toolset = build_explore_toolset()
    return cast(Callable[..., Awaitable[dict[str, Any]]], toolset.tools[name].function)


def _ctx(deps: AgentDeps) -> RunContext[AgentDeps]:
    return cast(RunContext[AgentDeps], SimpleNamespace(deps=deps, tool_call_approved=False, tool_call_id="tc-test"))


async def _seed_sandbox(db: Path) -> None:
    """沙箱库只需要 id / number / title: 交付 SQL 只查这三列."""
    async with aiosqlite.connect(db) as conn:
        await conn.execute("CREATE TABLE metadata (id INTEGER PRIMARY KEY, number TEXT, title TEXT, plot TEXT)")
        await conn.executemany(
            "INSERT INTO metadata (id, number, title, plot) VALUES (?, ?, ?, ?)",
            [(1, "AAA-001", "t1", "p1"), (2, "BBB-002", "t2", "p2"), (3, "CCC-003", "t3", "p3")],
        )
        await conn.commit()


@pytest_asyncio.fixture
async def env(tmp_path: Path, repo: Repository) -> tuple[AgentDeps, StubSearch]:
    db = tmp_path / "search.db"
    await _seed_sandbox(db)
    for number, title in (("AAA-001", "t1"), ("BBB-002", "t2"), ("CCC-003", "t3")):
        await repo.upsert_metadata(number=number, title=title)
    session = await repo.create_agent_session(title="search")
    assert session.id is not None
    stub = StubSearch([3, 1, 2])
    deps = AgentDeps(
        repo=repo,
        executor=QueryExecutor(ReadonlySqlSandbox(db), ResultCache(ttl_s=60, max_entries=8)),
        session_id=session.id,
        sql_timeout_ms=2000,
    )
    deps.bridge.search = lambda: stub
    return deps, stub


@pytest.mark.asyncio
async def test_search_media_returns_ranked_hits(env: tuple[AgentDeps, StubSearch]) -> None:
    """命中按相关度给出, 并带番号与标题供直接作答."""
    deps, _stub = env
    out = await _search_fn()(_ctx(deps), query="离家出走的少女")

    assert out["ids"] == [3, 1, 2]
    assert [hit["id"] for hit in out["hits"]] == [3, 1, 2]
    assert [hit["number"] for hit in out["hits"]] == ["CCC-003", "AAA-001", "BBB-002"]
    assert out["mode"] == "hybrid"
    assert out["degraded"] is False
    assert out["saved_query_id"] is None


@pytest.mark.asyncio
async def test_search_media_surfaces_degraded_keyword_only(env: tuple[AgentDeps, StubSearch]) -> None:
    """语义路不可用时必须让模型知道结果是关键词的."""
    deps, stub = env
    stub.mode = SearchMode.KEYWORD
    stub.degraded = True
    out = await _search_fn()(_ctx(deps), query="离家出走")

    assert out["mode"] == "keyword"
    assert out["degraded"] is True


@pytest.mark.asyncio
async def test_search_media_without_stack_explains_fallback(env: tuple[AgentDeps, StubSearch]) -> None:
    """未装配检索栈: 回执须点明原因, 不能回空命中让模型以为库里没有."""
    deps, _stub = env
    deps.bridge.search = None
    out = await _search_fn()(_ctx(deps), query="离家出走")

    assert "error" in out
    assert "检索未启用" in out["error"]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (0, 1),
        (-5, 1),
        (999, SEARCH_LIMIT_MAX),
        (7, 7),
    ],
)
@pytest.mark.asyncio
async def test_search_media_clamps_limit(env: tuple[AgentDeps, StubSearch], given: int, expected: int) -> None:
    deps, stub = env
    await _search_fn()(_ctx(deps), query="x", limit=given)

    assert stub.calls[-1][1] == expected


@pytest.mark.asyncio
async def test_search_media_rejects_blank_query(env: tuple[AgentDeps, StubSearch]) -> None:
    deps, stub = env
    out = await _search_fn()(_ctx(deps), query="   ")

    assert "error" in out
    assert stub.calls == []


@pytest.mark.asyncio
async def test_search_media_delivers_preset_in_relevance_order(env: tuple[AgentDeps, StubSearch]) -> None:
    """交付的预设是 SQL, 重跑必须复现相关度顺序 —— 否则最像的那条会沉底."""
    deps, _stub = env
    out = await _search_fn()(_ctx(deps), query="离家出走", name="离家主题")

    query = await deps.repo.get_saved_query(out["saved_query_id"])
    assert query is not None
    assert query.entity == SavedQueryEntity.METADATA
    assert query.name == "离家主题"
    assert [row[0] for row in (await deps.executor.ensure_cached(query, timeout_ms=deps.sql_timeout_ms)).rows] == [
        3,
        1,
        2,
    ]
    # 缓存失效后按 SQL 重跑, 顺序仍须一致
    deps.executor.cache.invalidate(out["saved_query_id"])
    again = await deps.executor.ensure_cached(query, timeout_ms=deps.sql_timeout_ms)
    assert [row[0] for row in again.rows] == [3, 1, 2]


@pytest.mark.asyncio
async def test_search_media_without_hit_skips_preset(env: tuple[AgentDeps, StubSearch]) -> None:
    """无命中时不建空预设."""
    deps, stub = env
    stub.ids = []
    out = await _search_fn()(_ctx(deps), query="不存在的剧情", name="空")

    assert "error" in out
    assert "无命中" in out["error"]


@pytest.mark.parametrize(
    ("sql", "search_available", "expects_advice"),
    [
        ("SELECT id FROM metadata WHERE LOWER(title) LIKE '%辅导%'", True, True),
        ("SELECT id FROM metadata WHERE number LIKE 'SSIS%'", True, False),
        ("SELECT id FROM metadata WHERE title = 'x'", True, False),
        # 检索栈缺席时全表扫描是唯一手段, 不能堵死 —— 只回原始原因
        ("SELECT id FROM metadata WHERE LOWER(title) LIKE '%辅导%'", False, False),
    ],
)
def test_sql_failure_receipt_only_redirects_when_search_exists(
    sql: str, search_available: bool, expects_advice: bool
) -> None:
    """子串扫描失败的回执: 检索可用才指向 search_media, 否则保留原路径."""
    out = sql_failure_receipt(sql, "查询超时 (2096ms > 1000ms)", search_available=search_available)

    assert ("search_media" in out["error"]) is expects_advice
    assert out["error"].startswith("查询超时")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # 2026-10-26 00:49 的真实失败: 模型把剧情列写成 description
        ("no such column: description", "plot"),
        ("no such column: metadata.overview", "actors"),
        ("no such column: synopsis", "plot"),
        # 普通列: 只说它属于哪张表
        ("no such column: body", "comments"),
        # 完全不存在: 指向 schema 摘要, 不再让模型猜
        ("no such column: whatever_field", "schema"),
        # 检索栈内部表被猜中时直接换工具
        ("no such table: metadata_fts", "search_media"),
        # 与纠错无关的报错不硬凑提示
        ('near "SELEC": syntax error', None),
    ],
)
def test_identifier_hint_corrects_only_known_identifiers(message: str, expected: str | None) -> None:
    """列名/表名写错是模型自己的问题, 回执要能一轮改对, 而不是让它对着 SQLite 原文再猜."""
    hint = identifier_hint(message)

    if expected is None:
        assert hint is None
    else:
        assert hint is not None
        assert expected in hint


@pytest.mark.asyncio
async def test_unknown_column_receipt_is_actionable(env: tuple[AgentDeps, StubSearch]) -> None:
    """真实沙箱路径: no such column 的回执要同时给出真实列名与换工具指引.

    SQL 取自线上日志 —— 语义查询被写成 description LIKE 全表扫描, 既撞未知列又注定超时.
    """
    deps, _stub = env
    out = await _tool_fn("sql_explore")(
        _ctx(deps),
        sql=(
            "SELECT id, number, title, description FROM metadata "
            "WHERE description LIKE '%离家%' OR description LIKE '%辅导%功课%'"
        ),
    )

    assert "no such column" in out["error"]
    assert "plot" in out["error"]
    assert "search_media" in out["error"]


@pytest.mark.asyncio
async def test_substring_scan_timeout_points_at_search_media(env: tuple[AgentDeps, StubSearch]) -> None:
    """真实超时路径: 模型拿到的是"换工具"而不是"再试一次"."""
    deps, _stub = env
    deps.sql_timeout_ms = 1  # 逼出真实超时, 而不是靠列名错误之类旁因
    out = await _tool_fn("sql_explore")(
        _ctx(deps),
        sql="SELECT id, number, title, plot FROM metadata WHERE LOWER(title) LIKE '%辅导%'",
    )

    assert "查询超时" in out["error"]
    assert "search_media" in out["error"]


@pytest.mark.asyncio
async def test_substring_scan_allow_slow_skips_approval(env: tuple[AgentDeps, StubSearch]) -> None:
    """检索可用时全表 LIKE 不进批准流: 让用户批一个必然低质的慢扫描没有意义."""
    deps, _stub = env
    out = await _tool_fn("sql_explore")(
        _ctx(deps), sql="SELECT id FROM metadata WHERE title LIKE '%辅导%'", allow_slow=True
    )

    assert "search_media" in out["error"]


@pytest.mark.asyncio
async def test_substring_scan_without_search_still_asks_approval(env: tuple[AgentDeps, StubSearch]) -> None:
    """检索栈缺席时全表扫描是唯一手段, 批准流照旧."""
    deps, _stub = env
    deps.bridge.search = None
    with pytest.raises(ApprovalRequired):
        await _tool_fn("sql_explore")(
            _ctx(deps), sql="SELECT id FROM metadata WHERE title LIKE '%辅导%'", allow_slow=True
        )


@pytest.mark.parametrize(
    ("allow_slow", "expected"),
    [
        (False, 2000),
        (True, APPROVED_SQL_TIMEOUT_MS),
    ],
)
@pytest.mark.asyncio
async def test_timeout_for_only_widens_approved_queries(
    env: tuple[AgentDeps, StubSearch], allow_slow: bool, expected: int
) -> None:
    """批准流必须真的放宽预算: 否则"批准"只是白点一次, 慢查询照样在同一个上限处被打断."""
    deps, _stub = env

    assert deps.timeout_for(allow_slow=allow_slow) == expected


@pytest.mark.asyncio
async def test_sql_explore_uses_approved_budget_after_approval(env: tuple[AgentDeps, StubSearch]) -> None:
    """已批准的慢查询实际下发的超时是放宽值, 不是默认值."""
    deps, _stub = env
    result = SqlResult(columns=["id"], rows=[[1]], row_count=1, elapsed_ms=1.0)
    run_sql = AsyncMock(return_value=result)
    deps.executor.run_sql = run_sql  # type: ignore[method-assign]
    approved_ctx = cast(
        RunContext[AgentDeps],
        SimpleNamespace(deps=deps, tool_call_approved=True, tool_call_id="tc-test"),
    )

    await _tool_fn("sql_explore")(approved_ctx, sql="SELECT id FROM metadata", allow_slow=True)

    call = run_sql.call_args
    assert call is not None
    assert call.kwargs["timeout_ms"] == APPROVED_SQL_TIMEOUT_MS
