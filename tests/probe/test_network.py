"""代理自检: 判据是"出口 IP 是否变化", 语法错误要在发请求前拦下."""

from __future__ import annotations

import pytest

from amane.config import HotSettings
from amane.probe import network as probe_module
from amane.probe.base import ProbeLevel
from amane.probe.network import _Trace, probe_network


def _hot(proxy: str | None) -> HotSettings:
    hot = HotSettings()
    hot.network.proxy = proxy
    return hot


def _stub(monkeypatch, direct: _Trace, proxied: _Trace | None = None) -> None:
    async def _fake_trace(proxy, timeout_s):
        return direct if proxy is None else (proxied or direct)

    monkeypatch.setattr(probe_module, "_trace", _fake_trace)


@pytest.mark.asyncio(loop_scope="function")
async def test_invalid_scheme_is_rejected_before_any_request(make_ctx, monkeypatch):
    async def _never(*args, **kwargs):
        raise AssertionError("地址不合法时不该发请求")

    monkeypatch.setattr(probe_module, "_trace", _never)

    events = [event async for event in probe_network(make_ctx(_hot("ftp://127.0.0.1:21")))]

    assert events[-1].level is ProbeLevel.ERROR
    assert "ftp" in (events[-1].detail or "")


@pytest.mark.asyncio(loop_scope="function")
async def test_proxy_changes_egress_ip(make_ctx, monkeypatch):
    _stub(
        monkeypatch,
        _Trace(ok=True, status=200, ip="1.1.1.1", elapsed_ms=120, error=None),
        _Trace(ok=True, status=200, ip="2.2.2.2", elapsed_ms=80, error=None),
    )

    events = [event async for event in probe_network(make_ctx(_hot("http://127.0.0.1:7890")))]

    assert events[-1].level is ProbeLevel.OK
    assert events[-1].message == "代理生效"
    assert "1.1.1.1" in (events[-1].detail or "")
    assert "2.2.2.2" in (events[-1].detail or "")


@pytest.mark.asyncio(loop_scope="function")
async def test_same_egress_ip_means_proxy_not_in_effect(make_ctx, monkeypatch):
    same = _Trace(ok=True, status=200, ip="9.9.9.9", elapsed_ms=100, error=None)
    _stub(monkeypatch, same, same)

    events = [event async for event in probe_network(make_ctx(_hot("socks5://127.0.0.1:1080")))]

    assert events[-1].level is ProbeLevel.WARN
    assert "未变化" in events[-1].message


@pytest.mark.asyncio(loop_scope="function")
async def test_proxy_failure_is_error_even_if_direct_works(make_ctx, monkeypatch):
    _stub(
        monkeypatch,
        _Trace(ok=True, status=200, ip="1.1.1.1", elapsed_ms=90, error=None),
        _Trace(ok=False, status=None, ip=None, elapsed_ms=2000, error="ConnectError: refused"),
    )

    events = [event async for event in probe_network(make_ctx(_hot("http://127.0.0.1:1")))]

    assert events[-1].level is ProbeLevel.ERROR
    assert "ConnectError" in (events[-1].detail or "")


@pytest.mark.asyncio(loop_scope="function")
async def test_direct_failure_without_proxy_is_error(make_ctx, monkeypatch):
    _stub(monkeypatch, _Trace(ok=False, status=None, ip=None, elapsed_ms=10, error="ConnectError: offline"))

    events = [event async for event in probe_network(make_ctx(_hot(None)))]

    assert [event.level for event in events] == [ProbeLevel.INFO, ProbeLevel.WARN, ProbeLevel.ERROR]
    assert "未配置代理" in events[-1].message


@pytest.mark.asyncio(loop_scope="function")
async def test_direct_works_without_proxy(make_ctx, monkeypatch):
    _stub(monkeypatch, _Trace(ok=True, status=200, ip="1.1.1.1", elapsed_ms=10, error=None))

    events = [event async for event in probe_network(make_ctx(_hot(None)))]

    assert events[-1].level is ProbeLevel.OK
    assert "直连" in (events[-1].detail or "")
