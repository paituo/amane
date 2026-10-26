from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import ApprovalRequired, RunContext
from pydantic_ai.toolsets import FunctionToolset

from ..db.models import Metadata, SavedQueryEntity
from ..db.repository import Repository
from ..enums import SearchField, SearchMode
from .bridge import AgentRuntimeBridge
from .cache import CachedResult
from .executor import QueryExecutor, extract_entity_ids
from .schema_docs import tables_with_column
from .sql import SqlNeedsApproval, SqlResult, SqlSandboxError, SqlTimeoutError

TOOL_OK = "OK"
"""成功回执: 模型下一步不需要任何观测的写操作只回它. 见 docs/dev/agent.md 返回值契约."""

SEARCH_LIMIT_MAX = 50
"""单页返回的条数上限."""

SEARCH_DEPTH = 50
"""单次检索的稳定排序深度, 翻页在此深度内切片.

翻页必须基于**同一个**排序, 否则第 2 页会与第 1 页重叠或漏项: 融合分数是候选集内的 z 标准化,
候选集一变排序就变. 所以每次调用都按固定深度 ``SEARCH_DEPTH`` 检索, 再用 ``offset`` 切片 ——
代价是多跑几条候选, 换来翻页可预期.
"""

PLOT_PREVIEW_CHARS = 320
"""命中条目回带的剧情摘要字数.

只有标题时模型无从判断相关性: 标题是译者剪出来的短句, "离家出走的女孩被带回家辅导考上大学"
这类**剧情**描述在标题里根本不存在, 于是模型只能猜片名或改措辞重搜. 摘要必须足够长到能放下
一个完整情节转折, 又要短到 20 条命中也塞得进上下文. 320 字覆盖本库 plot 的常见长度
(实测均长 148), 绝大多数条目能整段给出而不是被截半句.
"""

_HIT_ACTORS_MAX = 6
_HIT_DIRECTORS_MAX = 3
_HIT_TAGS_MAX = 8

QUERY_MAX = 6
"""单次调用最多带几个查询分割串.

一个剧情描述拆出 3-4 个就够 ("离家出走" / "家庭教师 辅导" / "考上大学"); 上限给到 6 是留给
"同时找几个不同片子"这类混合查询. 再多会把噪声词也当成一个串, 融合后被稀释.
"""

QUERY_WORDS_MAX = 3
"""单个查询串的空白分词上限.

关键词路是**词项 AND**: "离家出走 女孩 带回家 辅导 功课 考上大学" 要求六个词同时出现, 实际上
等于 0 命中. 模型会把整句用空格拼成一个串 (2026-10-26 实测: 前 3 次调用都是这么写的, 全部
0 命中, 白烧两轮), 所以这里直接拒收而不是让它自己从空结果里悟 —— 拒绝时给出正确的拆法示范.
值取 3 而不是 2 是给"家庭教师 辅导"这类两词短语与英文片名留余量.
"""

_RRF_K = 60
"""倒数排名融合的平滑常数, 取检索文献的惯用值: 让第一名与第二名之间不至于差出一整档."""

SEARCH_CALL_BUDGET = 6
"""本回合 ``search_media`` 的调用次数上限.

实测反例 (2026-10-26, session 8 之后): 同一句话换成 23 个近似串反复检索, 花了 13 分钟、结果集
几乎不变, 最后仍未收敛. 检索本该是"取到证据就作答", 不是穷举. 预算用完时工具回一句可行动的
文案, 逼模型基于已有命中收敛; 真需要新条件时它还留有 SQL 这条路.
"""


