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


@pytest.mark.asyncio(loop_scope="function")
async def test_real_fetch_shows_the_page_actually_returned(make_ctx, monkeypatch):
    """出口 IP 对了还要证明能拿到数据: 最后用真实番号抓一页, 把页面标题摊出来."""
    _stub(
        monkeypatch,
        _Trace(ok=True, status=200, ip="1.1.1.1", elapsed_ms=10, error=None),
        _Trace(ok=True, status=200, ip="2.2.2.2", elapsed_ms=10, error=None),
    )
    asked: dict[str, object] = {}

    async def _page(proxy, number):
        asked.update(proxy=proxy, number=number)
        return 200, "<html><head><title>SSIS-001 実測タイトル - JavBus</title></head></html>"

    monkeypatch.setattr(probe_module, "_real_page", _page)

    events = [event async for event in probe_network(make_ctx(_hot("http://127.0.0.1:7890"), None, object()))]

    assert events[-1].level is ProbeLevel.OK
    assert "真实抓取成功" in events[-1].message
    detail = events[-1].detail or ""
    assert "SSIS-001" in detail
    assert "内置样例" in detail, "库为空时用内置样例, 且必须标注来源"
    # 真实抓取要沿用表单里那份代理, 不能用直连顶替.
    assert asked["proxy"] == "http://127.0.0.1:7890"


@pytest.mark.asyncio(loop_scope="function")
async def test_real_fetch_detects_a_verification_page(make_ctx, monkeypatch):
    """HTTP 200 却不是详情页 (年龄验证页) 必须判出来: 这正是"通但没数据"的假绿."""
    _stub(
        monkeypatch,
        _Trace(ok=True, status=200, ip="1.1.1.1", elapsed_ms=10, error=None),
        _Trace(ok=True, status=200, ip="2.2.2.2", elapsed_ms=10, error=None),
    )

    async def _page(proxy, number):
        return 200, "<html><head><title>Age Verification JavBus - JavBus</title></head></html>"

    monkeypatch.setattr(probe_module, "_real_page", _page)

    events = [event async for event in probe_network(make_ctx(_hot("http://127.0.0.1:7890"), None, object()))]

    assert events[-1].level is ProbeLevel.WARN
    assert "不是 SSIS-001 的详情页" in events[-1].message
    assert "验证页" in (events[-1].detail or "")


@pytest.mark.asyncio(loop_scope="function")
async def test_real_fetch_failure_is_warn_not_error(make_ctx, monkeypatch):
    """真实抓取失败只降级为 warn: 站点侧的拦截不该把整段代理结论判死."""
    _stub(
        monkeypatch,
        _Trace(ok=True, status=200, ip="1.1.1.1", elapsed_ms=10, error=None),
        _Trace(ok=True, status=200, ip="2.2.2.2", elapsed_ms=10, error=None),
    )

    async def _page(proxy, number):
        raise RuntimeError("ConnectError: reset by peer")

    monkeypatch.setattr(probe_module, "_real_page", _page)

    events = [event async for event in probe_network(make_ctx(_hot("http://127.0.0.1:7890"), None, object()))]

    assert events[-1].level is ProbeLevel.WARN
    assert "真实抓取失败" in events[-1].message
    assert "reset by peer" in (events[-1].detail or "")


@pytest.mark.asyncio(loop_scope="function")
async def test_real_fetch_is_skipped_when_the_proxy_itself_fails(make_ctx, monkeypatch):
    """代理就不通时再抓一页只是噪音: 那条 error 已经是结论."""
    _stub(
        monkeypatch,
        _Trace(ok=True, status=200, ip="1.1.1.1", elapsed_ms=90, error=None),
        _Trace(ok=False, status=None, ip=None, elapsed_ms=2000, error="ConnectError: refused"),
    )

    async def _never(*args, **kwargs):
        raise AssertionError("代理已判失败时不该再发真实抓取")

    monkeypatch.setattr(probe_module, "_real_page", _never)

    events = [event async for event in probe_network(make_ctx(_hot("http://127.0.0.1:1"), None, object()))]

    assert events[-1].level is ProbeLevel.ERROR
    assert "经代理请求失败" in events[-1].message
