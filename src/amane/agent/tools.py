from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from pydantic_ai import ApprovalRequired, RunContext
from pydantic_ai.toolsets import FunctionToolset

from ..db.models import SavedQueryEntity
from ..db.repository import Repository
from .bridge import AgentRuntimeBridge
from .cache import CachedResult
from .executor import QueryExecutor, extract_entity_ids
from .schema_docs import tables_with_column
from .sql import SqlNeedsApproval, SqlResult, SqlSandboxError, SqlTimeoutError

TOOL_OK = "OK"
"""成功回执: 模型下一步不需要任何观测的写操作只回它. 见 docs/dev/agent.md 返回值契约."""

SEARCH_LIMIT_MAX = 50
"""单次检索返回的条数上限. 助理只需要"够说清楚"的头部候选, 不是召回窗口 (见 SearchService.recall_window)."""

APPROVED_SQL_TIMEOUT_MS = 30_000
"""用户批准 ``allow_slow`` 后的查询预算.

批准流的语义是"我愿意等这条慢查询", 若仍按默认超时执行, 批准就只是白点一次 —— 慢查询照样
在同一个上限处被打断. 30s 足够本地万级表任一次全表聚合, 又不会让一条失控查询占住线程更久.
"""


class SearchHit(BaseModel):
    """命中条目的识别信息. 检索只出 id, 番号与标题供模型直接作答而不必再查一次."""

    id: int
    number: str
    title: str | None = None


class SearchMediaResult(BaseModel):
    ids: list[int]
    """按相关度排序的 metadata id."""
    hits: list[SearchHit]
    total: int
    """窗口内命中数, 语义随 mode 变化 (见 docs/dev/search.md)."""
    mode: str
    degraded: bool
    """要求走向量但没走成: 结果是关键词的, 不是语义的."""
    saved_query_id: int | None = None
    """传入 name 时物化的 metadata 预设, 行序即相关度顺序."""


def ranked_metadata_sql(ids: Sequence[int]) -> str:
    """按给定顺序取 metadata 行.

    顺序写进 SQL 而不是只留在内存: Saved Query 的权威是 SQL, 列表页重跑预设时必须能复现
    相关度顺序, 否则"最像的那条"会沉到末尾. ``instr`` 用带分隔符的 id 串定位, 故 ``1``
    不会匹配到 ``12`` / ``21``.
    """
    joined = ",".join(str(metadata_id) for metadata_id in ids)
    return (
        f"SELECT id, number, title FROM metadata WHERE id IN ({joined}) ORDER BY instr(',{joined},', ',' || id || ',')"
    )


#: 前后带 ``%`` 的子串匹配. 只用于**失败回执的文案**, 不作安全校验 —— 沙箱的只读边界由
#: 连接态与 authorizer 构成, 词法判断不参与, 误判最坏只是多说一句话.
_SUBSTRING_LIKE = re.compile(r"like\s*\(?\s*['\"]?%", re.IGNORECASE)

_SUBSTRING_SCAN_ADVICE = (
    "文本匹配请改用 search_media; SQL 子串扫描 (LIKE '%...%') 走不了索引, 在万级库上必然超时, "
    "且只命中字面片段 —— 同义改写会漏"
)

#: SQLite 对未知标识符的两种报错. 两者都是模型自己写错了名字, 可以就地纠正.
_NO_SUCH_COLUMN = re.compile(r"no such column:\s*([A-Za-z0-9_.\"'\[\]]+)", re.IGNORECASE)
_NO_SUCH_TABLE = re.compile(r"no such table:\s*([A-Za-z0-9_.\"'\[\]]+)", re.IGNORECASE)

#: 检索栈内部表: 不在注入的 schema 摘要里, 被模型猜到时直接指向 search_media.
_SEARCH_INTERNAL_TABLES = frozenset({"metadata_fts", "metadata_embeddings"})

#: 概念 → 真实列. 只收录真实日志里出现过的混淆 (2026-10-26 00:49 的
#: ``no such column: description``): 模型想表达"剧情简介"时会造 ``description`` / ``synopsis`` /
#: ``overview``, 而库里剧情列叫 ``plot``, ``overview`` 是演员简介. 这类概念冲突靠
#: ``tables_with_column`` 只能答"description 在 feed_items / saved_queries 上", 对改写毫无帮助.
_CONCEPT_COLUMNS: dict[str, str] = {
    "description": "metadata 的剧情列是 `plot`, 没有 description",
    "desc": "metadata 的剧情列是 `plot`",
    "summary": "metadata 的剧情列是 `plot`",
    "synopsis": "metadata 的剧情列是 `plot`",
    "synopsis_text": "metadata 的剧情列是 `plot`",
    "intro": "metadata 的剧情列是 `plot`",
    "introduction": "metadata 的剧情列是 `plot`",
    "plot_summary": "metadata 的剧情列是 `plot`",
    "content": "metadata 的剧情列是 `plot`",
    "overview": "`overview` 属于 actors (演员简介); metadata 的剧情列是 `plot`",
}


