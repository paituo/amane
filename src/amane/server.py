from __future__ import annotations

import os
import socket
import sys

import structlog
import uvicorn
from uvicorn.config import STARTUP_FAILURE

from .api.app import create_app

EXIT_OK = 0
EXIT_RESTART = 3
EXIT_STARTUP_FAILED = 4

#: 默认监听双栈通配地址: 同时接受 IPv4 与 IPv6.
DEFAULT_HOST = "::"

#: 回环地址全部授予 ``X-Forwarded-For`` 的信任. 双栈下 IPv4 连接可能以
#: ``::ffff:127.0.0.1`` 呈现, 而 uvicorn 默认只信 ``127.0.0.1`` 这一个字面量.
_DEFAULT_FORWARDED_ALLOW_IPS = "127.0.0.1,::1,::ffff:127.0.0.1"

_IPV6_WILDCARDS = frozenset({"", "*", "::"})

logger = structlog.get_logger()


def _strip_brackets(value: str) -> str:
    """去掉 ``[::1]`` 的方括号: 该写法常被直接从 URL 里抄进环境变量."""
    if len(value) >= 2 and value.startswith("[") and value.endswith("]"):
        return value[1:-1]
    return value


def parse_addresses(raw: str) -> list[str]:
    """把 ``AMANE_HOST`` 解析成地址列表 (逗号分隔).

    空值、``*`` 归一为 ``::``, 即双栈通配. 保留顺序, 重复项不去重 —— 重复绑定同一
    地址应当报错而不是被静默吞掉.
    """
    parts = [_strip_brackets(part.strip()) for part in raw.split(",")]
    kept = [DEFAULT_HOST if part in {"", "*"} else part for part in parts if part]
    return kept or [DEFAULT_HOST]


def _resolve(address: str, port: int) -> tuple[int, tuple]:
    """解析地址到 ``(family, sockaddr)``, 失败统一抛 ``OSError``.

    走 ``getaddrinfo`` 而不是 ``ipaddress`` 是为了同时支持主机名与带 zone id 的
    链路本地地址 (``fe80::1%12``).
    """
    try:
        infos = socket.getaddrinfo(address, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OSError(f"无法解析监听地址 {address!r}: {exc}") from exc
    if not infos:
        raise OSError(f"无法解析监听地址 {address!r}")
    family, _, _, _, sockaddr = infos[0]
    return family, sockaddr


def _bind_family(family: int, sockaddr: tuple, label: str, *, v6only: int | None) -> tuple[socket.socket, str, int]:
    """按给定地址族绑定并 listen, 返回 ``(socket, 展示用地址, 实际端口)``."""
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if v6only is not None:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, v6only)
        sock.bind(sockaddr)
        # uvicorn 走 ``sockets=`` 时仍会再 listen 一次, 这里先 listen 是为了让
        # 绑定失败尽早暴露在同一处, 而不是留到事件循环启动之后.
        sock.listen(socket.SOMAXCONN)
    except OSError:
        sock.close()
        raise
    return sock, label, sock.getsockname()[1]


def _bind_one(address: str, port: int) -> tuple[socket.socket, str, int]:
    """绑定单个地址.

    ``::`` 必须显式把 ``IPV6_V6ONLY`` 置 0: Windows 上该选项默认是 1 (实测
    ``getsockopt`` 返回 1), 不清零的话 socket 只收 IPv6, 全部 IPv4 客户端都会被拒.
    IPv6 栈不可用时回退 IPv4 通配, 不让服务起不来.
    """
    if address in _IPV6_WILDCARDS:
        try:
            return _bind_family(socket.AF_INET6, ("::", port), "::", v6only=0)
        except OSError as exc:
            logger.warning("双栈通配绑定失败, 回退 IPv4 通配", error=str(exc))
            address = "0.0.0.0"
    family, sockaddr = _resolve(address, port)
    return _bind_family(family, sockaddr, address, v6only=1 if family == socket.AF_INET6 else None)


def bind_listeners(host: str, port: int) -> list[socket.socket]:
    """绑定 ``AMANE_HOST`` 声明的全部监听 socket.

    自己建 socket 而不交给 uvicorn 的 ``host`` 参数, 有两个原因: uvicorn 的
    ``host`` 是单值, 表达不了"同时监听 IPv4 与 IPv6 回环"; 且它不会为通配 IPv6
    强制双栈. 端口传 0 时第一个 socket 取到实际端口, 后续地址复用它.
    """
    sockets: list[socket.socket] = []
    bound_port = port
    try:
        for address in parse_addresses(host):
            sock, _, bound_port = _bind_one(address, bound_port)
            sockets.append(sock)
    except OSError:
        for sock in sockets:
            sock.close()
        raise
    return sockets


def _format_sockname(sockname: tuple) -> str:
    addr, port = sockname[0], sockname[1]
    return f"[{addr}]:{port}" if ":" in addr else f"{addr}:{port}"


def run_server(*, host: str, port: int, log_level: str = "info") -> int:
    """阻塞运行直到停机, 返回退出码 (0 正常停 / 3 请求重启).

    启动错误 (地址无法绑定、端口占用、配置非法) 不返回: 与 uvicorn 自己的行为
    一致, 抛 ``SystemExit(STARTUP_FAILURE)``, 由 `main` 改写为 `EXIT_STARTUP_FAILED`.
    """
    app = create_app()
    app.state.exit_code = EXIT_OK
    try:
        sockets = bind_listeners(host, port)
    except OSError as exc:
        logger.error("监听地址绑定失败", host=host, port=port, error=str(exc))
        raise SystemExit(STARTUP_FAILURE) from exc

    bound_port = sockets[0].getsockname()[1]
    logger.info(
        "listening",
        addresses=[_format_sockname(sock.getsockname()) for sock in sockets],
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            # uvicorn 只把这两个值用于启动日志, 真正的绑定已经完成; 多地址时取首个作展示.
            host=parse_addresses(host)[0],
            port=bound_port,
            log_level=log_level,
            timeout_graceful_shutdown=5,
            forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", _DEFAULT_FORWARDED_ALLOW_IPS),
        )
    )
    app.state.server = server
    server.run(sockets=sockets)
    code: int = app.state.exit_code
    return code


def main() -> None:
    if getattr(sys, "frozen", False):
        os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "1")
    host = os.environ.get("AMANE_HOST", DEFAULT_HOST)
    port = int(os.environ.get("AMANE_PORT", "8000"))
    try:
        code = run_server(host=host, port=port)
    except SystemExit as exc:
        # uvicorn 的启动失败码与 EXIT_RESTART 相同, 进程外无法区分, 因此改写为独立退出码.
        sys.exit(EXIT_STARTUP_FAILED if exc.code == STARTUP_FAILURE else exc.code)
    sys.exit(code)


if __name__ == "__main__":
    main()