def _rrf(rankings: Sequence[Sequence[int]]) -> tuple[list[int], dict[int, int], dict[int, float]]:
    """多个查询分割串的加权倒数排名融合, 回 (有序 id, {id: 命中串数}, {id: 融合分}).

    不用"取交集"是因为分割串之间常常不同义: 用户说"辅导功课", 库内写"家庭教师", 语义路能桥接,
    关键词路未必; 硬交集会把只在一路上出现的正确条目直接删掉. RRF 让**命中多个串的条目**
    自然浮到前面, 同时保留单串里的强命中, 并且不依赖任何一路的分数口径.

    **按串稀有度加权** (``1/sqrt(命中数)``, 即 IDF 的思路): 宽泛串会贡献几十条候选,
    若不加权, 它们的名次分 (1/61, 1/62, …) 会把精确串里的第一名挤到页外 —— 实测
    ["离家出走"(50 条), "辅导功课"(4), "考上大学"(5)] 时, 正确条目 `SCOP-193` 是"考上大学"
    的第 1 名, 却排在 20 条"离家出走"之后. 命中越少的串越有区分度, 权重就该越高.
    """
    matched: dict[int, int] = {}
    score: dict[int, float] = {}
    for ranking in rankings:
        if not ranking:
            continue
        weight = 1.0 / math.sqrt(len(ranking))
        for rank, metadata_id in enumerate(ranking):
            score[metadata_id] = score.get(metadata_id, 0.0) + weight / (_RRF_K + rank + 1)
            matched[metadata_id] = matched.get(metadata_id, 0) + 1
    ordered = sorted(score, key=lambda metadata_id: (-score[metadata_id], -matched[metadata_id], metadata_id))
    return ordered, matched, score


APPROVED_SQL_TIMEOUT_MS = 30_000
"""用户批准 ``allow_slow`` 后的查询预算.

批准流的语义是"我愿意等这条慢查询", 若仍按默认超时执行, 批准就只是白点一次 —— 慢查询照样
在同一个上限处被打断. 30s 足够本地万级表任一次全表聚合, 又不会让一条失控查询占住线程更久.
"""


class SearchHit(BaseModel):
    """命中条目的可判断信息.

    不是"识别信息"而是"判断依据": 模型要能只靠这一条决定"这条是不是用户说的那部", 否则它就
    只能拿标题猜 —— 而标题里不含剧情. ``plot`` 是摘要, ``actors`` / ``tags`` / ``series`` 提供
    二次检索的角度 (用户说"那个老师", 演员名比标题有用).
    """

    id: int
    number: str
    title: str | None = None
    plot: str | None = None
    """剧情摘要, 最多 ``PLOT_PREVIEW_CHARS`` 字; 原文更长时以 … 结尾."""
    content_type: str | None = None
    release: str | None = None
    runtime: int | None = None
    studio: str | None = None
    publisher: str | None = None
    series: str | None = None
    directors: list[str] = Field(default_factory=list)
    actors: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    score: float = 1.0
    """本页内的相对相关度, 首名恒 1.0.

    跨分割串融合后的名次分, 不是概率、也不能跨查询比较. 用途是让模型看出"前 3 条明显强,
    后面这批是尾巴", 从而不必把整页都当成候选.
    """
    matched_queries: int = 1
    """命中了几个查询分割串.

    只有多串调用时才有区分度: ``3/3`` 表示每个串都召回了它, ``1/3`` 只是某一个串里的近邻.
    单串调用恒为 1, 不要拿它当相关度分数用.
    """


class QueryTrace(BaseModel):
    """一个查询分割串的检索概况, 供模型判断"哪个条件把结果卡空了"."""

    query: str
    hits: int
    """该串在排序深度内的命中条数 (阈值过滤前)."""
    kept: int = 0
    """阈值过滤后留下的条数. ``hits`` 不为 0 而 ``kept`` 为 0, 说明是阈值设太高, 不是库里没有."""
    hint: str | None = None
    """该串的可行动建议 (换更具体的剧情词 / 降阈值), 无需处理时为 None."""


