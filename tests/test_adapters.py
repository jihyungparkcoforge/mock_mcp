"""Provider/protocol translation is tested separately from workflow policy."""

import asyncio
import copy
import json
import sys
from datetime import date
from types import SimpleNamespace

import pytest

from equipment_mcp.foundry import FoundryDecisionModel
from equipment_mcp.mcp_executor import McpToolExecutor
from equipment_mcp.models import (
    DecisionContext,
    EquipmentRequest,
    ModelTurn,
    ReviewResult,
    ToolCall,
    ToolDefinition,
    ToolExecutionError,
    ToolResult,
)


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
    model = FoundryDecisionModel(client, "test-model", max_tokens=123)
    definition = ToolDefinition("get_employee_info", "Employee lookup", {"type": "object"})
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
        }
    ]
    assert third["messages"][-1]["content"] == "Finish request r1 by calling submit_decision."
    assert definition.parameters == {"type": "object"}


def test_foundry_extracts_and_reviews_domain_values():
    client = Client(
        [
            response([text({"requests": [{"item": " Headset ", "text": "I need a headset."}]})]),
            response([text({"verdict": "confirmed", "issues": [], "revised_response": None})]),
        ]
    )
    model = FoundryDecisionModel(client, "test-model")

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


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_foundry_rejects_incomplete_responses(stop):
    model = FoundryDecisionModel(Client([response([], stop)]), "test-model")
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
                tools=[
                    SimpleNamespace(
                        name=name, description="lookup", input_schema={"type": "object"}
                    )
                ],
                next_cursor="next" if cursor is None else None,
            )

        async def call_tool(self, name, arguments):
            return SimpleNamespace(content=[], is_error=False, structured_content={"item": name})

    async def run():
        session = Session()
        tools = await McpToolExecutor.discover(session)
        assert session.cursors == [None, "next"]
        assert [t.name for t in tools.definitions] == ["first", "second"]
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
            return SimpleNamespace(
                content=[{"text": value}], is_error=is_error, structured_content=None
            )

    tools = McpToolExecutor(Session(), [ToolDefinition("lookup", "", {})])
    if expected is None:
        with pytest.raises(ToolExecutionError):
            asyncio.run(tools.call("lookup", {}))
    else:
        assert asyncio.run(tools.call("lookup", {})) == expected


def test_mcp_unknown_tool_is_rejected_without_calling_session():
    tools = McpToolExecutor(object(), [])
    with pytest.raises(ToolExecutionError, match="Unknown tool"):
        asyncio.run(tools.call("missing", {}))


def test_mcp_adapter_round_trip_with_local_stdio_server():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "equipment_mcp.server", "--stdio"]
        )
        async with asyncio.timeout(20):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    executor = await McpToolExecutor.discover(session)
                    assert len(executor.definitions) == 8
                    catalog = next(
                        t for t in executor.definitions if t.name == "get_item_catalog"
                    )
                    assert catalog.parameters["properties"]["item"]["description"]
                    result = await executor.call("get_item_catalog", {"item": "laptop"})
                    assert result["item"] == "laptop"
                    assert result["price"] > 0

    asyncio.run(run())
