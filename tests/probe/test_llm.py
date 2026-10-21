"""LLM 端点自检: 缺凭据必须当场判死, 有凭据则走真实的 ``build_model`` 构造路径, 最后真跑功能."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ToolCallPart

from amane.config import HotSettings
from amane.enums import LLMAuthMode
from amane.llm.guest_token import DEVICE_ID_FILENAME, GuestTokenAuth
from amane.probe import llm as probe_module
from amane.probe.base import ProbeLevel
from amane.probe.llm import probe_agent, probe_llm


class _Usage:
    request_tokens = 12
    response_tokens = 3


class _Response:
    text = "就绪"

    def usage(self) -> _Usage:
        return _Usage()


class _Translated:
    """翻译那一步的响应: 没有 usage, 走"取不到就不显示"的分支."""

    text = "新人NO.1STYLE 专属出道 葵司"
    usage = None


class _Item:
    def __init__(self, id_: int, number: str, title: str | None) -> None:
        self.id = id_
        self.number = number
        self.title = title


class _Repo:
    def __init__(self, items: list[_Item]) -> None:
        self._items = items

    async def list_metadata(self, *, ids: list[int] | None = None, limit: int = 20, **kwargs: object):
        items = self._items if ids is None else [item for item in self._items if item.id in ids]
        return items[:limit], len(items)


class _Runtime:
    """最小运行时替身. 真实数据段只用到 repo 与 agent_service."""

    def __init__(self, items: list[_Item] | None = None, agent_service: object = None) -> None:
        self.repo = _Repo(items or [])
        self.agent_service = agent_service


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


@pytest.mark.asyncio(loop_scope="function")
async def test_guest_mode_needs_no_key_and_attaches_auth(make_ctx, tmp_path, monkeypatch):
    """游客模式不读 api_key: 判据换成 deviceId 换 token, 且鉴权必须挂到传输客户端上."""
    captured: dict[str, object] = {}

    async def _fake_request(model, messages):
        return _Response()

    def _capture(*args, **kwargs):
        captured.update(kwargs)
        return real_client(*args, **kwargs)

    real_client = probe_module.AsyncClient
    monkeypatch.setattr("amane.probe.llm.model_request", _fake_request)
    monkeypatch.setattr(probe_module, "AsyncClient", _capture)
    hot = HotSettings()
    hot.llm.auth_mode = LLMAuthMode.GUEST_TOKEN
    hot.llm.api_key = None
    hot.llm.model = "glm-4-flash"

    events = [event async for event in probe_llm(make_ctx(hot))]

    assert not [event for event in events if "API Key" in event.message]
    assert any("游客 token" in event.message for event in events)
    assert events[-1].level is ProbeLevel.OK
    assert isinstance(captured.get("auth"), GuestTokenAuth)
    # deviceId 落盘: 下次测试复用同一身份, 否则每次都要换新 token.
    assert (tmp_path / DEVICE_ID_FILENAME).is_file()


@pytest.mark.asyncio(loop_scope="function")
async def test_api_key_mode_attaches_no_auth_hook(make_ctx, monkeypatch):
    """密钥模式不该被游客鉴权覆盖: auth 必须保持 None, 否则配置里的 key 会被丢掉."""
    captured: dict[str, object] = {}

    async def _fake_request(model, messages):
        return _Response()

    def _capture(*args, **kwargs):
        captured.update(kwargs)
        return real_client(*args, **kwargs)

    real_client = probe_module.AsyncClient
    monkeypatch.setattr("amane.probe.llm.model_request", _fake_request)
    monkeypatch.setattr(probe_module, "AsyncClient", _capture)
    hot = HotSettings()
    hot.llm.api_key = "sk-test"
    hot.llm.model = "gpt-4o-mini"

    events = [event async for event in probe_llm(make_ctx(hot))]

    assert events[-1].level is ProbeLevel.OK
    assert captured.get("auth") is None


@pytest.mark.asyncio(loop_scope="function")
async def test_translation_effect_uses_library_title_and_shows_result(make_ctx, monkeypatch):
    """连通之后必须真翻一条: 样例取自库内条目, 事件里同时给出原文与译文."""
    calls = {"ping": 0, "translate": 0}

    async def _ping(model, messages):
        calls["ping"] += 1
        return _Response()

    async def _translate(model, messages):
        calls["translate"] += 1
        return _Translated()

    monkeypatch.setattr("amane.probe.llm.model_request", _ping)
    # 翻译走的是 translator 自己的入口, 只补上探针那一处会真的打到上游.
    monkeypatch.setattr("amane.llm.translator.model_request", _translate)
    hot = HotSettings()
    hot.llm.api_key = "sk-test"
    hot.llm.model = "gpt-4o-mini"
    runtime = _Runtime([_Item(1, "SSIS-001", "新人NO.1STYLE 専属デビュー 葵つかさ")])

    events = [event async for event in probe_llm(make_ctx(hot, runtime=runtime))]

    effect = [event for event in events if "真实翻译" in event.message]
    assert effect, "连通之后应补一条真实翻译"
    assert effect[-1].level is ProbeLevel.OK
    detail = effect[-1].detail or ""
    assert "葵つかさ" in detail, "原文要出现在结论里"
    assert "葵司" in detail, "译文要出现在结论里"
    assert "库内条目 SSIS-001" in detail, "样例来源必须如实标注"
    # 连通一次 + 翻译一次: 翻译既不能省掉, 也不能被缓存伪装成"还没测".
    assert calls == {"ping": 1, "translate": 1}


@pytest.mark.asyncio(loop_scope="function")
async def test_translation_effect_falls_back_and_labels_the_sample(make_ctx, monkeypatch):
    """库里没有可翻的标题时用内置样例, 且必须标注来源, 不能冒充库内数据."""

    async def _respond(model, messages):
        return _Translated()

    monkeypatch.setattr("amane.probe.llm.model_request", _respond)
    monkeypatch.setattr("amane.llm.translator.model_request", _respond)
    hot = HotSettings()
    hot.llm.api_key = "sk-test"
    hot.llm.model = "gpt-4o-mini"

    events = [event async for event in probe_llm(make_ctx(hot, runtime=_Runtime()))]

    effect = [event for event in events if "真实翻译" in event.message]
    assert effect and effect[-1].level is ProbeLevel.OK
    assert "内置样例" in (effect[-1].detail or "")


@pytest.mark.asyncio(loop_scope="function")
async def test_agent_effect_reports_a_real_turn_with_tool_calls(make_ctx, monkeypatch):
    """助理段要真跑一个回合: 回答与工具调用次数都要出现在结论里."""
    asked: dict[str, object] = {}

    class _Message:
        parts = (ToolCallPart(tool_name="sql_explore", args={"sql": "select 1"}, tool_call_id="call-1"),)

    class _Result:
        output = "ABP-123\nSSIS-001"

        def all_messages(self):
            return [_Message()]

    class _FakeAgent:
        async def run(self, prompt, deps):
            asked.update(prompt=prompt, deps=deps)
            return _Result()

    async def _fake_request(model, messages):
        return _Response()

    monkeypatch.setattr("amane.probe.llm.model_request", _fake_request)
    monkeypatch.setattr("amane.agent.runtime.build_agent", lambda config: _FakeAgent())
    hot = HotSettings()
    hot.agent.api_key = "sk-test"

    events = [
        event
        async for event in probe_agent(
            make_ctx(hot, runtime=_Runtime(agent_service=SimpleNamespace(executor=object())))
        )
    ]

    effect = [event for event in events if "真实回合" in event.message]
    assert effect and effect[-1].level is ProbeLevel.OK
    detail = effect[-1].detail or ""
    assert "工具调用 1 次" in detail
    assert "ABP-123" in detail
    assert "SELECT number FROM metadata" in str(asked["prompt"]), "回合必须迫使模型真查库"


@pytest.mark.asyncio(loop_scope="function")
async def test_agent_effect_warns_when_model_skips_tools(make_ctx, monkeypatch):
    """一次工具都没调用的回答是臆测, 不能当作通过: 这正是免费网关的失败形态."""

    class _Result:
        output = "库里有 3 部影片"

        def all_messages(self):
            return []

    class _FakeAgent:
        async def run(self, prompt, deps):
            return _Result()

    async def _fake_request(model, messages):
        return _Response()

    monkeypatch.setattr("amane.probe.llm.model_request", _fake_request)
    monkeypatch.setattr("amane.agent.runtime.build_agent", lambda config: _FakeAgent())
    hot = HotSettings()
    hot.agent.api_key = "sk-test"

    events = [
        event
        async for event in probe_agent(
            make_ctx(hot, runtime=_Runtime(agent_service=SimpleNamespace(executor=object())))
        )
    ]

    assert events[-1].level is ProbeLevel.WARN
    assert "没有调用任何工具" in events[-1].message
    assert "查库" in (events[-1].detail or "")
