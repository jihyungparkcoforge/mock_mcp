"""Unit tests for the agent's pure helpers (no LLM, no MCP connection)."""

from types import SimpleNamespace

import pytest

from equipment_mcp.agent import (
    TraceStep,
    format_trace_step,
    mcp_tool_to_anthropic,
    parse_final_decision,
)


class TestMcpToolToAnthropic:
    def test_maps_fields(self):
        schema = {"type": "object", "properties": {"role": {"type": "string"}},
                  "required": ["role"]}
        tool = SimpleNamespace(name="get_policy_limits", description="Policy.",
                               input_schema=schema)
        assert mcp_tool_to_anthropic(tool) == {
            "name": "get_policy_limits",
            "description": "Policy.",
            "input_schema": schema,
        }

    def test_missing_description_becomes_empty_string(self):
        tool = SimpleNamespace(name="x", description=None,
                               input_schema={"type": "object", "properties": {}})
        assert mcp_tool_to_anthropic(tool)["description"] == ""


class TestFormatTraceStep:
    @pytest.mark.parametrize("kind", ["Thought", "Action", "Observation", "Reflection"])
    def test_prefixes_kind(self, kind):
        assert format_trace_step(TraceStep(kind, "hello")) == f"{kind}: hello"


class TestParseFinalDecision:
    def test_parses_json_block(self):
        text = (
            "All checks done.\n"
            "```json\n"
            '{"decision": "approve", "response": "Approved.", "ticket_id": null}\n'
            "```"
        )
        assert parse_final_decision(text) == {
            "decision": "approve",
            "response": "Approved.",
            "ticket_id": None,
        }

    def test_uses_last_json_block(self):
        text = (
            '```json\n{"decision": "approve", "response": "a"}\n```\n'
            "On reflection:\n"
            '```json\n{"decision": "escalate", "response": "b", "ticket_id": "ESC-0001"}\n```'
        )
        result = parse_final_decision(text)
        assert result["decision"] == "escalate"
        assert result["ticket_id"] == "ESC-0001"

    def test_missing_ticket_id_defaults_to_none(self):
        text = '```json\n{"decision": "deny", "response": "No."}\n```'
        assert parse_final_decision(text)["ticket_id"] is None

    def test_no_json_block_raises(self):
        with pytest.raises(ValueError):
            parse_final_decision("I approve this request.")

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError):
            parse_final_decision("```json\n{decision: approve}\n```")

    def test_unknown_decision_raises(self):
        with pytest.raises(ValueError):
            parse_final_decision('```json\n{"decision": "maybe", "response": "?"}\n```')
