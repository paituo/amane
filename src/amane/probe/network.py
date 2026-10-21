"""代理自检: 同一目标分别直连与走代理, 对比出口 IP; 最后再用这份配置真抓一次站点页面.

只测"能不能连上代理"意义不大 —— 代理进程活着但没生效 (命中直连规则、透明代理回落到本机)
是最常见的故障. 出口 IP 变化与否是唯一能区分这两种情况的判据.

出口 IP 是必要条件, 不是充分条件: 出口对了仍可能被目标站拦 (验证页、403、地区限制). 所以
最后一步用真实番号抓一次详情页, 把"能连"落到"能拿到数据".
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx2
import structlog

from .base import ProbeContext, ProbeEvent, ProbeLevel, describe_error

logger = structlog.get_logger()

_TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
"""返回纯文本键值对, 其中 ``ip=`` 是出口地址; 比 ipify 一类 JSON 接口更不受 CDN 缓存影响."""

_MISSING_IP = "未返回 ip 字段"

_SUPPORTED_SCHEMES = frozenset({"http", "https", "socks4", "socks5", "socks5h"})

_REAL_TARGET = "https://www.javbus.com/{number}"
"""真实抓取目标: 项目的主力站点之一, 且是刮削管线的实际入口. 取详情页而不是首页 ——
首页会跳年龄验证, 详情页带上 ``dv=1`` 直接给内容."""

_REAL_COOKIES = {"dv": "1"}
"""javbus 的年龄验证标记. 与 ``JavBusCrawler.profile`` 里的 cookies 同值; 少了它只会拿到验证页,
那条"成功但没数据"的假绿正是这一步要挡的."""

_FALLBACK_NUMBER = "SSIS-001"
"""库里没有番号时的样例. 只用来验证链路通不通, 不代表该番号一定还在站上."""

_REAL_TIMEOUT_S = 30.0
"""真实抓取上限. 比 trace 宽松: 详情页带图链与更多脚本, 慢一点正常."""

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


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
    """先判代理是否真的生效, 再用同一份配置真抓一次页面."""
    verdict: list[ProbeEvent] = []
    async for event in _connectivity(ctx):
        verdict.append(event)
        yield event

    # 代理本身就不通时, 真实抓取必然失败, 再补一条只是给结论添噪音.
    if verdict and verdict[-1].level is ProbeLevel.ERROR:
        return
    async for event in _real_fetch_effect(ctx):
        yield event


async def _connectivity(ctx: ProbeContext) -> AsyncIterator[ProbeEvent]:
    """代理链路判据: 直连与经代理各取一次出口 IP, 对比是否变化."""
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


def _sample_number(ctx: ProbeContext) -> tuple[str, str]:
    """从真实库里取一个番号, 返回 (番号, 来源说明).

    用库里的真实番号而不是固定样例: 抓的正是用户那批数据里的东西, 站点上还在不在也能顺带确认.
    库里还没有番号时退回内置样例, 并如实标注来源.
    """
    if not ctx.db_path.exists():
        return _FALLBACK_NUMBER, "内置样例"
    try:
        conn = sqlite3.connect(f"{ctx.db_path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return _FALLBACK_NUMBER, "内置样例"
    try:
        row = conn.execute(
            "SELECT number FROM media_files WHERE number IS NOT NULL AND number <> '' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    number = str(row[0]).strip() if row and row[0] is not None else ""
    return (number, "库内媒体文件") if number else (_FALLBACK_NUMBER, "内置样例")


def _page_title(html: str) -> str:
    """取页面标题. 只为让人一眼看出拿到的是详情页还是验证页, 不做完整解析."""
    match = _TITLE_RE.search(html)
    if match is None:
        return ""
    return " ".join(match.group(1).split())


async def _real_page(proxy: str | None, number: str) -> tuple[int, str]:
    """抓一页真实详情页. 单独成函数, 便于测试替换而不触网."""
    async with httpx2.AsyncClient(
        proxy=proxy,
        timeout=httpx2.Timeout(_REAL_TIMEOUT_S),
        follow_redirects=True,
        # 与 trace 同理: 环境代理会把"配的代理没生效"掩盖成正常.
        trust_env=False,
    ) as client:
        resp = await client.get(_REAL_TARGET.format(number=number), cookies=_REAL_COOKIES)
    return resp.status_code, resp.text


async def _real_fetch_effect(ctx: ProbeContext) -> AsyncIterator[ProbeEvent]:
    """真实数据段: 用这份代理配置真抓一次详情页, 看拿到的是数据还是验证页/空壳.

    出口 IP 对了也可能被目标站拦: 年龄验证页、403、地区限制都表现为 HTTP 200 + 一堆无关内容.
    因此判据不是状态码, 而是"页面标题里有没有这个番号".
    """
    if ctx.runtime is None:
        return

    number, origin = _sample_number(ctx)
    proxy = (ctx.hot.network.proxy or "").strip() or None
    started = time.perf_counter()
    try:
        status, html = await _real_page(proxy, number)
    except Exception as e:
        yield ctx.rec.warn(f"真实抓取失败 ({number})", detail=describe_error(e, limit=400))
        return

    elapsed = int((time.perf_counter() - started) * 1000)
    body = f"{number} ({origin}) · HTTP {status} · {len(html.encode('utf-8'))} 字节 · {elapsed} ms · 出口 {proxy or '直连'}"
    if not html.strip():
        yield ctx.rec.warn("真实抓取返回空响应", detail=body)
        return
    if status >= 400:
        yield ctx.rec.warn(f"真实抓取返回 HTTP {status}", detail=body)
        return

    title = _page_title(html)
    if not title:
        yield ctx.rec.warn("真实抓取没解析出页面标题", detail=body)
        return
    if number.lower() not in title.lower():
        yield ctx.rec.warn(
            f"抓到的不是 {number} 的详情页",
            detail=f"{body}\n页面标题: {title[:120]}\n多半是年龄验证页、站内无此条目或地区限制",
        )
        return
    yield ctx.rec.ok(f"真实抓取成功: {title[:80]}", detail=body)
