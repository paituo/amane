"""LLM 端点自检: 与翻译 / 助理走完全相同的构造路径, 发一次最小请求.

不自己拼 HTTP: 只有经 ``build_model`` + ``model_request`` 才能覆盖 ``api_type`` 到各家 SDK 的
映射、代理与超时. "配置看着对但端点不认这个协议" (例如把 Anthropic 端点配成 chat) 这类问题
只有真发一次才暴露.

请求体固定为一句话: 目的是连通性与鉴权, 不是测模型能力, 输出越短越省额度 —— 真正的能力验证
放在最后一步 ``effect``: 翻译段真翻一条真实标题, 助理段真跑一个回合, 让"通了"变成"效果是什么".
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from time import perf_counter

import structlog
from httpx2 import AsyncClient, Timeout
from pydantic_ai.direct import model_request
from pydantic_ai.messages import ModelRequest, SystemPromptPart, ToolCallPart, UserPromptPart
from pydantic_ai.models import Model

from ..enums import ApiType, Language, LLMAuthMode, MetadataField
from ..llm.guest_token import DEVICE_ID_FILENAME, GUEST_API_KEY, GuestTokenAuth, load_or_create_device_id
from ..llm.model import build_model
from ..llm.translator import LLMTranslator
from ..utils.language import needs_llm_translation
from .base import ProbeContext, ProbeEvent, describe_error

logger = structlog.get_logger()

_REQUEST_TIMEOUT_S = 60.0
"""单次请求上限. 上游默认重试仍在 SDK 内, 这里给的是墙钟上限, 超时即判失败."""

_AGENT_TIMEOUT_S = 180.0
"""真实回合上限. 一个回合可能含多次工具往返, 比单次请求宽松; 超时只降级为 warn, 不判死."""

_PROMPT = "只回复两个字: 就绪"
_SYSTEM = "你是连通性测试助手, 只按用户要求回复, 不要解释."

_SAMPLE_TITLE = "新人NO.1STYLE 専属デビュー 葵つかさ"
"""内置翻译样例: 真实站点标题 (javbus 详情页原文). 库里没有可用日文标题时用它."""

_SAMPLE_SCAN = 20
"""取样例时最多扫多少条元数据. 只找一条可用标题, 不必翻全库."""

_AGENT_PROMPT = (
    "用 sql_explore 执行这段只读 SQL: SELECT number FROM metadata ORDER BY id DESC LIMIT 3。"
    "把查到的番号原样列出来, 一行一个; 没有数据就直说没有, 不要编造."
)
"""真实回合的提问: 逼模型必须真的查库 (而不是凭上下文作答), 结果可对着库核对."""

EffectFn = Callable[[ProbeContext, Model], AsyncIterator[ProbeEvent]]
"""真实数据段的实现. 拿到上面已经构造好的 ``Model``, 复用其传输客户端与鉴权."""


@dataclass(frozen=True, slots=True)
class _Endpoint:
    """被测端点. 两个 section 的字段名一致, 只是各自独立配置."""

    label: str
    api_type: ApiType
    auth_mode: LLMAuthMode
    base_url: str
    api_key: str | None
    model: str
    extra: str = ""


async def _probe(
    ctx: ProbeContext, endpoint: _Endpoint, *, effect: EffectFn | None = None
) -> AsyncIterator[ProbeEvent]:
    rec = ctx.rec
    proxy = ctx.hot.network.proxy or "直连"
    detail = f"协议 {endpoint.api_type} · 模型 {endpoint.model.strip() or '(未填写)'} · {proxy}"
    if endpoint.extra:
        detail = f"{detail} · {endpoint.extra}"
    yield rec.info(f"{endpoint.label}端点 {endpoint.base_url}", detail=detail)

    auth: GuestTokenAuth | None = None
    api_key = endpoint.api_key
    if endpoint.auth_mode is LLMAuthMode.GUEST_TOKEN:
        auth = GuestTokenAuth(load_or_create_device_id(ctx.data_dir / DEVICE_ID_FILENAME), proxy=ctx.hot.network.proxy)
        api_key = GUEST_API_KEY
        yield rec.info(
            "游客 token 模式",
            detail="不读配置里的密钥; 请求前用本机 deviceId 换取短期 token, 过期自动刷新",
        )
    elif not api_key:
        yield rec.error(
            "未配置 API Key",
            detail="凭据为空时请求不会发出: 翻译会被整段跳过, 助理会话也起不来",
        )
        return
    if not endpoint.model.strip():
        yield rec.error("未填写模型名", detail="空模型名会让 provider 在构造请求时才报错")
        return

    # 客户端归本探针所有: 与翻译路径同构 (proxy + 超时), 用完即关, 不挂到运行时身上.
    client = AsyncClient(
        auth=auth, proxy=ctx.hot.network.proxy, timeout=Timeout(_REQUEST_TIMEOUT_S), follow_redirects=True
    )
    try:
        model = build_model(
            endpoint.api_type,
            base_url=endpoint.base_url,
            api_key=api_key,
            model=endpoint.model,
            http_client=client,
        )
        yield rec.ok("客户端已构造", detail=f"{endpoint.api_type} → {type(model).__name__}")

        messages = [ModelRequest(parts=[SystemPromptPart(content=_SYSTEM), UserPromptPart(content=_PROMPT)])]
        try:
            response = await asyncio.wait_for(model_request(model, messages), timeout=_REQUEST_TIMEOUT_S + 5)
        except TimeoutError:
            yield rec.error(
                f"请求超时 ({_REQUEST_TIMEOUT_S:.0f}s)",
                detail="端点可达但没有在时限内返回; 自建端点可能是模型未加载或排队",
            )
            return
        except Exception as e:
            yield rec.error("请求失败", detail=describe_error(e))
            return

        text = (response.text or "").strip()
        yield rec.ok(f"收到响应: {text[:60] or '(空文本)'}", detail=_usage_detail(response))

        # 连通性只是前半: 真正的判据是这个端点跑起功能来的样子. effect 在这里才拿到 model,
        # 复用它手上那个已挂好鉴权的客户端, 不另起一份传输.
        if effect is not None and ctx.runtime is not None:
            async for event in effect(ctx, model):
                yield event
    finally:
        await client.aclose()


def _clip(text: str, limit: int = 240) -> str:
    """压成单行摘要. 样例原文/译文可能很长, detail 里只留可读的一段."""
    single = " ".join(text.split())
    return single if len(single) <= limit else f"{single[:limit]}…"


async def _sample_title(ctx: ProbeContext) -> tuple[str, str]:
    """取一条真实日文标题做翻译样例, 返回 (文本, 来源说明).

    优先用库里的条目: 用户看到的"效果"就是他自己那批数据翻出来的样子. 库里还没有可翻的标题
    (未刮削或已刮成中文) 时退回内置样例, 并如实标注来源, 不假装是库内数据.
    """
    runtime = ctx.runtime
    if runtime is not None:
        try:
            items, _ = await runtime.repo.list_metadata(limit=_SAMPLE_SCAN)
        except Exception as e:
            logger.warning("probe sample lookup failed", error=str(e))
            items = []
        for item in items:
            title = (item.title or "").strip()
            if title and needs_llm_translation(title, Language.ZH_CN):
                return title, f"库内条目 {item.number}"
    return _SAMPLE_TITLE, "内置样例"


async def _translation_effect(ctx: ProbeContext, model: Model) -> AsyncIterator[ProbeEvent]:
    """真实数据段: 用同一份配置真翻一条标题, 让译文本身成为证据.

    直接复用上面构造好的 ``model``: 鉴权、代理、超时都已经是翻译路径那一套, 另建一份只会多一个
    变量. 走 ``LLMTranslator`` 而不是裸请求, 是为了连提示词与输出约束一起验证; 不带缓存实例,
    测的永远是这一次真实调用.
    """
    cfg = ctx.hot.llm
    text, origin = await _sample_title(ctx)
    translator = LLMTranslator(
        model,
        rate_limit=cfg.rate_limit,
        system_prompt=cfg.system_prompt,
        field_prompts=cfg.field_prompts,
    )
    started = perf_counter()
    try:
        # use_cache=False: 命中缓存只是回放旧译文, 证明不了这条链路此刻是通的.
        translated = await translator.translate(text, Language.ZH_CN, MetadataField.TITLE, use_cache=False)
    except Exception as e:
        yield ctx.rec.warn("真实翻译失败", detail=describe_error(e))
        return

    elapsed = int((perf_counter() - started) * 1000)
    body = f"字段 title · 目标 简体中文 · 耗时 {elapsed} ms\n原文 ({origin}): {_clip(text)}"
    if not translated:
        yield ctx.rec.warn("真实翻译没有产出译文", detail=f"{body}\n模型返回空文本或只有思维链")
        return
    yield ctx.rec.ok("真实翻译成功", detail=f"{body}\n译文: {_clip(translated)}")


async def _agent_effect(ctx: ProbeContext, _model: Model) -> AsyncIterator[ProbeEvent]:
    """真实数据段: 真跑一个回合, 看工具是否被调用、回答与库里的数据是否对得上.

    不复用上面那条 ``model``: 助理的价值在工具集与 capabilities, 只有经 ``build_agent`` 装配
    才算真实. 因此这里会再建一个模型实例, 代价是一个回合的时间.
    """
    runtime = ctx.runtime
    service = getattr(runtime, "agent_service", None) if runtime is not None else None
    if runtime is None or service is None:
        return

    # 延迟导入: 只有这条真实回合才需要助理那一套 (工具集 / 沙箱 / 执行器).
    from ..agent.runtime import build_agent
    from ..agent.tools import AgentDeps

    cfg = ctx.hot.agent
    # 复用助理的限速器: 自检回合与真实回合共桶, 否则自检自己就会撞上 token 限额
    agent = build_agent(cfg, service.request_limiter())
    if agent is None:
        yield ctx.rec.warn("真实回合跳过", detail="凭据为空, 助理装配为空")
        return

    deps = AgentDeps(repo=runtime.repo, executor=service.executor, session_id=0, sql_timeout_ms=cfg.sql_timeout_ms)
    started = perf_counter()
    try:
        # 回合里可能有多轮工具往返, 单独给一个比单次请求宽松的上限.
        result = await asyncio.wait_for(agent.run(_AGENT_PROMPT, deps=deps), timeout=_AGENT_TIMEOUT_S)
    except TimeoutError:
        yield ctx.rec.warn(
            f"真实回合超时 ({_AGENT_TIMEOUT_S:.0f}s)",
            detail="助理没有在时限内答复: 上游太慢, 或工具往返卡住",
        )
        return
    except Exception as e:
        yield ctx.rec.warn("真实回合失败", detail=describe_error(e))
        return

    elapsed = int((perf_counter() - started) * 1000)
    output = str(result.output or "").strip()
    calls = _tool_call_count(result)
    detail = f"耗时 {elapsed} ms · 工具调用 {calls} 次\n回复: {_clip(output, 400) or '(空)'}"
    if calls == 0:
        yield ctx.rec.warn(
            "真实回合没有调用任何工具",
            detail=f"{detail}\n上面的回答没有经过查库: 该端点多半不透传 tool 定义, 助理会答非所问",
        )
        return
    yield ctx.rec.ok("真实回合完成", detail=detail)


def _tool_call_count(result: object) -> int:
    """统计回合里实际发出的工具调用数. 取不到消息就按 0 算 (判据退化为"没证据")."""
    all_messages = getattr(result, "all_messages", None)
    if not callable(all_messages):
        return 0
    messages = all_messages()
    if not isinstance(messages, (list, tuple)):
        return 0
    return sum(1 for message in messages for part in getattr(message, "parts", ()) if isinstance(part, ToolCallPart))


def _usage_detail(response: object) -> str | None:
    """token 用量只是佐证, 不同 provider 提供得不齐; 取不到就不显示, 不算失败."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    try:
        used = usage() if callable(usage) else usage
        request_tokens = getattr(used, "request_tokens", None)
        response_tokens = getattr(used, "response_tokens", None)
    except Exception:
        return None
    if request_tokens is None and response_tokens is None:
        return None
    return f"输入 {request_tokens or 0} / 输出 {response_tokens or 0} tokens"


