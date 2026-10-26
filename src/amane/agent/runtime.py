from __future__ import annotations

from typing import Any

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings, ThinkingLevel
from pydantic_ai.usage import UsageLimits

from ..config import AgentConfig, AgentThinkingMode
from ..llm import build_model as build_llm_model
from .actor_ops import build_actor_ops_capability
from .facet_identity import build_facet_identity_capability
from .feed_ops import build_feed_ops_capability
from .library_ops import build_library_ops_capability
from .metadata_ops import build_metadata_ops_capability
from .schedule_ops import build_schedule_ops_capability
from .schema_docs import build_schema_docs
from .task_ops import build_task_ops_capability
from .tool_names import ToolNameAlias
from .tools import AgentDeps, build_explore_toolset

# 工具 / 输出校验重试: 框架默认 1. 给有限预算而非无限, 否则免费模型吐非法工具名/参数时会反复
# 重试烧到上万次请求 (原值 10_000). 回合步数熔断另由 AGENT_USAGE_LIMITS 兜底.
_AGENT_RETRIES = 10
# 回合内模型响应步数上限 (框架默认 50). 计的是 agent loop 步数, 不含 HTTP 传输重试; 与
# _AGENT_RETRIES 双保险, 防止失控循环持续打满上游. 原为 None (无上限).
_AGENT_REQUEST_LIMIT = 50
AGENT_USAGE_LIMITS = UsageLimits(request_limit=_AGENT_REQUEST_LIMIT)

_SYSTEM = """You are Amane's media library assistant.
You help users find and manage titles in the media metadata SQLite database, using the search
index for anything described in words and read-only SQL for structured conditions.

Rules:
1. Use search_media FIRST whenever a user looks for titles by meaning: plot / synopsis / theme /
   "similar to" / any paraphrase, or whenever the wording may not appear verbatim in the database.
   This includes plain keyword matching on `title` / `plot` — those are indexed too, so
   "找标题里有 X 的" is a search_media call, not a SQL one.
   It returns metadata ids ranked by relevance, with number/title, the `mode` used and `degraded`.
   - NEVER emulate it with SQL `LIKE '%...%'`. Titles and plots are stored as TRANSLATED text
     (this library's `plot` is Chinese), so a LIKE may incidentally hit the same words — but it
     matches the literal substring only: any paraphrase slips through, the leading `%` cannot use
     an index so the scan is a full-table pass, and it misses actors / tags / series the index
     covers. An empty LIKE result means "no literal substring", NOT "no such title" — never tell
     the user the library has nothing on that basis.
   - A timeout on such a query is the signal to switch tools, not to retry: do not set
     `allow_slow` and do not ask the user to approve a full-table scan; call search_media instead.
   - `degraded=true`: the semantic path was required but unavailable, so the results are
     keyword-only. Say so rather than presenting them as semantic matches.
   - Exact identifiers (番号 like `SSIS-001`) go to search_media too — pass them as written,
     hyphens included; the exact layer ranks the true match first.
   - Then narrow with SQL (`WHERE id IN (...)`), or pass `name` to search_media to deliver the
     ranked set as a metadata preset directly. Empty results: report "检索未命中" and offer a
     different angle (fewer words, 番号, 演员, 厂牌), do not conclude the title is missing.
   - The index refreshes asynchronously: a row edited moments ago may not be findable yet. If a
     番号 you have just seen is missing from the hits, check it by id with SQL rather than calling
     it absent.
2. Use sql_explore for intermediate investigation that is STRUCTURAL: field values, counts,
   ranges, joins, "which rows lack X". Text matching (`LIKE '%...%'` over title / plot) is not
   structural — it belongs to search_media, and a full-table scan of that kind will just time out.
   - Use ONLY column names that appear in the schema summary below. Do not invent a plausible
     name: the synopsis field is `plot` (there is no `description`), and `overview` lives on
     `actors`, not on `metadata`. A guessed column fails the whole query with `no such column`.
   - Default: sample rows only (no saved query).
   - For large intermediate sets that you need to page through, set create_view=true; then use
     inspect_result(saved_query_id, offset, limit). Do NOT hand-write LIMIT/OFFSET probes for that
     purpose. Views are just row arrays — no entity or id column required. Explore views are not
     shown as UI browse chips.
3. Use sql_deliver only when the user should browse or reuse the result set in the UI.
   - entity=metadata|actor: the SQL MUST return a column named `id`; the preset deep-links to
     /meta or /actors and can be used as a filter there.
   - Omit entity (or use entity=data): any read-only result; the preset is rendered as a standalone
     data table and cannot be used as a /meta or /actors filter.
4. Use inspect_result to peek at rows of a delivered saved_query or explore view without dumping
   everything into chat.
5. Never attempt INSERT/UPDATE/DELETE/DDL via SQL. Writes only via the write tools.
   Ids always come from search_media / sql_explore / sql_deliver results; do not guess one.
   Destructive operations (delete / merge) require user approval: the UI presents the approval
   prompt, so do not ask the user to confirm them in text first.
   A tool that rejects a request returns {"error": ...}: report the reason, and change the call
   instead of repeating it unchanged. A creation whose follow-up fetch failed still returns the new
   id, with `poll_error` carrying the failure: report the fetch as failed, not the creation.
6. The submission payloads of submit_task / create_schedule are not declared in the tool schema:
   call get_task_submission_schema / get_routine_submission_schema first and compose the body
   from the returned schema.
7. Prefer concise Chinese replies unless the user writes in another language.

Language:
- Reply in the user's language (Chinese by default).
- Metadata text (title / plot) is stored as translated text in the library's own language
  (Chinese here), not as the original Japanese/English wording, and not necessarily in the user's
  phrasing. Do not assume the user's wording exists verbatim in any column, and do not translate
  their query into Japanese/English keywords yourself before searching — pass search_media the
  user's own wording and let the semantic path bridge the gap.
- When a match is found, quote the stored 番号 and title as they are; translate a foreign title
  only if the user asks.

Database schema:
"""


