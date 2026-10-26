"""助理限速: 频率 / token 预算 / 限流冷却.

时间靠假时钟推进 (`monotonic` 与 `asyncio.sleep` 一并替换), 预算窗口因此不必真等 60 秒.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic_ai.exceptions import ModelHTTPError, ModelRateLimitError
from pydantic_ai.messages import (
    InstructionPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RequestUsage, RunUsage

from amane.agent.limits import (
    ENVELOPE_OVERHEAD,
    AgentRateLimiter,
    RateLimitedModel,
    estimate_tokens,
)
from tests.agent.support import no_limit


class Clock:
    """可控时钟: `sleep` 只推进时间, 不真睡."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr("amane.agent.limits.monotonic", fake.monotonic)
    monkeypatch.setattr("amane.agent.limits.asyncio.sleep", fake.sleep)
    return fake


def _messages(text: str = "在吗") -> list[ModelMessage]:
    return [ModelRequest(parts=[UserPromptPart(content=text)])]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("", ENVELOPE_OVERHEAD), ("abcd", ENVELOPE_OVERHEAD + 1), ("一" * 25, ENVELOPE_OVERHEAD + 17)],
)
def test_estimate_tokens_scales_with_text(text: str, expected: int) -> None:
    """空消息只剩请求信封; 4 个西文字符约 1 token, 25 个汉字按 1.5 字/token 约合 17 token."""
    assert estimate_tokens(_messages(text)) == expected


def test_estimate_tokens_distinguishes_cjk_from_ascii() -> None:
    """同样字符数, 中日文的 token 数是西文的 2.7 倍: 用单一换算会把中文上下文低估到防不住 429."""
    assert estimate_tokens(_messages("一" * 2_400)) > estimate_tokens(_messages("a" * 2_400)) * 2


def test_estimate_tokens_counts_tool_declarations_and_instructions() -> None:
    """工具声明与指令按实际内容逐次计入: 它们不随对话变化, 但比正文还大, 漏掉就等于没设预算."""
    tool = ToolDefinition(
        name="sql_explore",
        description="d" * 400,
        parameters_json_schema={"type": "object", "properties": {"sql": {"type": "string"}}},
    )
    params = ModelRequestParameters(
        function_tools=[tool],
        instruction_parts=[InstructionPart(content="i" * 400)],
    )
    assert estimate_tokens(_messages(), params) - estimate_tokens(_messages()) >= 200


def test_estimate_tokens_covers_tool_payloads() -> None:
    """工具回执与调用参数同样计入: 助理的长上下文多半来自它们."""
    messages: list[ModelMessage] = [
        ModelRequest(parts=[ToolReturnPart(tool_name="sql_explore", content={"rows": ["x" * 400]})]),
        ModelResponse(parts=[ToolCallPart(tool_name="sql_deliver", args='{"sql": "' + "y" * 400 + '"}')]),
    ]
    assert estimate_tokens(messages) > ENVELOPE_OVERHEAD + 180


@pytest.mark.asyncio
async def test_acquire_deducts_estimate_up_front(clock: Clock) -> None:
    """请求前先预扣估算: 并发会话在响应回来前也看得到这笔开销."""
    limiter = AgentRateLimiter(rate=100.0, tpm=10_000, cooldown_s=0.0)
    await limiter.acquire(_messages())
    assert sum(tokens for _, tokens, _ in limiter._spent) == estimate_tokens(_messages())


@pytest.mark.asyncio
async def test_record_replaces_estimate_with_actual_usage(clock: Clock) -> None:
    """上游回了真实用量就替换估算: 估算偏大时窗口不会一直被高估占着."""
    limiter = AgentRateLimiter(rate=100.0, tpm=10_000, cooldown_s=0.0)
    await limiter.acquire(_messages())
    limiter.record(RequestUsage(input_tokens=900, output_tokens=100))
    assert sum(tokens for _, tokens, _ in limiter._spent) == 1000
    assert limiter._spent[-1][2] is True


@pytest.mark.asyncio
async def test_budget_waits_until_window_slides(clock: Clock) -> None:
    """预算不够时等到窗口内的记录滑出, 而不是立刻发请求去撞 429."""
    limiter = AgentRateLimiter(rate=100.0, tpm=1_000, cooldown_s=0.0)
    limiter._spent.append((clock.now - 55.0, 800, True))
    await limiter.acquire(_messages("一" * 500))  # 约 200 token + 工具声明
    assert clock.slept and pytest.approx(sum(clock.slept), abs=0.2) == 5.0


@pytest.mark.asyncio
async def test_single_request_over_budget_is_not_blocked_forever(clock: Clock) -> None:
    """单次估算就超过整窗预算时等到窗口清空即放行: 上游无论如何都会拒, 无限等待没有意义."""
    limiter = AgentRateLimiter(rate=100.0, tpm=10, cooldown_s=0.0)
    limiter._spent.append((clock.now - 1.0, 10, True))
    await asyncio.wait_for(limiter.acquire(_messages()), timeout=5)
    assert clock.slept


@pytest.mark.parametrize(
    ("cooldown_s", "retry_after", "expected"),
    [(60.0, None, 60.0), (0.0, None, 0.0), (60.0, 5.0, 5.0)],
)
@pytest.mark.asyncio
async def test_cooldown_blocks_next_request(
    clock: Clock, cooldown_s: float, retry_after: float | None, expected: float
) -> None:
    """冷却期内不放行; 上游给了 `Retry-After` 时以它为准."""
    limiter = AgentRateLimiter(rate=100.0, tpm=None, cooldown_s=cooldown_s)
    limiter.penalize(retry_after)
    await limiter.acquire(_messages())
    assert pytest.approx(sum(clock.slept), abs=0.1) == expected


