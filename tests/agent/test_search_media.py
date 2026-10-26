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
    SEARCH_CALL_BUDGET,
    SEARCH_DEPTH,
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
        scores: list[float] | None = None,
    ) -> None:
        self.ids = ids
        self.mode = mode
        self.degraded = degraded
        self.scores = scores or []
        self.calls: list[tuple[str, int]] = []
        self.field_args: list[list[str] | None] = []

    async def search(self, query: str, *, limit: int, fields: Any = None) -> SearchResult:
        self.calls.append((query, limit))
        self.field_args.append(None if fields is None else [getattr(field, "value", field) for field in fields])
        ids = self.ids[:limit]
        scores = self.scores[: len(ids)] if self.scores else []
        return SearchResult(
            mode=self.mode,
            ids=ids,
            scores=scores,
            total=len(self.ids),
            degraded=self.degraded,
        )


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
    for number, title, plot in (
        ("AAA-001", "t1", "离家出走的少女被带回家，班主任每晚辅导她功课。"),
        ("BBB-002", "t2", "人妻与学生的重逢。"),
        ("CCC-003", "t3", "为了考上大学，母女请家庭教师来家里补习。"),
    ):
        await repo.upsert_metadata(
            number=number,
            title=title,
            plot=plot,
            actors=[f"演员{number[:3]}"],
            tags=["家庭教师", "学生"],
            series="补习系列",
            studio="厂牌A",
            release="2024-01-01",
            runtime=120,
        )
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
async def test_search_media_hits_carry_judgement_fields(env: tuple[AgentDeps, StubSearch]) -> None:
    """命中必须带剧情/演员/标签: 只有标题时模型无从判断相关性, 只能猜片名或换措辞重搜."""
    deps, _stub = env
    out = await _search_fn()(_ctx(deps), query="离家出走")
    hit = out["hits"][0]

    assert "考上大学" in hit["plot"]
    assert hit["actors"] == ["演员CCC"]
    assert hit["tags"] == ["家庭教师", "学生"]
    assert hit["series"] == "补习系列"
    assert hit["studio"] == "厂牌A"
    assert hit["release"] == "2024-01-01"
    assert hit["runtime"] == 120
    assert hit["content_type"]
    # 空字段是 null 而不是空串, 模型看到 null 才知道"库里没填"
    assert out["hits"][0]["title"] == "t3"


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
    """limit 夹在 [1, SEARCH_LIMIT_MAX]; 检索深度本身固定, 不随 limit 变形."""
    deps, stub = env
    stub.ids = list(range(1, 61))
    out = await _search_fn()(_ctx(deps), query="x", limit=given)

    assert len(out["ids"]) == expected
    assert stub.calls[-1][1] == SEARCH_DEPTH


@pytest.mark.asyncio
async def test_search_media_pages_through_ranking(env: tuple[AgentDeps, StubSearch]) -> None:
    """total 大于一页时必须给出 has_more / next_offset, 且各页不重叠、拼起来等于全序."""
    deps, stub = env
    stub.ids = list(range(1, 26))  # 25 条, 只有前 3 条在 repo 里
    first = await _search_fn()(_ctx(deps), query="x", limit=10)

    assert first["total"] == 25
    assert first["has_more"] is True
    assert first["next_offset"] == 10
    assert first["offset"] == 0
    assert first["ids"] == list(range(1, 11))

    second = await _search_fn()(_ctx(deps), query="x", limit=10, offset=first["next_offset"])
    assert second["offset"] == 10
    assert second["next_offset"] == 20
    assert second["ids"] == list(range(11, 21))
    assert not set(first["ids"]) & set(second["ids"])

    last = await _search_fn()(_ctx(deps), query="x", limit=10, offset=second["next_offset"])
    assert last["ids"] == [21, 22, 23, 24, 25]
    assert last["has_more"] is False
    assert last["next_offset"] is None


@pytest.mark.asyncio
async def test_search_media_paging_keeps_same_ranking(env: tuple[AgentDeps, StubSearch]) -> None:
    """翻页必须复用同一个排序深度: 请求不同 limit 若换来不同候选集, 页与页会漏项."""
    deps, stub = env
    stub.ids = list(range(1, 41))
    await _search_fn()(_ctx(deps), query="x", limit=5)
    await _search_fn()(_ctx(deps), query="x", limit=30, offset=5)

    assert {depth for _query, depth in stub.calls} == {SEARCH_DEPTH}