class SearchMediaResult(BaseModel):
    ids: list[int]
    """本页按相关度排序的 metadata id."""
    hits: list[SearchHit]
    total: int
    """可翻页的排序深度 = 本回合累计检索实际能给出的条数上限 (``ranked`` 长度), 不是全库匹配数.

    检索栈只保证召回窗口内的顺序 (见 docs/dev/search.md), 窗口外没有可信排名, 所以这个数
    可能小于用户以为的"库里相关条数". 报给用户时不要把它说成"库里共有 N 条相关".
    """
    offset: int = 0
    """本页起点, 与请求一致."""
    has_more: bool = False
    """``offset + len(hits) < total``, 即还有更深的一页可看."""
    next_offset: int | None = None
    """继续翻页时传给 ``offset`` 的值; ``has_more`` 为假时为 None."""
    mode: str
    degraded: bool
    """要求走向量但没走成: 结果是关键词的, 不是语义的."""
    queries: list[QueryTrace] = Field(default_factory=list)
    """本次新检索了哪些分割串、各自命中与留存多少."""
    searched: list[str] = Field(default_factory=list)
    """本回合已检索过的全部分割串 (累积). 其中任何一个都不要再搜第二次."""
    fields: list[str] | None = None
    """本次限定的检索字段; None 表示全部字段."""
    threshold: float | None = None
    """本次生效的相对相关度下限 (0..1); None 表示未过滤."""
    filtered_out: int = 0
    """被阈值砍掉的候选数. 它明显大于 0 而结果很少, 说明阈值偏高."""
    note: str | None = None
    """工具侧的提醒 (例如"这些串已搜过, 以下为累积排序"), 无则省略."""
    saved_query_id: int | None = None
    """传入 name 时物化的 metadata 预设, 行序即相关度顺序."""


def _clip(text: str | None, limit: int) -> str | None:
    """压掉换行与多余空白再截断; 空串归为 None 而不是空字符串."""
    if not text:
        return None
    collapsed = " ".join(text.split())
    if not collapsed:
        return None
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def _search_hit(row: Metadata, *, score: float = 1.0, matched_queries: int = 1) -> SearchHit:
    """把一条 metadata 行压成命中卡片.

    字段选择只服务一个目的: 让模型**在当前这一条上**就能判断相关性, 不必再发一次查询去补信息,
    更不能只拿标题去猜. 所以 plot 必须带, 演员/标签截断到前几个 (够定位就够, 全量会撑爆上下文).
    少了 plot 的命中卡片是无效的: 标题里没有剧情, 模型只能凭记忆补 —— 那正是它开始编片名的起点.
    """
    metadata_id = row.id
    if metadata_id is None:  # pragma: no cover - 主键来自刚查出的行
        raise ValueError("metadata row without id")
    return SearchHit(
        id=metadata_id,
        number=row.number,
        title=row.title,
        plot=_clip(row.plot, PLOT_PREVIEW_CHARS),
        content_type=row.content_type.value,
        release=row.release,
        runtime=row.runtime,
        studio=row.studio,
        publisher=row.publisher,
        series=row.series,
        directors=list(row.directors[:_HIT_DIRECTORS_MAX]),
        actors=list(row.actors[:_HIT_ACTORS_MAX]),
        tags=list(row.tags[:_HIT_TAGS_MAX]),
        score=round(score, 4),
        matched_queries=matched_queries,
    )


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
    search_trail: list[str] = field(default_factory=list)
    """本回合已执行过的查询串 (归一化后), 由 ``search_media`` 自己维护.

    用途是止损: 实测同一句话换措辞反复检索会吃掉 20+ 轮模型调用 (2026-10-26 探针: 23 次
    search、13 分钟), 而结果集几乎不变. 记录在 deps 上而不是模块级, 回合之间自然隔离.
    """
    search_calls: int = 0
    """本回合 ``search_media`` 的调用次数, 与 ``search_trail`` 一起实现检索预算."""
    search_rankings: list[list[int]] = field(default_factory=list)
    """本回合每个查询串的有序命中, **跨调用累积**.

    模型一次只能发一个 tool_call, 所以"混合查询"在运行时表现为多轮调用, 每轮带一个分割串.
    要给出混合后的排序就必须把历史串一起参与融合 —— 否则第 2 轮只看到第 2 个串的结果,
    等于把上一轮的证据丢了. 累积发生在 deps 上, 回合结束自然清空.
    """
    search_modes: list[SearchMode] = field(default_factory=list)
    """累积串各自走过的检索路径, 用于回执里的 ``mode`` 汇总."""

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


def _combined_mode(modes: Sequence[SearchMode]) -> str:
    """多分面各有各的路径时对外报 hybrid: 结果是多条路径融合出来的, 单路名都说不全."""
    unique = {mode.value for mode in modes}
    if len(unique) == 1:
        return next(iter(unique))
    return SearchMode.HYBRID.value