def identifier_hint(message: str) -> str | None:
    """``no such column`` / ``no such table`` 的就地纠错.

    沙箱把 SQLite 原文回给模型, 等于让它对着 ``no such column: description`` 再猜一次列名 ——
    而正确答案是确定的. 能确定才给提示, 不确定返回 ``None`` 让原始报错自己说话.
    """
    if (table_match := _NO_SUCH_TABLE.search(message)) is not None:
        table = table_match.group(1).strip("\"'[]").lower()
        if table in _SEARCH_INTERNAL_TABLES:
            return "检索栈内部表不可直接查询, 语义检索请用 search_media"
        return None

    column_match = _NO_SUCH_COLUMN.search(message)
    if column_match is None:
        return None
    # ``no such column: t.col`` 里的表前缀对定位无意义, 只留列名
    column = column_match.group(1).rsplit(".", 1)[-1].strip("\"'[]")
    if (concept := _CONCEPT_COLUMNS.get(column.lower())) is not None:
        return concept
    owners = tables_with_column(column)
    if len(owners) == 1:
        return f"列 `{column}` 在 `{owners[0]}` 上, 不在你查询的表里"
    if owners:
        return f"列 `{column}` 只在 {' / '.join(f'`{owner}`' for owner in owners)} 上"
    return f"库里没有 `{column}` 这一列; 只能用 schema 摘要里列出的列名"


def sql_failure_receipt(sql: str, message: str, *, search_available: bool) -> dict[str, Any]:
    """失败的 SQL 回执: 原始原因 + 就地纠错 + (必要时) 换工具指引.

    三段合成一条 ``error`` 而不是分字段: 模型一轮只读一次回执, 拆开容易被只看到第一条.
    """
    parts = [message]
    if (hint := identifier_hint(message)) is not None:
        parts.append(hint)
    if search_available and _SUBSTRING_LIKE.search(sql):
        parts.append(_SUBSTRING_SCAN_ADVICE)
    return {"error": "; ".join(parts)}


def unknown_field_error(unknown: Iterable[str], allowed: Iterable[str]) -> dict[str, Any]:
    """patch 拒绝未知字段时一并回可写字段, 省掉一轮试探."""
    return {"error": f"不允许的字段: {', '.join(sorted(unknown))}; 可写字段: {', '.join(sorted(allowed))}"}


class ExploreResult(BaseModel):
    columns: list[str]
    sample_rows: list[list[Any]]
    row_count: int
    truncated: bool = False
    saved_query_id: int | None = None
    """create_view 时物化的会话视图 id."""


class DeliverResult(BaseModel):
    saved_query_id: int
    name: str
    row_count: int


class InspectResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    total: int


@dataclass
class AgentDeps:
    repo: Repository
    executor: QueryExecutor
    session_id: int
    sql_timeout_ms: int
    sample_limit: int = 20
    bridge: AgentRuntimeBridge = field(default_factory=AgentRuntimeBridge)
    approved_sql_timeout_ms: int = APPROVED_SQL_TIMEOUT_MS
    """批准 ``allow_slow`` 后实际使用的超时, 见 ``APPROVED_SQL_TIMEOUT_MS``."""

    def timeout_for(self, *, allow_slow: bool) -> int:
        """批准过的慢查询用放宽后的预算, 其余用默认值.

        只在 ``allow_slow`` 时放宽: 普通探查不该有 30s 的预算, 否则一次误写的全表扫描会白占
        线程半分钟.
        """
        return self.approved_sql_timeout_ms if allow_slow else self.sql_timeout_ms


def require_approval(
    ctx: RunContext[AgentDeps],
    *,
    sql: str,
    tool: str,
    entity: SavedQueryEntity | None = None,
    name: str | None = None,
    create_view: bool = False,
    extra: dict[str, Any] | None = None,
) -> None:
    """未批准时 raise ApprovalRequired. metadata 供 UI 确认文案; approval 键为 tool_call_id."""
    if ctx.tool_call_approved:
        return
    tool_call_id = ctx.tool_call_id or ""
    if not tool_call_id:
        raise RuntimeError("require_approval 需要 tool_call_id")
    meta: dict[str, Any] = {
        "sql": sql,
        "tool": tool,
        "entity": entity if entity is not None else None,
        "name": name,
        "create_view": create_view,
        "extra": dict(extra or {}),
        "reason": "allow_slow",
    }
    raise ApprovalRequired(metadata=meta)


