"""``AMANE_HOST`` 的地址解析与监听绑定.

这个文件守的不是"端口能绑上", 而是"通配地址必须同时收 IPv4 与 IPv6":
Windows 的 ``IPV6_V6ONLY`` 默认是 1 (实测), 只绑 ``::`` 会让全部 IPv4 客户端被拒.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest
import uvicorn
from uvicorn.config import STARTUP_FAILURE

import amane.server as server_module
from amane.server import bind_listeners, parse_addresses, run_server


def _ipv6_available() -> bool:
    if not socket.has_ipv6:
        return False
    probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        probe.bind(("::1", 0))
    except OSError:
        return False
    finally:
        probe.close()
    return True


requires_ipv6 = pytest.mark.skipif(not _ipv6_available(), reason="本机没有可用的 IPv6 回环")


def _connect(family: int, host: str, port: int) -> bool:
    probe = socket.socket(family, socket.SOCK_STREAM)
    probe.settimeout(3)
    try:
        probe.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def test_blank_and_star_mean_dual_stack_wildcard() -> None:
    assert parse_addresses("") == ["::"]
    assert parse_addresses("   ") == ["::"]
    assert parse_addresses("*") == ["::"]


def test_comma_separated_with_brackets() -> None:
    assert parse_addresses("127.0.0.1, [::1]") == ["127.0.0.1", "::1"]
    assert parse_addresses("127.0.0.1,,::1") == ["127.0.0.1", "::1"]


@requires_ipv6
def test_wildcard_listens_on_both_families() -> None:
    socks = bind_listeners("::", 0)
    try:
        assert len(socks) == 1
        port = socks[0].getsockname()[1]
        assert _connect(socket.AF_INET, "127.0.0.1", port), "通配地址丢了 IPv4"
        assert _connect(socket.AF_INET6, "::1", port), "通配地址丢了 IPv6"
    finally:
        for sock in socks:
            sock.close()


@requires_ipv6
def test_wildcard_socket_has_v6only_disabled() -> None:
    socks = bind_listeners("::", 0)
    try:
        v6only = socks[0].getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY)
        assert v6only == 0
    finally:
        for sock in socks:
            sock.close()


@requires_ipv6
def test_explicit_ipv4_stays_ipv4_only() -> None:
    socks = bind_listeners("127.0.0.1", 0)
    try:
        assert socks[0].family == socket.AF_INET
        port = socks[0].getsockname()[1]
        assert _connect(socket.AF_INET, "127.0.0.1", port)
        assert not _connect(socket.AF_INET6, "::1", port), "显式 IPv4 地址不应顺带监听 IPv6"
    finally:
        for sock in socks:
            sock.close()


@requires_ipv6
def test_multiple_addresses_share_one_port() -> None:
    socks = bind_listeners("127.0.0.1,::1", 0)
    try:
        assert len(socks) == 2
        ports = {sock.getsockname()[1] for sock in socks}
        assert len(ports) == 1, "多地址必须共用同一端口"
        port = ports.pop()
        assert _connect(socket.AF_INET, "127.0.0.1", port)
        assert _connect(socket.AF_INET6, "::1", port)
    finally:
        for sock in socks:
            sock.close()


def test_bad_address_raises_oserror() -> None:
    with pytest.raises(OSError):
        bind_listeners("300.1.1.1", 0)


def test_wildcard_falls_back_to_ipv4_when_ipv6_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 IPv6 栈 (容器默认关) 时必须退到 IPv4 通配, 而不是起不来."""
    real_bind = server_module._bind_family

    def fake_bind(family: int, sockaddr: tuple, label: str, *, v6only: int | None) -> tuple[socket.socket, str, int]:
        if family == socket.AF_INET6:
            raise OSError("Address family not supported")
        return real_bind(family, sockaddr, label, v6only=v6only)

    monkeypatch.setattr(server_module, "_bind_family", fake_bind)

    socks = bind_listeners("::", 0)
    try:
        assert len(socks) == 1
        assert socks[0].family == socket.AF_INET
        port = socks[0].getsockname()[1]
        assert _connect(socket.AF_INET, "127.0.0.1", port)
    finally:
        for sock in socks:
            sock.close()


def _patch_app(monkeypatch: pytest.MonkeyPatch) -> None:
    class _App:
        def __init__(self) -> None:
            self.state = SimpleNamespace(exit_code=0)

    app = _App()
    monkeypatch.setattr("amane.server.create_app", lambda: app)


def test_run_server_passes_sockets_and_keeps_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_app(monkeypatch)
    captured: dict[str, object] = {}

    def fake_run(self: uvicorn.Server, sockets: list[socket.socket] | None = None) -> None:
        captured["sockets"] = sockets
        captured["host"] = self.config.host
        captured["port"] = self.config.port

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)

    code = run_server(host="127.0.0.1", port=0)

    assert code == 0
    sockets = captured["sockets"]
    assert isinstance(sockets, list) and len(sockets) == 1
    assert captured["port"] == sockets[0].getsockname()[1]
    assert captured["host"] == "127.0.0.1"
    sockets[0].close()


def test_bind_failure_maps_to_startup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_app(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        run_server(host="300.1.1.1", port=0)

    assert exc.value.code == STARTUP_FAILURE