@pytest.mark.asyncio
async def test_search_media_offset_beyond_ranking_is_empty(env: tuple[AgentDeps, StubSearch]) -> None:
    """越界页返回空, 不抛错也不回退到第一页."""
    deps, _stub = env
    out = await _search_fn()(_ctx(deps), query="x", offset=40)

    assert out["ids"] == []
    assert out["hits"] == []
    assert out["has_more"] is False


@pytest.mark.asyncio
async def test_search_media_multi_facet_fuses_and_reports_traces(env: tuple[AgentDeps, StubSearch]) -> None:
    """多分面各自检索再融合: 同时命中两个分面的条目排在只命中一个的前面."""
    deps, stub = env
    stub.ids = [3, 1, 2]

    async def per_query(query: str, *, limit: int, fields: Any = None) -> SearchResult:
        stub.calls.append((query, limit))
        table = {"离家出走": [1, 2, 3], "考上大学": [3, 2]}
        return SearchResult(mode=SearchMode.HYBRID, ids=table[query][:limit], total=len(table[query]), degraded=False)

    stub.search = per_query  # type: ignore[method-assign]
    out = await _search_fn()(_ctx(deps), queries=["离家出走", "考上大学"])

    assert out["ids"][0] == 3  # 两路都第 1
    assert [hit["matched_queries"] for hit in out["hits"]] == [2, 2, 1]
    assert [(f["query"], f["hits"]) for f in out["queries"]] == [("离家出走", 3), ("考上大学", 2)]


@pytest.mark.asyncio
async def test_search_media_single_facet_from_query(env: tuple[AgentDeps, StubSearch]) -> None:
    """只传 query 时等价于单分面, 回显一个 trace."""
    deps, _stub = env
    out = await _search_fn()(_ctx(deps), query="离家出走")

    assert [f["query"] for f in out["queries"]] == ["离家出走"]
    assert out["hits"][0]["matched_queries"] == 1


@pytest.mark.asyncio
async def test_search_media_mixed_modes_report_hybrid(env: tuple[AgentDeps, StubSearch]) -> None:
    """各分面走的路径不同时模式报 hybrid, 单路名说不全."""
    deps, stub = env

    async def per_query(query: str, *, limit: int, fields: Any = None) -> SearchResult:
        stub.calls.append((query, limit))
        mode = SearchMode.VECTOR if "考上" in query else SearchMode.KEYWORD
        return SearchResult(mode=mode, ids=[1], total=1, degraded=False)

    stub.search = per_query  # type: ignore[method-assign]
    out = await _search_fn()(_ctx(deps), queries=["离家出走", "考上大学"])

    assert out["mode"] == "hybrid"


@pytest.mark.asyncio
async def test_search_media_rare_facet_wins_over_broad_facet(env: tuple[AgentDeps, StubSearch]) -> None:
    """稀有分面的第 1 名必须排在宽泛分面的弱命中之前.

    真实反例: ["离家出走"(50 条), "辅导功课"(4), "考上大学"(5)] 未加权时, 正确条目 SCOP-193
    是"考上大学"第 1 名, 却被 20 条"离家出走"挤到页外, 模型因此答"库里没有".
    """
    deps, stub = env
    broad = list(range(100, 150))  # 50 条, 全是泛命中
    rare = [5558, *range(200, 204)]  # 5 条, 精确分面

    async def per_query(query: str, *, limit: int, fields: Any = None) -> SearchResult:
        stub.calls.append((query, limit))
        ids = broad if query == "离家出走" else rare
        return SearchResult(mode=SearchMode.HYBRID, ids=ids[:limit], total=len(ids), degraded=False)

    stub.search = per_query  # type: ignore[method-assign]
    out = await _search_fn()(_ctx(deps), queries=["离家出走", "考上大学"], limit=10)

    assert out["ids"][0] == 5558


@pytest.mark.asyncio
async def test_search_media_rejects_too_many_queries(env: tuple[AgentDeps, StubSearch]) -> None:
    """查询串超限直接说清楚而不是静默截断: 少查一个串会让 matched_queries 失真."""
    deps, stub = env
    out = await _search_fn()(_ctx(deps), queries=["a", "b", "c", "d", "e", "f", "g"])

    assert "error" in out
    assert "查询串过多" in out["error"]
    assert stub.calls == []


