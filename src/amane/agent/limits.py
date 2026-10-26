"""助理模型请求的限速: 频率 / token 预算 / 限流冷却共用一份状态.

三者合成一个对象: 三项配置都约束同一个上游账户, 分开实现时并发会话各自放行, 上游看到的
速率是配置值的数倍.

限速挂在 model 上而不是回合的能力钩子上: 会话标题请求不经过 agent 回合 (见
`agent.naming`), 只有挂在 model 上它才能与回合共用同一个桶; 首条用户输入时两者并行,
上游按并发计量算两次.

token 预算按**预扣 + 校正**记账: 请求前按估算预扣, 响应后用上游回的真实用量校正. 上游不
回用量时保留估算值, 否则窗口会漏记整轮开销.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections import deque
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from aiolimiter import AsyncLimiter
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRateLimitError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequestPart,
    ModelResponse,
    ModelResponsePart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage, RunUsage

WINDOW_S = 60.0
"""token 预算的滑动窗口长度 (秒). 上游按分钟计量, 故与之一致."""

CHARS_PER_TOKEN = 4.0
"""非中日文的字符到 token 换算: 拉丁/数字/标点约 4 字符 1 token."""

CJK_CHARS_PER_TOKEN = 1.5
"""中日文的字符到 token 换算: 汉字与假名约 1.5 字 1 token (比按西文折算低估 2.7 倍, 故单列)."""

ENVELOPE_OVERHEAD = 200
"""请求信封的固定开销 (角色字段、JSON 框架). 指令与工具声明按实际内容计入, 不在此估."""


def _text_tokens(text: str) -> int:
    """按字符估算 token, 中日文与西文分别折算."""
    cjk = sum(1 for ch in text if "\u2e80" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef")
    return math.ceil(cjk / CJK_CHARS_PER_TOKEN + (len(text) - cjk) / CHARS_PER_TOKEN)


def _part_text(part: ModelRequestPart | ModelResponsePart) -> str:
    """参与 token 估算的文本; 结构化载荷按 JSON 计."""
    match part:
        case SystemPromptPart(content=content) | UserPromptPart(content=content) | TextPart(content=content):
            return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        case ThinkingPart(content=content):
            return content
        case ToolReturnPart(content=content):
            return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        case ToolCallPart(args=args):
            return args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        case _:
            return ""


def _tool_tokens(params: ModelRequestParameters | None) -> int:
    """工具声明与指令的 token 数.

    这两项是请求里最大的一块 (助理全量声明工具, 光 explore 三个工具的参数 schema 就两万字符),
    且不随对话变化, 逐次全额计入预算. 取自已解析的请求参数而不是写死常量: 能力的工具在运行时
    才并入, 常量估不出真实值.
    """
    if params is None:
        return 0
    tools = [*params.function_tools, *params.output_tools]
    payload = sum(
        len(tool.name)
        + len(tool.description or "")
        + len(json.dumps(tool.parameters_json_schema, separators=(",", ":")))
        for tool in tools
    )
    instructions = sum(len(part.content) for part in params.instruction_parts or ())
    return math.ceil((payload + instructions) / CHARS_PER_TOKEN)


def estimate_tokens(messages: Sequence[ModelMessage], params: ModelRequestParameters | None = None) -> int:
    """估算一次请求的 token 数; 只用于没有实测值可依时的首次请求.

    ``messages`` 按内容折算, ``params`` 里的工具声明与指令按内容折算. 工具 schema 是 JSON,
    按字符/4 计足以用于预算; 偏保守的一侧留给 ``record`` 用上游真实用量校正.
    """
    text = "".join(_part_text(part) for message in messages for part in message.parts)
    return _text_tokens(text) + _tool_tokens(params) + ENVELOPE_OVERHEAD


def _usage_of(response: StreamedResponse) -> RequestUsage:
    """取流式响应的用量.

    ``StreamedResponse`` 把它声明为方法, 但已完成响应的实现给的是属性, 故两种都要接.
    """
    raw = response.usage
    return raw if isinstance(raw, RequestUsage) else raw()


@dataclass
class AgentRateLimiter:
    """助理模型请求的共享限速状态: 频率桶 + token 预算窗口 + 限流冷却."""

    rate: float
    """每秒请求数. 桶容量 1, 严格平滑."""

    tpm: int | None
    """每分钟 token 上限 (输入 + 输出). None 表示不按 token 限速."""

    cooldown_s: float
    """收到限流后的冷却秒数; 期间不放行任何请求. 0 表示不冷却."""

    _gate: AsyncLimiter = field(init=False, repr=False)
    # (时刻, token 数, 是否已按上游实测校正)
    _spent: deque[tuple[float, int, bool]] = field(default_factory=deque, init=False, repr=False)
    _cooldown_until: float = field(default=0.0, init=False, repr=False)
    _last_input: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._gate = AsyncLimiter(1, 1 / self.rate)

    def _evict(self, now: float) -> None:
        """丢掉滑出窗口的记录."""
        while self._spent and self._spent[0][0] + WINDOW_S <= now:
            self._spent.popleft()

    async def acquire(self, messages: Sequence[ModelMessage], params: ModelRequestParameters | None = None) -> None:
        """放行一次请求: 等冷却结束 → 等频率桶 → 等 token 预算腾出, 然后按估算预扣.

        预算不足时等到窗口内的记录滑出为止. 单次估算即超过 ``tpm`` 时等到窗口清空即放行:
        上游此时无论如何都会拒绝, 无限等待没有意义, 由冷却与回合超时兜底.
        """
        # 历史只增不减, 故上次实测的 input 是本次的下限; 首轮没有实测值时退回字符估算
        need = max(estimate_tokens(messages, params), self._last_input)
        while True:
            wait = self._cooldown_until - monotonic()
            if wait <= 0:
                break
            await asyncio.sleep(wait)
        await self._gate.acquire()
        if self.tpm is not None:
            await self._wait_for_budget(need, self.tpm)
        self._spent.append((monotonic(), need, False))

    async def _wait_for_budget(self, need: int, tpm: int) -> None:
        """等到窗口内的已用量降到 ``tpm - need`` 以下."""
        while True:
            now = monotonic()
            self._evict(now)
            used = sum(tokens for _, tokens, _ in self._spent)
            if used + need <= tpm or not self._spent:
                return
            # 最早的一条滑出窗口后重新判断; 它必然未过期 (_evict 刚清过), 故等待时间恒为正
            await asyncio.sleep(min(self._spent[0][0] + WINDOW_S - now, WINDOW_S))

    def record(self, usage: RequestUsage | RunUsage) -> None:
        """用上游回的真实用量校正最近一次预扣."""
        total = usage.input_tokens + usage.output_tokens
        self._last_input = usage.input_tokens
        for index in range(len(self._spent) - 1, -1, -1):
            at, _, settled = self._spent[index]
            if not settled:
                self._spent[index] = (at, total, True)
                return
        self._spent.append((monotonic(), total, True))

    def penalize(self, retry_after: float | None = None) -> None:
        """记一次限流: 冷却期内不放行. 不重发请求, 重试仍归 SDK 与本侧回合之外.

        ``retry_after`` 取上游给的 ``Retry-After`` (pydantic-ai 从响应头解析后挂在异常上);
        上游没给时退回配置的 ``cooldown_s``.
        """
        seconds = self.cooldown_s if retry_after is None else retry_after
        if seconds > 0:
            self._cooldown_until = monotonic() + seconds


class RateLimitedModel(WrapperModel):
    """给任意 model 套上共享限速: 请求前过桶, 限流后冷却, 响应后记账."""

    def __init__(self, wrapped: Model, limiter: AgentRateLimiter) -> None:
        # 基类签名还接受 KnownModelName, 但本侧一律传入已构造的 Model (见 runtime.build_model):
        # 收窄到 Model 才能让"包装器本身就是 model"这件事在类型上成立, 不需要 model 名字解析.
        super().__init__(wrapped)
        self.limiter = limiter

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        await self.limiter.acquire(messages, model_request_parameters)
        try:
            response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        except ModelRateLimitError as exc:
            self.limiter.penalize(exc.retry_after)
            raise
        self.limiter.record(response.usage)
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        """流式路径同样过桶.

        ``@asynccontextmanager`` 不是可选装饰: ``Model.request_stream`` 的契约定在这里 —— 基类
        （`models/__init__.py::Model` 与 `models/wrapper.py::WrapperModel`）以及全部 18 个
        provider 实现都是 ``asynccontextmanager``. 少了它本方法就成了裸 async generator, 上游
        一旦按契约 ``async with`` 消费即报 ``'async_generator' object does not support the
        asynchronous context manager protocol``. 测试若也用 ``async for`` 消费, 两边错得一致,
        照样全绿 —— 故 `tests/agent/test_limits.py` 断言的必须是 ``async with``.
        """
        await self.limiter.acquire(messages, model_request_parameters)
        try:
            async with self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as response:
                yield response
            # 退出流后再读用量: 让出期间流已被消费完
            self.limiter.record(_usage_of(response))
        except ModelRateLimitError as exc:
            self.limiter.penalize(exc.retry_after)
            raise
