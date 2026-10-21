"""LLM 端点自检: 与翻译 / 助理走完全相同的构造路径, 发一次最小请求.

不自己拼 HTTP: 只有经 ``build_model`` + ``model_request`` 才能覆盖 ``api_type`` 到各家 SDK 的
映射、代理与超时. "配置看着对但端点不认这个协议" (例如把 Anthropic 端点配成 chat) 这类问题
只有真发一次才暴露.

请求体固定为一句话: 目的是连通性与鉴权, 不是测模型能力, 输出越短越省额度.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import structlog
from httpx2 import AsyncClient, Timeout
from pydantic_ai.direct import model_request
from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart

from ..enums import ApiType
from ..llm.model import build_model
from .base import ProbeContext, ProbeEvent, describe_error

logger = structlog.get_logger()

_REQUEST_TIMEOUT_S = 60.0
"""单次请求上限. 上游默认重试仍在 SDK 内, 这里给的是墙钟上限, 超时即判失败."""

_PROMPT = "只回复两个字: 就绪"
_SYSTEM = "你是连通性测试助手, 只按用户要求回复, 不要解释."


@dataclass(frozen=True, slots=True)
class _Endpoint:
    """被测端点. 两个 section 的字段名一致, 只是各自独立配置."""

    label: str
    api_type: ApiType
    base_url: str
    api_key: str | None
    model: str
    extra: str = ""


async def _probe(ctx: ProbeContext, endpoint: _Endpoint) -> AsyncIterator[ProbeEvent]:
    rec = ctx.rec
    proxy = ctx.hot.network.proxy or "直连"
    detail = f"协议 {endpoint.api_type} · 模型 {endpoint.model.strip() or '(未填写)'} · {proxy}"
    if endpoint.extra:
        detail = f"{detail} · {endpoint.extra}"
    yield rec.info(f"{endpoint.label}端点 {endpoint.base_url}", detail=detail)

    if not endpoint.api_key:
        yield rec.error(
            "未配置 API Key",
            detail="凭据为空时请求不会发出: 翻译会被整段跳过, 助理会话也起不来",
        )
        return
    if not endpoint.model.strip():
        yield rec.error("未填写模型名", detail="空模型名会让 provider 在构造请求时才报错")
        return

    # 客户端归本探针所有: 与翻译路径同构 (proxy + 超时), 用完即关, 不挂到运行时身上.
    client = AsyncClient(proxy=ctx.hot.network.proxy, timeout=Timeout(_REQUEST_TIMEOUT_S), follow_redirects=True)
    try:
        model = build_model(
            endpoint.api_type,
            base_url=endpoint.base_url,
            api_key=endpoint.api_key,
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
    finally:
        await client.aclose()


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
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=cfg.model,
            extra=f"翻译字段 {fields} · 限速 {cfg.rate_limit:g}/s",
        ),
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
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=cfg.model,
            extra=f"输出上限 {cfg.max_tokens} · {thinking}",
        ),
    ):
        yield event