@pytest.mark.asyncio
async def test_search_media_dedupes_queries(env: tuple[AgentDeps, StubSearch]) -> None:
    """重复串只查一次, 否则同一路投票两次会人为抬高它的权重."""
    deps, stub = env
    out = await _search_fn()(_ctx(deps), query="离家出走", queries=["离家出走", "考上大学"])

    assert [q for q, _depth in stub.calls] == ["离家出走", "考上大学"]
    assert [f["query"] for f in out["queries"]] == ["离家出走", "考上大学"]


@pytest.mark.asyncio
async def test_search_media_repeated_query_returns_cumulative_page(env: tuple[AgentDeps, StubSearch]) -> None:
    """重搜同一串不再报错, 而是把**累积排序**还给它.

    报错等于让模型空转一轮; 累积页能直接支撑作答, 同时 ``note`` 说明"这串搜过了".
    """
    deps, stub = env
    first = await _search_fn()(_ctx(deps), query="离家出走")
    out = await _search_fn()(_ctx(deps), query="离家 出走")

    assert "error" not in out
    assert out["note"] and "已检索过" in out["note"]
    assert out["ids"] == first["ids"]
    assert len(stub.calls) == 1


@pytest.mark.asyncio
async def test_search_media_second_call_fuses_with_first(env: tuple[AgentDeps, StubSearch]) -> None:
    """运行时多串混合查询: 第二次调用必须把第一次的串一起参与融合.

    模型一次只能发一个 tool_call, 若每轮只回本串结果, 它永远看不到"同时满足两个条件"的条目.
    """
    deps, stub = env

    async def per_query(query: str, *, limit: int, fields: Any = None) -> SearchResult:
        stub.calls.append((query, limit))
        table = {"离家出走": [1, 2, 3], "考上大学": [3, 2]}
        return SearchResult(mode=SearchMode.HYBRID, ids=table[query][:limit], total=len(table[query]), degraded=False)

    stub.search = per_query  # type: ignore[method-assign]
    await _search_fn()(_ctx(deps), query="离家出走")
    out = await _search_fn()(_ctx(deps), query="考上大学")

    assert out["ids"][0] == 3
    assert [hit["matched_queries"] for hit in out["hits"]] == [2, 2, 1]
    assert out["searched"] == ["离家出走", "考上大学"]


@pytest.mark.asyncio
async def test_search_media_allows_paging_same_query(env: tuple[AgentDeps, StubSearch]) -> None:
    """翻页在已有排序上切片, 不重跑检索.

    旧实现让 offset>0 豁免判重, 同一个串被再检索一次又进累积 —— 重复投票扰动整份排名.
    检索是幂等的, 重跑只会把页间顺序搞乱, 所以第二页必须直接切累积排序.
    """
    deps, stub = env
    stub.ids = list(range(1, 31))
    await _search_fn()(_ctx(deps), query="离家出走", limit=5)
    out = await _search_fn()(_ctx(deps), query="离家出走", limit=5, offset=5)

    assert "error" not in out
    assert out["ids"] == [6, 7, 8, 9, 10]
    assert len(stub.calls) == 1
    assert "已检索过" in (out["note"] or "")


@pytest.mark.asyncio
async def test_search_media_passes_fields_to_keyword_path(env: tuple[AgentDeps, StubSearch]) -> None:
    """字段限定必须真下发到检索栈, 并在回执回显 —— 模型要能确认它确实生效了."""
    deps, stub = env
    out = await _search_fn()(_ctx(deps), query="离家出走", fields=["title", "plot"])

    assert stub.field_args[-1] == ["title", "plot"]
    assert out["fields"] == ["title", "plot"]


@pytest.mark.asyncio
async def test_search_media_rejects_unknown_field(env: tuple[AgentDeps, StubSearch]) -> None:
    """未知字段直接拒绝: 静默忽略会让模型以为限定生效, 据此得出"库里没有"就是错的."""
    deps, stub = env
    out = await _search_fn()(_ctx(deps), query="离家出走", fields=["plot", "description"])

    assert "error" in out
    assert "description" in out["error"]
    assert "plot" in out["error"]  # 回执列出合法取值
    assert stub.calls == []