async def materialize_saved_query(
    deps: AgentDeps,
    *,
    sql: str,
    entity: SavedQueryEntity | None,
    name: str | None,
    description: str | None = None,
    result: SqlResult,
) -> tuple[int, str, list[int]]:
    """把全量 SQL 结果写成会话 SavedQuery 并入缓存.

    ``entity`` 省略按 ``DATA`` 落库: 不校验/不抽取 id, 只作数据表交付或探查视图.
    ``entity`` 为 metadata/actor 时按交付契约抽取 ``id`` 列 (缺失抛 ValueError).
    """
    entity = entity or SavedQueryEntity.DATA
    entity_ids: list[int] = []
    if entity is not SavedQueryEntity.DATA:
        entity_ids = extract_entity_ids(result.columns, result.rows)
    display_name = (name or "").strip() or ("数据查询" if entity is SavedQueryEntity.DATA else f"查询 ({entity})")
    saved = await deps.repo.create_saved_query(
        name=display_name,
        description=(description or "").strip(),
        sql=sql,
        entity=entity,
        session_id=deps.session_id,
        persisted=False,
    )
    assert saved.id is not None
    deps.executor.cache.put(CachedResult(saved_query_id=saved.id, sql=sql, columns=result.columns, rows=result.rows))
    return saved.id, saved.name, entity_ids


