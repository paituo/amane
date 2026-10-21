"""配置自检注册表.

键 = ``HotSettings`` 的 section 名, 与设置页的导航一一对应. 前端不需要知道有哪些探针:
新加一个 section 只需在这里注册, 按钮随之出现.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import structlog

from .base import ProbeContext, ProbeEvent, ProbeFn, describe_error
from .llm import probe_agent, probe_llm
from .network import probe_network
from .search import probe_search

logger = structlog.get_logger()

PROBES: dict[str, ProbeFn] = {
    "llm": probe_llm,
    "agent": probe_agent,
    "search": probe_search,
    "network": probe_network,
}


def supported_sections() -> tuple[str, ...]:
    """有自检的分区名. 供文档与前端对照, 顺序固定."""
    return tuple(PROBES)


def is_supported(section: str) -> bool:
    return section in PROBES


async def run_probe(section: str, ctx: ProbeContext) -> AsyncIterator[ProbeEvent]:
    """跑一个分区的探针. 探针内部异常在这里收口成一条 ``ERROR`` 事件.

    不向上抛: 流已经发给客户端了, 用一次干净的收尾把异常原因写进日志区, 比让连接断在半路有用.
    """
    probe = PROBES.get(section)
    if probe is None:
        yield ctx.rec.error(
            f"不支持的分区: {section}",
            detail=f"可选: {', '.join(supported_sections())}",
        )
        return
    try:
        async for event in probe(ctx):
            yield event
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("config probe crashed", section=section)
        yield ctx.rec.error("测试异常中止", detail=describe_error(exc))