def _collect_queries(query: str | None, queries: Sequence[str] | None) -> list[str] | str:
    """归一化查询串入参; 非法时回一句可行动的文案 (str), 合法时回去重后的列表.

    超过 ``QUERY_MAX`` 报错而不是静默截断: 少查一个串会让"命中几个条件"这个判断失真,
    模型却以为全查过了 —— 宁可让它合并后重发一次.
    """
    # 兼容端点会把单串塞进数组参数 (实测出现过 "queries": "离家出走 女孩 带回家"), 而 str 是
    # Sequence[str]: 不先包一层就会被逐字符遍历, 变成几十个单字查询.
    if isinstance(queries, str):
        queries = [queries]
    raw: list[str] = []
    if query and query.strip():
        raw.append(query.strip())
    raw.extend(item.strip() for item in (queries or []) if item and item.strip())
    items = list(dict.fromkeys(raw))
    if not items:
        return "查询词为空: 需传入 query 或 queries"
    if len(items) > QUERY_MAX:
        return f"查询串过多 ({len(items)} 个): 最多 {QUERY_MAX} 个, 请合并语义相近的串"
    for item in items:
        words = item.split()
        if len(words) > QUERY_WORDS_MAX:
            return (
                f"查询串 {item!r} 含 {len(words)} 个词, 关键词路是**词项 AND**, 这类串要求每个词同时出现, "
                f"实际等于 0 命中。把整句拆成 2-4 个短串分别传: "
                f'queries=["离家出走", "家庭教师 辅导", "考上大学"] (每个串 {QUERY_WORDS_MAX} 词以内), '
                f"或直接传不含空格的短语。"
            )
    return items


def _parse_fields(fields: Sequence[str] | None) -> list[SearchField] | str | None:
    """把模型给的字段名收窄到 ``SearchField``; 非法值回一句可行动的文案 (str).

    未知字段直接拒绝而不是忽略: 静默忽略会让模型以为"限定在 plot 里搜"生效了, 实际仍然全字段搜,
    它据此得出的"库里没有"就是错的.
    """
    if not fields:
        return None
    resolved: list[SearchField] = []
    for raw in fields:
        key = raw.strip().casefold()
        if not key:
            continue
        try:
            resolved.append(SearchField(key))
        except ValueError:
            valid = ", ".join(field.value for field in SearchField)
            return f"未知检索字段 {raw!r}: 可选 {valid}"
    return list(dict.fromkeys(resolved)) or None


def _query_key(text: str) -> str:
    """判重用的归一化 key: 只留字母数字, 忽略空白/标点/大小写.

    "离家出走" 与 "离家 出走" 是同一个串, 重搜只会把同一批候选再排一遍.
    """
    return "".join(ch for ch in text if ch.isalnum()).casefold()


def _trace_hint(*, query: str, hits: int, kept: int, floor: float | None) -> str | None:
    """给单个串一句可行动的下一步, 而不是让模型面对 ``hits: 0`` 自己猜."""
    if hits == 0:
        if len(query.split()) > 1:
            return "多词串在 AND 语义下要求所有词同时出现, 0 命中是预期: 拆成单个词分别传"
        return "这个串在库里没有落点: 换成更具体的剧情词 (人物/事件/关系), 不要重复同一串"
    if kept == 0 and floor is not None:
        return f"命中 {hits} 条但都被 threshold={floor:g} 砍掉: 阈值偏高, 降到 0.3 左右或去掉它"
    return None


def _dedupe_against_trail(trail: Sequence[str], queries: Sequence[str]) -> tuple[list[str], list[str]]:
    """拆成 (本回合没搜过的, 已经搜过的); 两边都按原顺序, 保持确定性."""
    seen = {_query_key(item) for item in trail}
    fresh: list[str] = []
    repeated: list[str] = []
    for item in queries:
        key = _query_key(item)
        if key in seen:
            repeated.append(item)
        else:
            fresh.append(item)
            seen.add(key)
    return fresh, repeated


