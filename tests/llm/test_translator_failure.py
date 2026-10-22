"""LLM 翻译失败分类与降噪.

上游对某些题材恒定返回内容审核拒绝: 同一文本重发必然还被拒, 重试只白等一次约 12 秒.
这里锁定三件事:

- ``classify_llm_failure`` 把「再试没意义」与「换个时机可能好」分开;
- 连续确定性失败达到阈值后短路后续请求, 冷却结束放行探针探活;
- 命中限定词黑名单时根本不发请求.
"""

from collections.abc import Sequence

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from amane.enums import Language, LLMFailureKind, MetadataField
from amane.llm import LLMTranslator
from amane.llm.translator import _DeterministicFailureGate, classify_llm_failure

_TEXT = "こんにちは世界"


def _policy_error(body: object = None) -> ModelHTTPError:
    """上游内容审核拒绝: 实测 glm-4-flash 恒返 400 + ``{"code": "1301"}``."""
    return ModelHTTPError(
        400, "glm-4-flash", {"error": {"code": "1301", "message": "内容审核"}} if body is None else body
    )


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        # 内容审核: 确定性, 重试无意义.
        (_policy_error(), LLMFailureKind.CONTENT_POLICY),
        (ModelHTTPError(400, "m", "content management policy violation"), LLMFailureKind.CONTENT_POLICY),
        (ModelHTTPError(400, "m", "系统检测到输入或生成内容存在风险"), LLMFailureKind.CONTENT_POLICY),
        # 凭据 / 参数: 同样确定性.
        (ModelHTTPError(401, "m", "no auth"), LLMFailureKind.AUTH),
        (ModelHTTPError(403, "m", "forbidden"), LLMFailureKind.AUTH),
        (ModelHTTPError(400, "m", "bad request"), LLMFailureKind.BAD_REQUEST),
        # 服务端抖动: 换个时机可能成功.
        (ModelHTTPError(429, "m", "slow down"), LLMFailureKind.TRANSIENT),
        (ModelHTTPError(500, "m", "boom"), LLMFailureKind.TRANSIENT),
        (ModelHTTPError(503, "m", "unavailable"), LLMFailureKind.TRANSIENT),
        (TimeoutError(), LLMFailureKind.TRANSIENT),
        (ValueError("unexpected"), LLMFailureKind.UNKNOWN),
    ],
)
def test_classify_llm_failure(exc: BaseException, expected: LLMFailureKind):
    assert classify_llm_failure(exc) is expected


def test_gate_opens_after_threshold() -> None:
    now = [0.0]
    gate = _DeterministicFailureGate(threshold=2, cooldown_s=60.0, clock=lambda: now[0])

    gate.record(LLMFailureKind.CONTENT_POLICY)
    assert not gate.open  # 一次失败不足以认定「这条路走不通」
    gate.record(LLMFailureKind.CONTENT_POLICY)
    assert gate.open


def test_gate_ignores_transient_failures() -> None:
    """可恢复失败不能累计成「确定性拒绝」, 否则一次网络抖动就会让翻译停摆."""
    now = [0.0]
    gate = _DeterministicFailureGate(threshold=2, cooldown_s=60.0, clock=lambda: now[0])

    gate.record(LLMFailureKind.TRANSIENT)
    gate.record(LLMFailureKind.TRANSIENT)
    gate.record(LLMFailureKind.UNKNOWN)
    assert not gate.open


def test_gate_closes_after_cooldown_and_resets_on_success() -> None:
    now = [0.0]
    gate = _DeterministicFailureGate(threshold=1, cooldown_s=60.0, clock=lambda: now[0])

    gate.record(LLMFailureKind.AUTH)
    assert gate.open

    now[0] = 61.0
    assert not gate.open  # 冷却结束放行探针

    gate.record(LLMFailureKind.AUTH)  # 探针仍失败: 重新关门
    assert gate.open


def _translator(
    *,
    result: str | None = "TRANSLATED",
    raise_exc: BaseException | None = None,
    skip_keywords: Sequence[str] = (),
    gate: _DeterministicFailureGate | None = None,
) -> tuple[LLMTranslator, list[str]]:
    calls: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(_TEXT)
        if raise_exc is not None:
            raise raise_exc
        return ModelResponse(parts=[TextPart(result or "")])

    translator = LLMTranslator(
        FunctionModel(respond),
        rate_limit=100.0,
        skip_keywords=skip_keywords,
        gate=gate,
    )
    return translator, calls


@pytest.mark.asyncio
async def test_skip_keyword_avoids_the_request_entirely() -> None:
    """命中限定词时连请求都不发: 这类文本在上游必然被拒, 发出去只买一次 12 秒等待."""
    translator, calls = _translator(skip_keywords=["こんにちは"])

    assert await translator.translate(_TEXT, Language.ZH_CN, MetadataField.PLOT) is None
    assert calls == []


@pytest.mark.asyncio
async def test_skip_keyword_is_case_insensitive() -> None:
    translator, calls = _translator(skip_keywords=["HELLO"])

    assert await translator.translate("hello world", Language.ZH_CN, MetadataField.PLOT) is None
    assert calls == []


@pytest.mark.asyncio
async def test_skip_keyword_miss_still_translates() -> None:
    translator, calls = _translator(skip_keywords=["別の語"])

    assert await translator.translate(_TEXT, Language.ZH_CN, MetadataField.PLOT) == "TRANSLATED"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_open_gate_short_circuits_without_calling_model() -> None:
    now = [0.0]
    gate = _DeterministicFailureGate(threshold=1, cooldown_s=600.0, clock=lambda: now[0])
    gate.record(LLMFailureKind.CONTENT_POLICY)
    assert gate.open

    translator, calls = _translator(gate=gate)

    assert await translator.translate(_TEXT, Language.ZH_CN, MetadataField.PLOT) is None
    assert calls == []


@pytest.mark.asyncio
async def test_repeated_content_policy_failures_trip_the_gate() -> None:
    """三次审核拒绝之后, 第四次翻译不再打上游."""
    gate = _DeterministicFailureGate(threshold=3, cooldown_s=600.0)
    translator, calls = _translator(raise_exc=_policy_error(), gate=gate)

    for _ in range(3):
        assert await translator.translate(_TEXT, Language.ZH_CN, MetadataField.PLOT) is None
    assert len(calls) == 3
    assert gate.open

    assert await translator.translate(_TEXT, Language.ZH_CN, MetadataField.PLOT) is None
    assert len(calls) == 3  # 第四次被闸门挡住