@pytest.mark.asyncio
async def test_search_media_threshold_drops_weak_tail(env: tuple[AgentDeps, StubSearch]) -> None:
    """阈值按该串自己的相对分裁尾巴, 并报出砍掉多少."""
    deps, stub = env
    stub.ids = [1, 2, 3]
    stub.scores = [1.0, 0.55, 0.2]
    out = await _search_fn()(_ctx(deps), query="离家出走", threshold=0.5)

    assert out["ids"] == [1, 2]
    assert out["filtered_out"] == 1
    assert out["threshold"] == 0.5
    assert out["queries"][0]["hits"] == 3
    assert out["queries"][0]["kept"] == 2


@pytest.mark.asyncio
async def test_search_media_threshold_none_keeps_everything(env: tuple[AgentDeps, StubSearch]) -> None:
    """不传阈值时不过滤, 也不谎报 filtered_out."""
    deps, stub = env
    stub.ids = [1, 2, 3]
    stub.scores = [1.0, 0.55, 0.01]
    out = await _search_fn()(_ctx(deps), query="离家出走")

    assert len(out["ids"]) == 3
    assert out["filtered_out"] == 0
    assert out["threshold"] is None


@pytest.mark.asyncio
async def test_search_media_hits_carry_relative_score(env: tuple[AgentDeps, StubSearch]) -> None:
    """命中带相对相关度, 首名 1.0: 模型据此区分强命中与尾巴, 不必把整页都当候选."""
    deps, _stub = env
    out = await _search_fn()(_ctx(deps), query="离家出走")

    assert out["hits"][0]["score"] == 1.0
    assert all(0.0 < hit["score"] <= 1.0 for hit in out["hits"])
    assert [hit["score"] for hit in out["hits"]] == sorted((hit["score"] for hit in out["hits"]), reverse=True)


@pytest.mark.asyncio
async def test_search_media_rejects_whole_sentence_as_one_string(env: tuple[AgentDeps, StubSearch]) -> None:
    """整句用空格拼成一个串必然 0 命中 (词项 AND), 直接拒收并给拆法示范.

    实测 2026-10-26: 模型前 3 次调用都是这么写的, 全 0 命中, 白烧两轮才自己改对.
    """
    deps, stub = env
    out = await _search_fn()(_ctx(deps), queries=["离家出走 女孩 带回家 辅导 功课 考上大学"])

    assert "error" in out
    assert "AND" in out["error"]
    assert "离家出走" in out["error"]  # 示范拆法
    assert stub.calls == []


@pytest.mark.asyncio
async def test_search_media_accepts_short_phrase(env: tuple[AgentDeps, StubSearch]) -> None:
    """两三个词的短语在 AND 语义下有意义, 不能被长串规则误伤."""
    deps, _stub = env
    out = await _search_fn()(_ctx(deps), queries=["家庭教师 辅导"])

    assert "error" not in out


@pytest.mark.asyncio
async def test_search_media_trace_hints_on_zero_hits(env: tuple[AgentDeps, StubSearch]) -> None:
    """0 命中的串要带一句可行动的下一步, 而不是只给一个 0 让模型自己猜."""
    deps, stub = env
    stub.ids = []
    out = await _search_fn()(_ctx(deps), query="不存在的剧情")

    assert out["queries"][0]["hits"] == 0
    assert "没有落点" in out["queries"][0]["hint"]


@pytest.mark.asyncio
async def test_search_media_stops_at_call_budget(env: tuple[AgentDeps, StubSearch]) -> None:
    """预算用完时给可行动文案, 而不是让模型无限换措辞."""
    deps, _stub = env
    for index in range(SEARCH_CALL_BUDGET):
        out = await _search_fn()(_ctx(deps), query=f"查询串{index}")
        assert "error" not in out
    out = await _search_fn()(_ctx(deps), query="再来一个")

    assert "error" in out
    assert "上限" in out["error"]


@pytest.mark.asyncio
async def test_search_media_budget_survives_repeat_attempts(env: tuple[AgentDeps, StubSearch]) -> None:
    """重搜不消耗预算, 否则模型连"换个真串"的机会都没有."""
    deps, stub = env
    for index in range(SEARCH_CALL_BUDGET - 1):
        await _search_fn()(_ctx(deps), query=f"查询串{index}")
    for _ in range(3):
        assert "note" in await _search_fn()(_ctx(deps), query="查询串0")
    out = await _search_fn()(_ctx(deps), query="最后一个真串")

    assert "error" not in out
    assert len(stub.calls) == SEARCH_CALL_BUDGET


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