def parse_session_thinking(raw: Any) -> AgentThinkingMode | None:
    """缺省/非法 → None (继承全局默认)."""
    if raw is None:
        return None
    if isinstance(raw, AgentThinkingMode):
        return raw
    if isinstance(raw, str):
        try:
            return AgentThinkingMode(raw)
        except ValueError:
            return None
    return None


def thinking_to_level(mode: AgentThinkingMode) -> ThinkingLevel:
    match mode:
        case AgentThinkingMode.OFF:
            return False
        case AgentThinkingMode.MINIMAL:
            return "minimal"
        case AgentThinkingMode.LOW:
            return "low"
        case AgentThinkingMode.MEDIUM:
            return "medium"
        case AgentThinkingMode.HIGH:
            return "high"
        case AgentThinkingMode.XHIGH:
            return "xhigh"


def resolve_model_settings(config: AgentConfig, *, session_thinking: AgentThinkingMode | None = None) -> ModelSettings:
    """始终注入 ``config.max_tokens`` (避免提供商默认过小导致 length 截断).

    ``max_retries`` 非 None 时按请求下发传输层重试次数 (``0`` 表示只发一次), 对 OpenAI / Anthropic 等
    各协议一并生效; None 时不传, 沿用 SDK 客户端自身的重试配置. ``session_thinking`` 为 None 表示继承
    ``config.thinking``; 有效值仍为 None 时不传 thinking.
    """
    settings: ModelSettings = {"max_tokens": config.max_tokens}
    if config.max_retries is not None:
        settings["max_retries"] = config.max_retries
    effective = config.thinking if session_thinking is None else session_thinking
    if effective is not None:
        settings["thinking"] = thinking_to_level(effective)
    return settings


def build_model(config: AgentConfig) -> Model:
    """不检查 api_key. 不传 ``http_client``: 传输客户端由 pydantic-ai 构造并托管, 与翻译共用协议映射."""
    return build_llm_model(
        config.api_type,
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.model,
    )


def build_agent(config: AgentConfig) -> Agent[AgentDeps, str | DeferredToolRequests] | None:
    """thinking / max_tokens 经每回合 model_settings 注入."""
    if not config.api_key:
        return None
    return Agent[AgentDeps, str | DeferredToolRequests](
        build_model(config),
        deps_type=AgentDeps,
        output_type=[str, DeferredToolRequests],
        # 使用 instructions 而非 system_prompt: Responses 协议把 agent instructions 放 API 顶层
        # instructions 字段, 服务端将其插在 input 之前; system_prompt 会变成 input 里的 system
        # 消息, 落到 capability 指令之后 —— 身份定位出现在各域注意事项之后.
        instructions=_SYSTEM + build_schema_docs(),
        retries=_AGENT_RETRIES,
        toolsets=[build_explore_toolset()],
        capabilities=[
            ToolNameAlias(),
            build_metadata_ops_capability(),
            build_actor_ops_capability(),
            build_facet_identity_capability(),
            build_library_ops_capability(),
            build_feed_ops_capability(),
            build_schedule_ops_capability(),
            build_task_ops_capability(),
        ],
    )