def build_explore_toolset() -> FunctionToolset[AgentDeps]:
    toolset: FunctionToolset[AgentDeps] = FunctionToolset()

    @toolset.tool
    async def search_media(
        ctx: RunContext[AgentDeps],
        query: str,
        limit: int = 20,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Search the library by meaning: plot / synopsis / theme / "similar to" / any paraphrase.

        Use this instead of SQL for anything a user describes in their own words. SQL
        `LIKE '%...%'` only matches literal substrings of the stored text: a paraphrase
        ("离家出走" vs "家出", "辅导功课" vs "家教") slips through, the leading `%` cannot use an
        index so the scan is a full-table pass over the whole library, and title/plot are stored
        as translated text that need not reuse the user's wording. An empty LIKE result means
        "no literal substring", NOT "no such title".
        Returns metadata ids ranked by relevance, their number/title, the `mode` actually used
        (`like` / `keyword` / `vector` / `hybrid`) and `degraded` (semantic path was required but
        unavailable — results are keyword-only then).
        Pass `name` to also deliver the ranked set as a metadata preset (`entity=metadata`, keeps
        the relevance order); otherwise use the ids in SQL `WHERE id IN (...)`.
        """
        gateway = ctx.deps.bridge.search() if ctx.deps.bridge.search is not None else None
        if gateway is None:
            return {"error": "检索未启用 (关键词与向量开关均关闭), 无法按语义检索; 请改用结构化 SQL 条件"}
        text = query.strip()
        if not text:
            return {"error": "查询词为空"}
        limit = max(1, min(limit, SEARCH_LIMIT_MAX))
        try:
            result = await gateway.search(text, limit=limit)
        except Exception as exc:
            # 检索栈的失败必须变成可行动的回执而不是回合错误: 否则模型只看到工具调用失败, 无从改写.
            return {"error": f"检索失败: {exc}"}
        ids = list(result.ids)[:limit]
        rows = await ctx.deps.repo.get_metadata_many(ids)
        out = SearchMediaResult(
            ids=ids,
            hits=[
                SearchHit(id=metadata_id, number=rows[metadata_id].number, title=rows[metadata_id].title)
                for metadata_id in ids
                if metadata_id in rows
            ],
            total=result.total,
            mode=result.mode.value,
            degraded=result.degraded,
        )
        if name is None:
            return out.model_dump(mode="json")
        if not ids:
            return {"error": "检索无命中, 未创建预设"}
        sql = ranked_metadata_sql(ids)
        try:
            sql_result = await ctx.deps.executor.run_sql(sql, timeout_ms=ctx.deps.sql_timeout_ms)
        except (SqlSandboxError, SqlTimeoutError) as exc:
            return {"error": str(exc)}
        try:
            view_id, _view_name, _entity_ids = await materialize_saved_query(
                ctx.deps,
                sql=sql,
                entity=SavedQueryEntity.METADATA,
                name=name,
                result=sql_result,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        out.saved_query_id = view_id
        return out.model_dump(mode="json")

    @toolset.tool
    async def sql_explore(
        ctx: RunContext[AgentDeps],
        sql: str,
        allow_slow: bool = False,
        create_view: bool = False,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Exploratory read-only SQL. Sample rows by default.

        Set create_view=True to materialize a session view for inspect_result pagination
        instead of hand-written LIMIT/OFFSET probes. Views are just row arrays — no entity
        or `id` column requirement. Views are not shown as UI chips.
        """
        try:
            result = await ctx.deps.executor.run_sql(
                sql,
                timeout_ms=ctx.deps.timeout_for(allow_slow=allow_slow),
                allow_slow=allow_slow,
                approved=ctx.tool_call_approved,
                max_rows=None if create_view else ctx.deps.sample_limit,
            )
        except SqlNeedsApproval:
            if allow_slow and ctx.deps.bridge.search is not None and _SUBSTRING_LIKE.search(sql):
                # 检索可用时的全表 LIKE 不值得让用户批准: 批了也只是换来一次慢的全表扫描
                return {"error": _SUBSTRING_SCAN_ADVICE}
            require_approval(
                ctx,
                sql=sql,
                tool="sql_explore",
                name=name,
                create_view=create_view,
            )
            raise  # pragma: no cover - require_approval 必 raise
        except (SqlSandboxError, SqlTimeoutError) as exc:
            return sql_failure_receipt(sql, str(exc), search_available=ctx.deps.bridge.search is not None)

        if create_view:
            try:
                view_id, _view_name, _entity_ids = await materialize_saved_query(
                    ctx.deps,
                    sql=sql,
                    entity=None,
                    name=name,
                    result=result,
                )
            except ValueError as exc:
                return {"error": str(exc)}
            out = ExploreResult(
                columns=result.columns,
                sample_rows=result.rows[: ctx.deps.sample_limit],
                row_count=len(result.rows),
                truncated=False,
                saved_query_id=view_id,
            )
        else:
            out = ExploreResult(
                columns=result.columns,
                sample_rows=result.rows[: ctx.deps.sample_limit],
                row_count=result.row_count if result.row_count >= 0 else len(result.rows),
                truncated=result.row_count < 0,
            )
        return out.model_dump(mode="json")

    @toolset.tool
    async def sql_deliver(
        ctx: RunContext[AgentDeps],
        sql: str,
        entity: SavedQueryEntity | None = None,
        name: str | None = None,
        description: str | None = None,
        allow_slow: bool = False,
    ) -> dict[str, Any]:
        """Deliver a final result set for humans. Creates a saved_query.

        name / description: short human-readable label and note, shown in the preset list
        and on the data page.
        entity=metadata|actor: SQL MUST return a column named `id`; the preset becomes a
        filter in /meta or /actors AND can be opened as a data table.
        Omit entity (or use data): any read-only result, rendered only as a data table.
        """
        try:
            result = await ctx.deps.executor.run_sql(
                sql,
                timeout_ms=ctx.deps.timeout_for(allow_slow=allow_slow),
                allow_slow=allow_slow,
                approved=ctx.tool_call_approved,
            )
        except SqlNeedsApproval:
            if allow_slow and ctx.deps.bridge.search is not None and _SUBSTRING_LIKE.search(sql):
                return {"error": _SUBSTRING_SCAN_ADVICE}
            require_approval(ctx, sql=sql, tool="sql_deliver", entity=entity, name=name)
            raise  # pragma: no cover
        except (SqlSandboxError, SqlTimeoutError) as exc:
            return sql_failure_receipt(sql, str(exc), search_available=ctx.deps.bridge.search is not None)

        try:
            view_id, display_name, _ = await materialize_saved_query(
                ctx.deps,
                sql=sql,
                entity=entity,
                name=name,
                description=description,
                result=result,
            )
        except ValueError as exc:
            return {"error": str(exc)}

        out = DeliverResult(
            saved_query_id=view_id,
            name=display_name,
            row_count=len(result.rows),
        )
        return out.model_dump(mode="json")

    @toolset.tool
    async def inspect_result(
        ctx: RunContext[AgentDeps],
        saved_query_id: int,
        offset: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Inspect rows of a saved_query / explore view by id (uses cache or re-runs SQL)."""
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        query = await ctx.deps.repo.get_saved_query(saved_query_id)
        if query is None:
            return {"error": f"saved_query {saved_query_id} 不存在"}
        try:
            cached = await ctx.deps.executor.ensure_cached(query, timeout_ms=ctx.deps.sql_timeout_ms)
        except (SqlSandboxError, SqlTimeoutError) as exc:
            return {"error": str(exc)}

        out = InspectResult(
            columns=cached.columns,
            rows=cached.rows[offset : offset + limit],
            total=len(cached.rows),
        )
        return out.model_dump(mode="json")

    return toolset
