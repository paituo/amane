"""工具名命名空间改写表测试."""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

from amane.agent.runtime import build_agent
from amane.agent.tool_names import ToolNameAlias, alias_response_tool_names, canonical_tool_name
from amane.config import AgentConfig
from tests.agent.support import no_limit


@pytest.mark.parametrize(
    ("name", "available", "expected"),
    [
        ("rename_facet", frozenset({"rename_facet"}), "rename_facet"),
        ("rename_facet__rename_facet", frozenset({"rename_facet"}), "rename_facet"),
        ("list_facet_rules__list_facet_rules", frozenset({"list_facet_rules"}), "list_facet_rules"),
        ("mcp__rename_facet", frozenset({"rename_facet"}), "rename_facet"),
        ("a__b__rename_facet", frozenset({"rename_facet"}), "rename_facet"),
        ("__rename_facet", frozenset({"rename_facet"}), "rename_facet"),
        ("rename_facet__", frozenset({"rename_facet"}), "rename_facet__"),
        ("__", frozenset({"rename_facet"}), "__"),
        ("add_facet_rule", frozenset({"rename_facet"}), "add_facet_rule"),
        ("foo__bar", frozenset({"rename_facet"}), "foo__bar"),
        ("sql_explore", frozenset({"sql_explore"}), "sql_explore"),
        (
            "rename_facet__rename_facet",
            frozenset({"rename_facet__rename_facet", "rename_facet"}),
            "rename_facet__rename_facet",
        ),
        ("", frozenset(), ""),
        ("rename_facet__rename_facet", frozenset(), "rename_facet__rename_facet"),
        ("facet-identity.rename_facet", frozenset({"rename_facet"}), "facet-identity.rename_facet"),
    ],
)
def test_canonical_tool_name(name: str, available: frozenset[str], expected: str) -> None:
    assert canonical_tool_name(name, available) == expected


def test_alias_response_rewrites_unknown_suffix_only() -> None:
    original = ToolCallPart(
        tool_name="rename_facet__rename_facet", args={"kind": "studio", "facet_id": 1, "name": "x"}, tool_call_id="c1"
    )
    kept = ToolCallPart(tool_name="add_facet_rule", args={}, tool_call_id="c2")
    text = TextPart(content="ok")
    response = ModelResponse(parts=[text, original, kept])

    out = alias_response_tool_names(response, frozenset({"rename_facet"}))
    assert out is not response
    assert isinstance(out.parts[0], TextPart)
    rewritten = out.parts[1]
    assert isinstance(rewritten, ToolCallPart)
    assert rewritten.tool_name == "rename_facet"
    assert rewritten.args == original.args
    assert rewritten.tool_call_id == "c1"
    leftover = out.parts[2]
    assert isinstance(leftover, ToolCallPart)
    assert leftover is kept


def test_alias_response_noop_when_already_available() -> None:
    call = ToolCallPart(tool_name="rename_facet", args={"kind": "studio"}, tool_call_id="c1")
    response = ModelResponse(parts=[call])
    assert alias_response_tool_names(response, frozenset({"rename_facet"})) is response


def test_alias_response_preserves_other_response_fields() -> None:
    response = ModelResponse(
        parts=[ToolCallPart(tool_name="sql_explore__sql_explore", args={}, tool_call_id="c1")],
        model_name="deepseek-v4-flash",
    )
    out = alias_response_tool_names(response, frozenset({"sql_explore"}))
    rewritten = out.parts[0]
    assert isinstance(rewritten, ToolCallPart)
    assert rewritten.tool_name == "sql_explore"
    assert out.model_name == "deepseek-v4-flash"


def test_build_agent_wires_tool_name_alias() -> None:
    agent = build_agent(AgentConfig(api_key="sk-test", model="gpt-4o", base_url="https://example.com/v1"), no_limit())
    assert agent is not None
    assert any(isinstance(cap, ToolNameAlias) for cap in agent.root_capability.capabilities)
