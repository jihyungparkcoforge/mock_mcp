"""Provider/protocol translation is tested separately from workflow policy."""

import asyncio
import copy
import json
import sys
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp.types import CallToolResult, TextContent, Tool

from equipment_mcp.foundry import FoundryAdapter
from equipment_mcp.mcp_executor import McpToolExecutor
from equipment_mcp.models import (
    DecisionContext,
    EquipmentRequest,
    ModelTurn,
    ReviewResult,
    ToolCall,
    ToolExecutionError,
    ToolResult,
)
from equipment_mcp.prompts import LOOP_REFLECTION_PROMPT, REFLECTION_PROMPT, REQUEST_SPLIT_PROMPT


class Client:
    def __init__(self, responses):
        self.messages = self
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return self.responses.pop(0)


def response(content, stop="end_turn"):
    return SimpleNamespace(content=content, stop_reason=stop)


def text(value):
    return SimpleNamespace(type="text", text=json.dumps(value))


def context():
    return DecisionContext(
        EquipmentRequest("r1", "headset", "I need a headset."),
        "I need a headset.",
        "E001",
        date(2026, 11, 23),
        {"IT": 120},
    )


def test_foundry_translates_calls_results_and_preserves_opaque_history():
    thinking = SimpleNamespace(type="thinking", thinking="Check employee", signature="opaque")
    call = SimpleNamespace(
        type="tool_use", id="c1", name="get_employee_info", input={"employee_id": "E001"}
    )
    client = Client([response([thinking, call]), response([]), response([])])
    model = FoundryAdapter(client, "test-model", max_tokens=123)
    definition = {
        "name": "get_employee_info",
        "description": "Employee lookup",
        "input_schema": {"type": "object"},
    }
    session = model.start_session(context(), [definition])
    assert not client.calls  # Construction and session creation have no I/O.

    async def run():
        turn = await session.next_actions()
        assert turn == ModelTurn(
            [ToolCall("c1", "get_employee_info", {"employee_id": "E001"})], ["Check employee"]
        )
        await session.next_actions([ToolResult("c1", "temporary error", True)])
        await session.next_actions([])

    asyncio.run(run())
    first, second, third = client.calls
    assert LOOP_REFLECTION_PROMPT not in str(first["messages"])
    assert first["model"] == "test-model" and first["max_tokens"] == 123
    props = first["tools"][-1]["input_schema"]["properties"]
    assert props["request_id"]["enum"] == ["r1"] and props["item"]["enum"] == ["headset"]
    assert second["messages"][1]["content"][0].signature == "opaque"
    assert second["messages"][2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "c1",
            "content": "temporary error",
            "is_error": True,
        },
        {"type": "text", "text": LOOP_REFLECTION_PROMPT},
    ]
    assert third["messages"][-1]["content"] == [{"type": "text", "text": LOOP_REFLECTION_PROMPT}]
    # Each iteration adds one instruction after results, preserving earlier history.
    assert third["messages"][:4] == second["messages"] + [{"role": "assistant", "content": []}]
    assert first["tools"][0] == {**definition, "cache_control": {"type": "ephemeral"}}
    assert "cache_control" not in first["tools"][-1]  # Per-item schema follows the shared prefix.
    for request in client.calls:
        assert request["cache_control"] == {"type": "ephemeral"}
        assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["system"] == second["system"] == third["system"]
    assert "cache_control" not in definition
    assert definition["input_schema"] == {"type": "object"}


