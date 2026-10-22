"""共享 ``Model`` 工厂: 协议映射与传输客户端归属."""

import asyncio

import httpx2
import pytest
from pydantic_ai.direct import model_request
from pydantic_ai.exceptions import ModelRateLimitError, ModelRequestTimeout
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel

from amane.enums import ApiType
from amane.llm import build_model

_ENDPOINT = "https://api.example/v1"

_CHAT_COMPLETION = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "test-model",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("with_http_client", [False, True])
@pytest.mark.parametrize(
    ("api_type", "model_cls"),
    [
        (ApiType.CHAT, OpenAIChatModel),
        (ApiType.RESPONSE, OpenAIResponsesModel),
        (ApiType.ANTHROPIC, AnthropicModel),
    ],
)
async def test_build_model_by_api_type(api_type: ApiType, model_cls: type[Model], with_http_client: bool) -> None:
    """三协议各自映射到对应模型类; 传入与不传传输客户端 (助理路径) 都必须构造成功."""
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(60.0)) as client:
        model = build_model(
            api_type,
            base_url=_ENDPOINT,
            api_key="test-key",
            model="test-model",
            http_client=client if with_http_client else None,
        )
    assert isinstance(model, model_cls)


@pytest.mark.asyncio
async def test_build_model_request_uses_injected_client() -> None:
    """传入传输客户端时请求必须经它发出. proxy 与超时都挂在它上面, 被 provider 忽略即静默失效."""
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=_CHAT_COMPLETION)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        model = build_model(
            ApiType.CHAT, base_url=_ENDPOINT, api_key="test-key", model="test-model", http_client=client
        )
        response = await model_request(model, [ModelRequest(parts=[UserPromptPart(content="hi")])])

    assert [str(request.url) for request in requests] == [f"{_ENDPOINT}/chat/completions"]
    assert response.text == "OK"


_ANTHROPIC_MESSAGE = {
    "id": "msg-test",
    "type": "message",
    "role": "assistant",
    "model": "test-model",
    "content": [{"type": "text", "text": "OK"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


@pytest.mark.asyncio
async def test_anthropic_request_path_reaches_endpoint() -> None:
    """Anthropic 协议必须真的走到发请求这一步.

    ``AnthropicModel`` 构造成功不代表可用: 消息映射在发出请求前执行, 依赖 anthropic SDK 的
    ``BetaMessageParam`` 等名字可被调用. SDK 把其中某个改成 ``TypeAlias`` 时, 构造仍然成功,
    首个回合才抛 ``TypeError``, 只有覆盖请求路径的用例能发现.
    """
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=_ANTHROPIC_MESSAGE)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        model = build_model(
            ApiType.ANTHROPIC, base_url=_ENDPOINT, api_key="test-key", model="test-model", http_client=client
        )
        response = await model_request(model, [ModelRequest(parts=[UserPromptPart(content="hi")])])

    assert [str(request.url).split("?")[0] for request in requests] == [f"{_ENDPOINT}/v1/messages"]
    assert response.text == "OK"


def _rate_limited(request: httpx2.Request) -> httpx2.Response:
    """恒定 429; ``retry-after: 0`` 让 SDK 重试不退避, 用例才能快."""
    return httpx2.Response(
        429,
        headers={"retry-after": "0"},
        json={"error": {"message": "rate limited", "type": "rate_limit_error"}},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("max_retries", [0, 2])
@pytest.mark.parametrize("api_type", [ApiType.CHAT, ApiType.ANTHROPIC])
async def test_max_retries_applies_per_request(api_type: ApiType, max_retries: int) -> None:
    """``ModelSettings['max_retries']`` 按协议分别生效: 预算 N 对应实发 N + 1 次请求.

    该设置只改重试次数, 由 SDK 决定哪些失败可重试, 因此两支都必须以 429 收尾.
    """
    attempts = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts["n"] += 1
        return _rate_limited(request)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        model = build_model(api_type, base_url=_ENDPOINT, api_key="test-key", model="test-model", http_client=client)
        with pytest.raises(ModelRateLimitError):
            await model_request(
                model, [ModelRequest(parts=[UserPromptPart(content="hi")])], model_settings={"max_retries": max_retries}
            )

    assert attempts["n"] == max_retries + 1, f"实发 {attempts['n']} 次"


@pytest.mark.asyncio
async def test_request_timeout_cancels_slow_request() -> None:
    """``ModelSettings['request_timeout']`` 是整请求墙钟上限, 到点中止而不等上游返回."""

    async def handler(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)
        return httpx2.Response(200, json=_CHAT_COMPLETION)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        model = build_model(
            ApiType.CHAT, base_url=_ENDPOINT, api_key="test-key", model="test-model", http_client=client
        )
        started = asyncio.get_running_loop().time()
        with pytest.raises(ModelRequestTimeout):
            await model_request(
                model,
                [ModelRequest(parts=[UserPromptPart(content="hi")])],
                model_settings={"request_timeout": 0.2, "max_retries": 0},
            )
        elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 3, f"未在墙钟上限内中止, 耗时 {elapsed:.2f}s"