@pytest.mark.asyncio
async def test_cooldown_expires(clock: Clock) -> None:
    """冷却过期后不再等待."""
    limiter = AgentRateLimiter(rate=100.0, tpm=None, cooldown_s=30.0)
    limiter.penalize()
    clock.now += 30.0
    await limiter.acquire(_messages())
    assert not clock.slept


class _RateLimited(FunctionModel):
    """恒定抛 429 的模型."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__(function=lambda messages, info: ModelResponse(parts=[TextPart("")]))
        self.retry_after = retry_after

    async def request(  # type: ignore[override]
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        raise ModelHTTPError.for_category(
            ModelRateLimitError,
            status_code=429,
            model_name="test-model",
            body={"message": "TPM limit reached."},
            retry_after=self.retry_after,
        )


@pytest.mark.asyncio
async def test_model_penalizes_on_rate_limit(clock: Clock) -> None:
    """429 触发冷却并继续上抛: 本侧重抛而不重发, 重试仍归 SDK."""
    limiter = AgentRateLimiter(rate=100.0, tpm=None, cooldown_s=60.0)
    model = RateLimitedModel(_RateLimited(), limiter)
    with pytest.raises(ModelRateLimitError):
        await model.request(_messages(), None, ModelRequestParameters())
    assert pytest.approx(limiter._cooldown_until - clock.now) == 60.0


@pytest.mark.asyncio
async def test_model_prefers_upstream_retry_after(clock: Clock) -> None:
    """上游给了 `Retry-After` 时不按配置值冷却."""
    limiter = AgentRateLimiter(rate=100.0, tpm=None, cooldown_s=60.0)
    model = RateLimitedModel(_RateLimited(retry_after=3.0), limiter)
    with pytest.raises(ModelRateLimitError):
        await model.request(_messages(), None, ModelRequestParameters())
    assert pytest.approx(limiter._cooldown_until - clock.now) == 3.0


@pytest.mark.asyncio
async def test_model_records_usage_after_success(clock: Clock) -> None:
    """成功响应用真实用量入账."""
    limiter = AgentRateLimiter(rate=100.0, tpm=None, cooldown_s=0.0)

    def respond(messages: list[ModelMessage], info: Any) -> ModelResponse:
        return ModelResponse(parts=[TextPart("好")], usage=RequestUsage(input_tokens=700, output_tokens=300))

    model = RateLimitedModel(FunctionModel(respond), limiter)
    await model.request(_messages(), None, ModelRequestParameters())
    assert sum(tokens for _, tokens, _ in limiter._spent) == 1000


@pytest.mark.asyncio
async def test_model_limits_stream_requests(clock: Clock) -> None:
    """流式路径同样过桶: 助理回合走 `request_stream`, 绕开它就等于没限速."""
    limiter = AgentRateLimiter(rate=100.0, tpm=1_000, cooldown_s=0.0)
    limiter._spent.append((clock.now - 55.0, 800, True))

    async def stream(messages: list[ModelMessage], info: Any) -> AsyncIterator[str]:
        yield "好"

    model = RateLimitedModel(FunctionModel(stream_function=stream), limiter)
    # 必须 `async with`: `Model.request_stream` 的契约就是上下文管理器 (基类与全部 provider
    # 都带 `@asynccontextmanager`). 用 `async for` 消费时, 实现即使漏了装饰器也一样能跑, 测试
    # 与实现错得一致就永远抓不到这个 bug.
    async with model.request_stream(_messages(), None, ModelRequestParameters()) as response:
        response.get()
    assert clock.slept


def test_request_stream_is_context_manager() -> None:
    """契约守卫: `request_stream` 返回的必须是上下文管理器, 不是裸 async generator.

    漏 `@asynccontextmanager` 时, 上游按契约 `async with` 会报
    `'async_generator' object does not support the asynchronous context manager protocol`.
    """

    async def stream(messages: list[ModelMessage], info: Any) -> AsyncIterator[str]:
        yield ""

    model = RateLimitedModel(FunctionModel(stream_function=stream), no_limit())
    stream_cm = model.request_stream(_messages(), None, ModelRequestParameters())
    assert hasattr(stream_cm, "__aenter__") and hasattr(stream_cm, "__aexit__"), (
        "request_stream 返回了 async generator; 需要 @asynccontextmanager"
    )


@pytest.mark.asyncio
async def test_stream_penalizes_on_rate_limit(clock: Clock) -> None:
    """流式路径同样在 429 后冷却: 助理回合全走流式, 漏掉这条等于没做冷却."""
    limiter = AgentRateLimiter(rate=100.0, tpm=None, cooldown_s=60.0)

    async def stream(messages: list[ModelMessage], info: Any) -> AsyncIterator[str]:
        raise ModelHTTPError.for_category(
            ModelRateLimitError, status_code=429, model_name="test-model", body={"message": "TPM limit reached."}
        )
        # 上一行必抛; 这个 yield 只是让它成为异步生成器
        yield ""

    model = RateLimitedModel(FunctionModel(stream_function=stream), limiter)
    with pytest.raises(ModelRateLimitError):
        async with model.request_stream(_messages(), None, ModelRequestParameters()):
            pass
    assert pytest.approx(limiter._cooldown_until - clock.now) == 60.0


def test_turn_usage_recording_accepts_run_usage() -> None:
    """回合用量与请求用量同构: 两者都有 input/output."""
    limiter = no_limit()
    limiter.record(RunUsage(input_tokens=10, output_tokens=5))
    assert limiter._spent[-1][1] == 15


def test_model_exposes_wrapped_model() -> None:
    """包装后仍是 Model: 装配与自检按 Model 用它."""
    model = RateLimitedModel(FunctionModel(function=lambda m, i: ModelResponse(parts=[TextPart("")])), no_limit())
    assert isinstance(model, Model)