def test_foundry_extracts_and_reviews_domain_values():
    client = Client(
        [
            response([text({"requests": [{"item": " Headset ", "text": "I need a headset."}]})]),
            response([text({"verdict": "confirmed", "issues": [], "revised_response": None})]),
        ]
    )
    model = FoundryAdapter(client, "test-model")

    async def run():
        assert await model.split_requests("I need a headset.") == [context().task]
        review = await model.review(
            context(),
            {
                "decision": "approve",
                "ticket_id": None,
                "response": "Approved.",
            },
            [],
        )
        assert review == ReviewResult("confirmed", [])

    asyncio.run(run())
    assert "Current request ID: r1" in client.calls[1]["messages"][0]["content"]
    for request, prompt in zip(
        client.calls, [REQUEST_SPLIT_PROMPT, REFLECTION_PROMPT], strict=True
    ):
        assert request["cache_control"] == {"type": "ephemeral"}
        assert request["system"] == [
            {"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}
        ]


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_foundry_rejects_incomplete_responses(stop):
    model = FoundryAdapter(Client([response([], stop)]), "test-model")
    with pytest.raises(RuntimeError, match=stop):
        asyncio.run(model.split_requests("I need a headset."))


def test_mcp_pagination_and_structured_results():
    class Session:
        cursors = []

        async def list_tools(self, *, params=None):
            cursor = params.cursor if params else None
            self.cursors.append(cursor)
            name = "first" if cursor is None else "second"
            return SimpleNamespace(
                tools=[Tool(name=name, description="lookup", input_schema={"type": "object"})],
                next_cursor="next" if cursor is None else None,
            )

        async def call_tool(self, name, arguments):
            return CallToolResult(content=[], structured_content={"item": name})

    async def run():
        session = Session()
        tools = await McpToolExecutor.discover(session)
        assert session.cursors == [None, "next"]
        assert [t["name"] for t in tools.definitions] == ["first", "second"]
        assert await tools.call("second", {}) == {"item": "second"}

    asyncio.run(run())


@pytest.mark.parametrize(
    "value,is_error,expected",
    [
        ('{"price": 120}', False, {"price": 120}),
        ("invalid JSON", False, None),
        ("[]", False, None),
        ("tool failed", True, None),
    ],
)
def test_mcp_text_and_error_translation(value, is_error, expected):
    class Session:
        async def call_tool(self, name, arguments):
            return CallToolResult(content=[TextContent(type="text", text=value)], is_error=is_error)

    tool = Tool(name="lookup", description="Lookup", input_schema={"type": "object"})
    tools = McpToolExecutor([async_mcp_tool(tool, Session())])
    if expected is None:
        with pytest.raises(ToolExecutionError):
            asyncio.run(tools.call("lookup", {}))
    else:
        assert asyncio.run(tools.call("lookup", {})) == expected


def test_mcp_unknown_tool_is_rejected_without_calling_session():
    tools = McpToolExecutor([])
    with pytest.raises(ToolExecutionError, match="Unknown tool"):
        asyncio.run(tools.call("missing", {}))


def test_mcp_adapter_round_trip_with_local_stdio_server():
    from mcp import Client, StdioServerParameters

    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "equipment_mcp.server", "--stdio"]
        )
        async with asyncio.timeout(20):
            async with Client(params, mode="legacy") as client:
                executor = await McpToolExecutor.discover(client.session)
                assert len(executor.definitions) == 8
                catalog = next(t for t in executor.definitions if t["name"] == "get_item_catalog")
                assert catalog["input_schema"]["properties"]["item"]["description"]
                catalog_result, employee, inventory = await asyncio.gather(
                    executor.call("get_item_catalog", {"item": "laptop"}),
                    executor.call("get_employee_info", {"employee_id": "E001"}),
                    executor.call("check_inventory", {"item": "monitor"}),
                )
                assert catalog_result["item"] == "laptop" and catalog_result["price"] > 0
                assert employee["employee_id"] == "E001"
                assert inventory["item"] == "monitor" and "in_stock" in inventory

    asyncio.run(run())


def test_unpriced_earlier_approvals_are_visible_to_session_and_reviewer():
    client = Client(
        [
            response([]),
            response([text({"verdict": "confirmed", "issues": [], "revised_response": None})]),
        ]
    )
    model = FoundryAdapter(client, "test-model")
    current = replace(context(), unpriced_approvals=("laptop",))

    async def run():
        await model.start_session(current, []).next_actions()
        await model.review(
            current, {"decision": "deny", "ticket_id": None, "response": "Denied."}, []
        )

    asyncio.run(run())
    for call in client.calls:
        assert (
            "Earlier approvals missing price or department: laptop"
            in call["messages"][0]["content"]
        )
