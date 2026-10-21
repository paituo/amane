"""代理自检: 同一目标分别直连与走代理, 对比出口 IP.

只测"能不能连上代理"意义不大 —— 代理进程活着但没生效 (命中直连规则、透明代理回落到本机)
是最常见的故障. 出口 IP 变化与否是唯一能区分这两种情况的判据.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx2
import structlog

from .base import ProbeContext, ProbeEvent, describe_error

logger = structlog.get_logger()

_TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
"""返回纯文本键值对, 其中 ``ip=`` 是出口地址; 比 ipify 一类 JSON 接口更不受 CDN 缓存影响."""

_MISSING_IP = "未返回 ip 字段"

_SUPPORTED_SCHEMES = frozenset({"http", "https", "socks4", "socks5", "socks5h"})


@dataclass(frozen=True, slots=True)
class _Trace:
    ok: bool
    status: int | None
    ip: str | None
    elapsed_ms: int
    error: str | None


async def _trace(proxy: str | None, timeout_s: float) -> _Trace:
    started = time.perf_counter()
    try:
        async with httpx2.AsyncClient(
            proxy=proxy,
            timeout=httpx2.Timeout(timeout_s),
            follow_redirects=True,
            # 不读 HTTP(S)_PROXY 环境变量: 否则"直连"其实走了环境代理, 出口 IP 对比就失去意义,
            # "代理没生效"也会被环境代理掩盖成"一切正常".
            trust_env=False,
        ) as client:
            resp = await client.get(_TRACE_URL)
        elapsed = int((time.perf_counter() - started) * 1000)
        ip = None
        for line in resp.text.splitlines():
            if line.startswith("ip="):
                ip = line[3:].strip() or None
                break
        return _Trace(ok=resp.is_success, status=resp.status_code, ip=ip, elapsed_ms=elapsed, error=None)
    except Exception as e:
        return _Trace(
            ok=False,
            status=None,
            ip=None,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            error=describe_error(e, limit=400),
        )


def _parse_proxy(raw: str) -> tuple[str | None, str | None]:
    """返回 (规范化地址, 错误说明). 只校验语法: 连不通由真实请求判定."""
    parts = urlsplit(raw)
    if parts.scheme.lower() not in _SUPPORTED_SCHEMES:
        supported = "/".join(sorted(_SUPPORTED_SCHEMES))
        return None, f"协议 {parts.scheme or '(空)'} 不受支持, 应为 {supported}"
    if not parts.hostname:
        return None, "缺少主机名, 形如 http://127.0.0.1:7890"
    return raw, None


def _summary(trace: _Trace) -> str:
    if trace.status is not None:
        return f"HTTP {trace.status} · {trace.elapsed_ms} ms · 出口 {trace.ip or _MISSING_IP}"
    return f"{trace.elapsed_ms} ms · {trace.error}"


async def probe_network(ctx: ProbeContext) -> AsyncIterator[ProbeEvent]:
    cfg = ctx.hot.network
    rec = ctx.rec
    raw = (cfg.proxy or "").strip()
    timeout_s = max(cfg.timeout, 8.0)

    if not raw:
        yield rec.info("未配置代理", detail="所有请求直连; 下面只测直连可达性")
        proxy = None
    else:
        proxy, problem = _parse_proxy(raw)
        if problem is not None:
            yield rec.error("代理地址不合法", detail=f"{raw!r}: {problem}")
            return
        yield rec.info(f"代理 {raw}", detail=f"超时 {timeout_s:g}s · 目标 {_TRACE_URL}")

    direct = await _trace(None, timeout_s)
    if direct.ok:
        yield rec.ok("直连成功", detail=_summary(direct))
    else:
        # 直连失败不判死: 用户配代理往往正是因为直连不通.
        yield rec.warn("直连失败", detail=_summary(direct))

    if proxy is None:
        if direct.ok:
            yield rec.ok("网络可用", detail="未配置代理时直连就是最终结果")
        else:
            yield rec.error("直连失败且未配置代理", detail="检查本机网络或填写代理地址")
        return

    proxied = await _trace(proxy, timeout_s)
    if not proxied.ok:
        yield rec.error("经代理请求失败", detail=_summary(proxied))
        return
    yield rec.ok("经代理请求成功", detail=_summary(proxied))

    if proxied.ip is None:
        yield rec.warn("无法判定代理是否生效", detail="目标未返回出口 IP; 只能确认代理链路可通")
        return
    if direct.ip is None:
        yield rec.ok("代理生效 (直连取不到出口 IP, 无法对比)", detail=f"经代理出口 {proxied.ip}")
        return
    if proxied.ip == direct.ip:
        yield rec.warn(
            "出口 IP 未变化, 代理可能没生效",
            detail=f"直连与代理出口都是 {direct.ip}; 常见于规则命中直连、透明代理回落或代理未启动",
        )
        return
    yield rec.ok("代理生效", detail=f"直连 {direct.ip} → 代理 {proxied.ip}")