async def probe_llm(ctx: ProbeContext) -> AsyncIterator[ProbeEvent]:
    """``llm`` section: 元数据翻译用的端点."""
    cfg = ctx.hot.llm
    fields = ", ".join(str(field) for field in cfg.translate_fields) or "(无)"
    async for event in _probe(
        ctx,
        _Endpoint(
            label="翻译",
            api_type=cfg.api_type,
            auth_mode=cfg.auth_mode,
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=cfg.model,
            extra=f"翻译字段 {fields} · 限速 {cfg.rate_limit:g}/s",
        ),
        effect=_translation_effect,
    ):
        yield event


async def probe_agent(ctx: ProbeContext) -> AsyncIterator[ProbeEvent]:
    """``agent`` section: 智能体会话用的端点, 与翻译的凭据/模型互相独立."""
    cfg = ctx.hot.agent
    thinking = f"思考 {cfg.thinking}" if cfg.thinking else "思考 (未指定)"
    async for event in _probe(
        ctx,
        _Endpoint(
            label="智能体",
            api_type=cfg.api_type,
            # agent section 没有免密钥档: 上游免费网关无工具调用通道, 助理只能用密钥.
            auth_mode=LLMAuthMode.API_KEY,
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=cfg.model,
            extra=f"输出上限 {cfg.max_tokens} · {thinking}",
        ),
        effect=_agent_effect,
    ):
        yield event
