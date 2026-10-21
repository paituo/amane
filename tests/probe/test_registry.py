"""探针注册表: 未注册的分区要给出可行动的错误, 而不是静默成功."""

from __future__ import annotations

import pytest

from amane.probe import run_probe, supported_sections
from amane.probe.base import ProbeLevel


@pytest.mark.asyncio(loop_scope="function")
async def test_unknown_section_reports_supported(make_ctx):
    events = [event async for event in run_probe("nope", make_ctx())]

    assert len(events) == 1
    assert events[0].level is ProbeLevel.ERROR
    assert "nope" in events[0].message
    assert "llm" in (events[0].detail or "")


def test_supported_sections_are_the_four_configurable_ones():
    assert supported_sections() == ("llm", "agent", "search", "network")