async def _render_search_page(
    ctx: RunContext[AgentDeps],
    *,
    offset: int,
    limit: int,
    floor: float | None,
    fields: Sequence[SearchField] | None,
    traces: Sequence[QueryTrace],
    degraded: bool,
    note: str | None,
    name: str | None,
    filtered_out: int = 0,
) -> dict[str, Any]:
    """把**累积**的分串排序渲染成当前页.

    每次调用都重算全量融合再切片, 而不是只回本次这一串的结果: 模型一次只发一个 tool_call,
    若每轮只给本串结果, 它永远看不到"哪个候选同时满足多个条件", 也就做不成混合查询.
    ``score`` 是融合后的相对名次分 (本页最高者为 1.0), 供模型区分强命中与尾巴.
    """
    ranked, matched, weights = _rrf(ctx.deps.search_rankings)
    ids = ranked[offset : offset + limit]
    rows = await ctx.deps.repo.get_metadata_many(ids)
    top = max((weights.get(metadata_id, 0.0) for metadata_id in ids), default=0.0)
    hits = [
        _search_hit(
            rows[metadata_id],
            score=(weights[metadata_id] / top) if top > 0.0 else 1.0,
            matched_queries=matched.get(metadata_id, 1),
        )
        for metadata_id in ids
        if metadata_id in rows
    ]
    next_offset = offset + len(ids) if offset + len(ids) < len(ranked) else None
    out = SearchMediaResult(
        ids=ids,
        hits=hits,
        total=len(ranked),
        offset=offset,
        has_more=next_offset is not None,
        next_offset=next_offset,
        mode=_combined_mode(ctx.deps.search_modes),
        degraded=degraded,
        queries=list(traces),
        searched=list(ctx.deps.search_trail),
        fields=[field.value for field in fields] if fields else None,
        threshold=floor,
        filtered_out=filtered_out,
        note=note,
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


def build_explore_toolset() -> FunctionToolset[AgentDeps]:
    toolset: FunctionToolset[AgentDeps] = FunctionToolset()

    @toolset.tool
    async def search_media(
        ctx: RunContext[AgentDeps],
        query: str | None = None,
        queries: list[str] | str | None = None,
        fields: list[str] | None = None,
        limit: int = 20,
        offset: int = 0,
        threshold: float | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Search the library by meaning: plot / synopsis / theme / "similar to" / any paraphrase.

        Use this instead of SQL for anything a user describes in their own words. SQL
        `LIKE '%...%'` only matches literal substrings of the stored text: a paraphrase
        ("离家出走" vs "家出", "辅导功课" vs "家教") slips through, the leading `%` cannot use an
        index so the scan is a full-table pass over the whole library, and title/plot are stored
        as translated text that need not reuse the user's wording. An empty LIKE result means
        "no literal substring", NOT "no such title".

        Each hit carries the fields you need to JUDGE relevance — `plot` (plot summary excerpt),
        `directors`, `actors`, `tags`, `series`, `studio`, `publisher`, `release`, `runtime`, plus
        `score` (relative relevance, top hit = 1.0) and `matched_queries` — so a plot-shaped
        request ("a runaway girl is taken home and tutored into university") can be decided from
        the hit itself. Read `plot`; never infer from `title` alone and never guess a title from
        memory.

        SPLIT THE QUERY. The keyword path ANDs the terms of one string, so a whole sentence
        usually comes back empty. Send 2-4 short strings instead — either all at once as `queries`
        (["离家出走", "家庭教师 辅导", "考上大学"]) or one per call. Calls ADD UP: every string you
        have searched stays in the fused ranking, so a later call shows the combined result and
        `matched_queries` says how many of your strings a hit satisfies. `queries[].hits` /
        `queries[].kept` show which string found nothing (0 hits = no anchor in the library,
        drop or reword it). Change the ANGLE, never re-send the same string with synonyms.

        `fields` narrows which columns the keyword path looks at: number / title / plot / actors /
        tags / series / studio / publisher (default: all). The semantic path always reads the whole
        record, so `fields` only sharpens the literal match — use it when you want e.g. only
        titles, or only plot text.

        `threshold` (0..1) drops hits below that share of the best hit of each string — useful to
        cut a long tail of weak neighbours. `filtered_out` reports how many were cut. Set it high
        (0.6+) to get only the strong matches, low (0.2) to keep everything plausible.

        PAGING: `total` is how deep the fused ranking goes, `has_more` / `next_offset` tell you
        whether a deeper page exists. If nothing on this page matches and `has_more` is true, keep
        going with `offset=next_offset` before concluding anything — never report "库里没有" after
        looking at only the first page.

        Returns ids ranked by relevance, the `mode` actually used (`like` / `keyword` / `vector` /
        `hybrid`) and `degraded` (semantic path was required but unavailable — results are
        keyword-only then).

        Pass `name` to also deliver the ranked set as a metadata preset (`entity=metadata`, keeps
        the relevance order); otherwise use the ids in SQL `WHERE id IN (...)`.
        """
        gateway = ctx.deps.bridge.search() if ctx.deps.bridge.search is not None else None
        if gateway is None:
            return {"error": "检索未启用 (关键词与向量开关均关闭), 无法按语义检索; 请改用结构化 SQL 条件"}
        items = _collect_queries(query, queries)
        if isinstance(items, str):
            return {"error": items}
        narrowed = _parse_fields(fields)
        if isinstance(narrowed, str):
            return {"error": narrowed}
        limit = max(1, min(limit, SEARCH_LIMIT_MAX))
        offset = max(0, min(offset, SEARCH_DEPTH))
        floor = None if threshold is None else max(0.0, min(1.0, float(threshold)))
        # 判重不看 offset: 翻页只需要"在已有排序上切片", 不该重跑检索. 旧实现对 offset>0 豁免判重,
        # 结果同一个串被再检索一次又进累积 —— 重复投票把排名整体扰动 (实测翻页第 21 条竟是原第 1 页
        # 的末位), 而检索本身是幂等的, 重跑纯属浪费.
        fresh, repeated = _dedupe_against_trail(ctx.deps.search_trail, items)
        if not fresh and not ctx.deps.search_rankings:
            return {"error": f"这些查询串本回合已检索过 ({', '.join(repeated)}), 且此前没有任何命中."}
        if not fresh:
            # 重复串不再消耗预算, 也不报错: 把**累积后的当前页**还给它, 让模型在已有证据上收敛.
            return await _render_search_page(
                ctx,
                offset=offset,
                limit=limit,
                floor=floor,
                fields=narrowed,
                traces=[],
                degraded=False,
                note=f"这些查询串本回合已检索过 ({', '.join(repeated)}), 以下为累积排序的第 {offset} 条起; "
                "不要换措辞重搜, 直接基于它作答或继续翻页.",
                name=name,
            )
        if ctx.deps.search_calls >= SEARCH_CALL_BUDGET:
            return {
                "error": (
                    f"本回合检索已达上限 ({SEARCH_CALL_BUDGET} 次)。停止检索, 立即用已返回的命中作答: "
                    "说明实际看过哪些条目、哪一条最接近、还差哪一点。若必须再筛, 用 sql_explore 在已有 "
                    "`id IN (...)` 上做结构化过滤, 不要重新检索。"
                )
            }
        ctx.deps.search_calls += 1
        ctx.deps.search_trail.extend(fresh)
        traces: list[QueryTrace] = []
        degraded = False
        filtered_out = 0
        for item in fresh:
            try:
                # 固定深度检索再切片: 翻页必须共享同一个排序, 否则页与页之间会重叠或漏项.
                result = await gateway.search(item, limit=SEARCH_DEPTH, fields=narrowed)
            except Exception as exc:
                # 检索栈的失败必须变成可行动的回执而不是回合错误: 否则模型只看到工具调用失败, 无从改写.
                return {"error": f"检索失败: {exc}"}
            ids = list(result.ids)
            kept = ids
            if floor is not None and len(result.scores) == len(ids):
                # 阈值按**该串自己的相对分**裁尾巴: 绝对分跨 mode 不可比, 相对分才可以.
                kept = [metadata_id for metadata_id, score in zip(ids, result.scores, strict=True) if score >= floor]
                filtered_out += len(ids) - len(kept)
            ctx.deps.search_rankings.append(kept)
            ctx.deps.search_modes.append(result.mode)
            degraded = degraded or result.degraded
            traces.append(
                QueryTrace(
                    query=item,
                    hits=len(ids),
                    kept=len(kept),
                    hint=_trace_hint(query=item, hits=len(ids), kept=len(kept), floor=floor),
                )
            )
        return await _render_search_page(
            ctx,
            offset=offset,
            limit=limit,
            floor=floor,
            fields=narrowed,
            traces=traces,
            degraded=degraded,
            note=None,
            name=name,
            filtered_out=filtered_out,
        )

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
