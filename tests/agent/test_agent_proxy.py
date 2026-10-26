"""助理传输层: 生效代理的解析与客户端归属.

对照的线上故障: 同一端点翻译能通、助理恒报 ``Connection error.``. 根因不是网络, 是助理此前把传输
客户端交给 pydantic-ai 托管 —— 默认客户端 ``trust_env=True``, 只认环境变量 ``HTTP_PROXY`` /
``HTTPS_PROXY``, 设置页里配的代理对它完全无效. 这里钉住三件事: 解析规则、``trust_env`` 关闭、
注入的客户端确实被交给 provider (没有另建一个偷偷直连的).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.models.test import TestModel

from amane.agent.cache import ResultCache
from amane.agent.runtime import build_agent, build_http_client
from amane.agent.service import AgentService
from amane.config import AgentConfig
from tests.agent.support import no_limit, stub_client

_PROXY = "socks5h://127.0.0.1:10808"
"""与设置页里填的全局代理同形: 带协议, 否则无法区分 http 与 socks."""


@pytest.mark.parametrize(
    ("config", "fallback", "expected"),
    [
        # 默认开关打开且未填专用地址: 回退全局 —— 与站点级 use_proxy 的语义一致
        (AgentConfig(), _PROXY, _PROXY),
        (AgentConfig(proxy="http://127.0.0.1:7890"), _PROXY, "http://127.0.0.1:7890"),
        # 总闸优先于专用地址: 关掉就是直连, 留着地址也不生效
        (AgentConfig(use_proxy=False), _PROXY, None),
        (AgentConfig(use_proxy=False, proxy="http://127.0.0.1:7890"), _PROXY, None),
        # 两处都没有: 直连
        (AgentConfig(), None, None),
    ],
)
def test_resolved_proxy(config: AgentConfig, fallback: str | None, expected: str | None) -> None:
    assert config.resolved_proxy(fallback) == expected


@pytest.mark.asyncio
async def test_http_client_ignores_environment_proxy() -> None:
    """关掉 ``trust_env``: 「不使用代理」必须是真直连, 而不是"环境变量还在生效"."""
    client = build_http_client(None)
    try:
        assert client._trust_env is False
        assert not client._mounts
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_http_client_mounts_configured_proxy() -> None:
    """配了代理就必须挂到客户端上, 且 ``socks5h`` 要真的当 SOCKS 用.

    只断言"挂载表非空"不够: ``socks5h://`` 若被当成 HTTP 代理处理, httpx 会对 SOCKS 端口发
    ``CONNECT``, 换来的是一句与"代理没起"分不清的连接错误. httpcore 按 scheme 选连接池, 这里钉住
    它选中的是 SOCKS 池.
    """
    client = build_http_client(_PROXY)
    try:
        assert client._trust_env is False
        transport = next(iter(client._mounts.values()))
        assert type(getattr(transport, "_pool", None)).__name__ == "AsyncSOCKSProxy"
    finally:
        await client.aclose()


def test_agent_handles_injected_client_to_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """装配时传入的客户端必须原样交给 provider.

    另建一个默认客户端是最隐蔽的退化: 页面配了代理、请求却直连, 而现象与"代理坏了"一模一样.
    """
    captured: dict[str, Any] = {}

    def fake_build(
        api_type: Any, *, base_url: str, api_key: str | None, model: str, http_client: Any = None
    ) -> TestModel:
        captured["client"] = http_client
        return TestModel()

    monkeypatch.setattr("amane.agent.runtime.build_llm_model", fake_build)
    client = stub_client(_PROXY)

    agent = build_agent(AgentConfig(api_key="sk-test"), no_limit(), http_client=client)

    assert agent is not None
    assert captured["client"] is client


@pytest.mark.asyncio(loop_scope="function")
async def test_rebuild_keeps_old_client_open_for_inflight_turns(repo, tmp_path) -> None:
    """保存设置触发 rebuild 时, 旧客户端不能立刻关.

    另一个标签页可能正在跑回合, 它手上的还是旧 client; 就地 ``aclose`` 会让那边突然失败,
    用户看到的是"改完设置对话就断了".
    """
    service = AgentService(
        db_path=tmp_path / "amane.db",
        data_dir=tmp_path,
        repo=repo,
        cache=ResultCache(),
        config=AgentConfig(api_key="sk-test", proxy="http://127.0.0.1:1080"),
    )
    first = service._http_client
    assert first is not None

    service.rebuild(AgentConfig(api_key="sk-test", proxy="http://127.0.0.1:1081"), None)
    second = service._http_client
    assert second is not None and second is not first
    assert first.is_closed is False
    assert first in service._retired

    await service.aclose()
    assert first.is_closed is True
    assert second.is_closed is True


@pytest.mark.asyncio(loop_scope="function")
async def test_rebuild_without_api_key_releases_the_client(repo, tmp_path) -> None:
    """清空密钥后不该继续留着连接池, 也不该留下能发请求的 agent."""
    service = AgentService(
        db_path=tmp_path / "amane.db",
        data_dir=tmp_path,
        repo=repo,
        cache=ResultCache(),
        config=AgentConfig(api_key="sk-test"),
    )
    assert service._http_client is not None

    service.rebuild(AgentConfig(api_key=None), None)

    assert service._http_client is None
    assert service.agent is None
    assert service.naming_model is None
    await service.aclose()
