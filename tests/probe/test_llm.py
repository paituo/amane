"""LLM 端点自检: 缺凭据必须当场判死, 有凭据则走真实的 ``build_model`` 构造路径."""

from __future__ import annotations

import pytest

from amane.config import HotSettings
from amane.probe.base import ProbeLevel
from amane.probe.llm import probe_agent, probe_llm


class _Usage:
    request_tokens = 12
    response_tokens = 3


class _Response:
    text = "就绪"

    def usage(self) -> _Usage:
        return _Usage()


@pytest.mark.asyncio(loop_scope="function")
async def test_missing_api_key_stops_before_any_request(make_ctx, monkeypatch):
    called = False

    async def _never(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("没有凭据时不该发请求")

    monkeypatch.setattr("amane.probe.llm.model_request", _never)
    events = [event async for event in probe_llm(make_ctx())]

    assert [event.level for event in events] == [ProbeLevel.INFO, ProbeLevel.ERROR]
    assert "API Key" in events[-1].message
    assert called is False


@pytest.mark.asyncio(loop_scope="function")
async def test_missing_model_is_rejected(make_ctx):
    hot = HotSettings()
    hot.llm.api_key = "sk-test"
    hot.llm.model = ""

    events = [event async for event in probe_llm(make_ctx(hot))]

    assert events[-1].level is ProbeLevel.ERROR
    assert "模型名" in events[-1].message


@pytest.mark.asyncio(loop_scope="function")
async def test_success_path_reports_model_class_and_usage(make_ctx, monkeypatch):
    seen: dict[str, object] = {}

    async def _fake_request(model, messages):
        seen["model"] = model
        seen["messages"] = messages
        return _Response()

    monkeypatch.setattr("amane.probe.llm.model_request", _fake_request)
    hot = HotSettings()
    hot.llm.api_key = "sk-test"
    hot.llm.model = "gpt-4o-mini"

    events = [event async for event in probe_llm(make_ctx(hot))]

    assert events[-1].level is ProbeLevel.OK
    assert "就绪" in events[-1].message
    assert "12" in (events[-1].detail or "")
    # api_type=chat 应映射到 chat 模型, 而不是 responses / anthropic.
    assert type(seen["model"]).__name__ == "OpenAIChatModel"
    assert seen["messages"]


@pytest.mark.asyncio(loop_scope="function")
async def test_upstream_error_is_surfaced_verbatim(make_ctx, monkeypatch):
    async def _boom(model, messages):
        raise RuntimeError("401 Unauthorized: invalid api key")

    monkeypatch.setattr("amane.probe.llm.model_request", _boom)
    hot = HotSettings()
    hot.llm.api_key = "sk-bad"
    hot.llm.model = "gpt-4o-mini"

    events = [event async for event in probe_llm(make_ctx(hot))]

    assert events[-1].level is ProbeLevel.ERROR
    assert "401" in (events[-1].detail or "")


@pytest.mark.asyncio(loop_scope="function")
async def test_agent_section_uses_its_own_credentials(make_ctx, monkeypatch):
    """agent 与 llm 的凭据互相独立: 只配 llm 不该让智能体测试通过."""

    async def _never(*args, **kwargs):
        raise AssertionError("agent 未配置 key, 不该发请求")

    monkeypatch.setattr("amane.probe.llm.model_request", _never)
    hot = HotSettings()
    hot.llm.api_key = "sk-translate-only"

    events = [event async for event in probe_agent(make_ctx(hot))]

    assert events[-1].level is ProbeLevel.ERROR
    assert "API Key" in events[-1].message
