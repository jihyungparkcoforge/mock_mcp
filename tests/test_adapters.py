"""Provider/protocol translation is tested separately from workflow policy."""

import asyncio
import copy
import json
import sys
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import ClientSession
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


# Replace only the SDK client: canned responses avoid network calls while the real
# FoundryAdapter translates requests, responses, and conversation history.
class Client:
    def __init__(self, responses):
        self.messages = self
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        return self.responses.pop(0)


def response(content, stop="end_turn"):
    """Build a fake SDK response from predefined content blocks."""
    return SimpleNamespace(content=content, stop_reason=stop)


def text(value):
    """Wrap JSON as a fake SDK text block."""
    return SimpleNamespace(type="text", text=json.dumps(value))


def context():
    return DecisionContext(
        EquipmentRequest("r1", "headset", "I need a headset."),
        "I need a headset.",
        "E001",
        date(2026, 11, 23),
        {"IT": 120},
    )


@pytest.fixture
def foundry_session():
    """Create a real FoundryAdapter session with a mock client; no external API calls."""
    thinking = SimpleNamespace(type="thinking", thinking="Check employee", signature="opaque")
    call = SimpleNamespace(
        type="tool_use", id="c1", name="get_employee_info", input={"employee_id": "E001"}
    )
    # Simulate an employee lookup request followed by two empty model responses.
    client = Client([response([thinking, call]), response([]), response([])])
    model = FoundryAdapter(client, "test-model", max_tokens=123)
    definition = {
        "name": "get_employee_info",
        "description": "Employee lookup",
        "input_schema": {"type": "object"},
    }
    session = model.start_session(context(), [definition])
    return SimpleNamespace(client=client, session=session, definition=definition)


@pytest.fixture
def foundry_conversation(foundry_session):
    """Replay three mock model responses to inspect the real adapter's conversation history."""

    async def run():
        turn = await foundry_session.session.next_actions()
        await foundry_session.session.next_actions([ToolResult("c1", "temporary error", True)])
        await foundry_session.session.next_actions([])
        return turn

    foundry_session.turn = asyncio.run(run())
    return foundry_session


def test_foundry_session_creation_does_not_call_provider(foundry_session):
    assert foundry_session.client.calls == []


def test_foundry_translates_thinking_and_tool_calls(foundry_conversation):
    assert foundry_conversation.turn == ModelTurn(
        [ToolCall("c1", "get_employee_info", {"employee_id": "E001"})], ["Check employee"]
    )


def test_foundry_first_turn_has_no_loop_reflection(foundry_conversation):
    first = foundry_conversation.client.calls[0]
    assert LOOP_REFLECTION_PROMPT not in str(first["messages"])


@pytest.mark.parametrize("field,expected", [("model", "test-model"), ("max_tokens", 123)])
def test_foundry_passes_model_settings(foundry_conversation, field, expected):
    assert foundry_conversation.client.calls[0][field] == expected


@pytest.mark.parametrize("field,expected", [("request_id", ["r1"]), ("item", ["headset"])])
def test_foundry_scopes_decision_schema(foundry_conversation, field, expected):
    props = foundry_conversation.client.calls[0]["tools"][-1]["input_schema"]["properties"]
    assert props[field]["enum"] == expected


def test_foundry_preserves_opaque_thinking_signature(foundry_conversation):
    second = foundry_conversation.client.calls[1]
    assert second["messages"][1]["content"][0].signature == "opaque"


def test_foundry_translates_error_result_with_reflection(foundry_conversation):
    second = foundry_conversation.client.calls[1]
    assert second["messages"][2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "c1",
            "content": "temporary error",
            "is_error": True,
        },
        {"type": "text", "text": LOOP_REFLECTION_PROMPT},
    ]


def test_foundry_empty_results_still_add_reflection(foundry_conversation):
    third = foundry_conversation.client.calls[2]
    assert third["messages"][-1]["content"] == [{"type": "text", "text": LOOP_REFLECTION_PROMPT}]


def test_foundry_next_turn_preserves_earlier_history(foundry_conversation):
    _, second, third = foundry_conversation.client.calls
    assert third["messages"][:4] == second["messages"] + [{"role": "assistant", "content": []}]


def test_foundry_caches_shared_tool_definition(foundry_conversation):
    first = foundry_conversation.client.calls[0]
    assert first["tools"][0] == {
        **foundry_conversation.definition,
        "cache_control": {"type": "ephemeral"},
    }


def test_foundry_does_not_cache_per_item_decision_schema(foundry_conversation):
    assert "cache_control" not in foundry_conversation.client.calls[0]["tools"][-1]


@pytest.mark.parametrize("turn", [0, 1, 2], ids=["first", "with-results", "empty-results"])
def test_foundry_caches_each_request(foundry_conversation, turn):
    assert foundry_conversation.client.calls[turn]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize("turn", [0, 1, 2], ids=["first", "with-results", "empty-results"])
def test_foundry_caches_each_system_prompt(foundry_conversation, turn):
    request = foundry_conversation.client.calls[turn]
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_foundry_system_prompt_stays_stable(foundry_conversation):
    first, second, third = foundry_conversation.client.calls
    assert first["system"] == second["system"] == third["system"]


def test_foundry_does_not_mutate_caller_tool_definition(foundry_conversation):
    assert foundry_conversation.definition == {
        "name": "get_employee_info",
        "description": "Employee lookup",
        "input_schema": {"type": "object"},
    }


@pytest.fixture
def foundry_domain_calls():
    """Exercise real request extraction and review parsing with fixed mock client responses."""
    client = Client(
        [
            response([text({"requests": [{"item": " Headset ", "text": "I need a headset."}]})]),
            response([text({"verdict": "confirmed", "issues": [], "revised_response": None})]),
        ]
    )
    model = FoundryAdapter(client, "test-model")

    async def run():
        requests = await model.split_requests("I need a headset.")
        review = await model.review(
            context(),
            {"decision": "approve", "ticket_id": None, "response": "Approved."},
            [],
        )
        return SimpleNamespace(requests=requests, review=review, calls=client.calls)

    return asyncio.run(run())


def test_foundry_extracts_normalized_requests(foundry_domain_calls):
    assert foundry_domain_calls.requests == [context().task]


def test_foundry_translates_review_result(foundry_domain_calls):
    assert foundry_domain_calls.review == ReviewResult("confirmed", [])


def test_foundry_review_includes_current_request_id(foundry_domain_calls):
    assert "Current request ID: r1" in foundry_domain_calls.calls[1]["messages"][0]["content"]


@pytest.mark.parametrize("phase", [0, 1], ids=["split", "review"])
def test_foundry_caches_domain_requests(foundry_domain_calls, phase):
    assert foundry_domain_calls.calls[phase]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize(
    "phase,prompt", [(0, REQUEST_SPLIT_PROMPT), (1, REFLECTION_PROMPT)], ids=["split", "review"]
)
def test_foundry_uses_cached_domain_prompt(foundry_domain_calls, phase, prompt):
    assert foundry_domain_calls.calls[phase]["system"] == [
        {"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}
    ]


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_foundry_rejects_incomplete_responses(stop):
    model = FoundryAdapter(Client([response([], stop)]), "test-model")
    with pytest.raises(RuntimeError, match=stop):
        asyncio.run(model.split_requests("I need a headset."))


@pytest.fixture
def paginated_mcp():
    """Exercise the real MCP executor with a mock session that returns two tool pages."""

    # Emulate two MCP pages and a structured tool response without a server process.
    # The real executor must advance the cursor and decode the protocol response.
    class Session:
        def __init__(self):
            self.cursors = []

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
        result = await tools.call("second", {})
        return SimpleNamespace(session=session, tools=tools, result=result)

    return asyncio.run(run())


def test_mcp_discovery_advances_page_cursor(paginated_mcp):
    assert paginated_mcp.session.cursors == [None, "next"]


def test_mcp_discovery_keeps_tools_from_all_pages(paginated_mcp):
    assert [t["name"] for t in paginated_mcp.tools.definitions] == ["first", "second"]


def test_mcp_translates_structured_result(paginated_mcp):
    assert paginated_mcp.result == {"item": "second"}


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
    # Mock the SDK session without opening a connection. Autospec checks method
    # signatures, and spec_set rejects attributes absent from ClientSession.
    # The real SDK wrapper and executor still decode the controlled wire payload.
    session = create_autospec(ClientSession, instance=True, spec_set=True)
    session.call_tool.return_value = CallToolResult(
        content=[TextContent(type="text", text=value)], is_error=is_error
    )

    tool = Tool(name="lookup", description="Lookup", input_schema={"type": "object"})
    tools = McpToolExecutor([async_mcp_tool(tool, session)])
    if expected is None:
        with pytest.raises(ToolExecutionError):
            asyncio.run(tools.call("lookup", {}))
    else:
        assert asyncio.run(tools.call("lookup", {})) == expected


def test_mcp_unknown_tool_is_rejected_without_calling_session():
    tools = McpToolExecutor([])
    with pytest.raises(ToolExecutionError, match="Unknown tool"):
        asyncio.run(tools.call("missing", {}))


@pytest.fixture(scope="module")
def stdio_round_trip():
    """Connect the real MCP executor to a local server process over stdio; no mocks."""
    from mcp import Client, StdioServerParameters

    async def run():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "equipment_mcp.server", "--stdio"]
        )
        async with asyncio.timeout(20):
            async with Client(params, mode="legacy") as client:
                executor = await McpToolExecutor.discover(client.session)
                catalog_result, employee, inventory = await asyncio.gather(
                    executor.call("get_item_catalog", {"item": "laptop"}),
                    executor.call("get_employee_info", {"employee_id": "E001"}),
                    executor.call("check_inventory", {"item": "monitor"}),
                )
                return SimpleNamespace(
                    definitions=executor.definitions,
                    catalog=catalog_result,
                    employee=employee,
                    inventory=inventory,
                )

    return asyncio.run(run())


def test_stdio_discovery_returns_all_tools(stdio_round_trip):
    assert len(stdio_round_trip.definitions) == 8


def test_stdio_catalog_schema_describes_item(stdio_round_trip):
    catalog = next(t for t in stdio_round_trip.definitions if t["name"] == "get_item_catalog")
    assert catalog["input_schema"]["properties"]["item"]["description"]


def test_stdio_catalog_returns_requested_item(stdio_round_trip):
    assert stdio_round_trip.catalog["item"] == "laptop"


def test_stdio_catalog_returns_positive_price(stdio_round_trip):
    assert stdio_round_trip.catalog["price"] > 0


def test_stdio_employee_returns_requested_employee(stdio_round_trip):
    assert stdio_round_trip.employee["employee_id"] == "E001"


def test_stdio_inventory_returns_requested_item(stdio_round_trip):
    assert stdio_round_trip.inventory["item"] == "monitor"


def test_stdio_inventory_includes_stock_count(stdio_round_trip):
    assert "in_stock" in stdio_round_trip.inventory


@pytest.mark.parametrize("phase", [0, 1], ids=["session", "reviewer"])
def test_unpriced_earlier_approvals_are_visible_to_session_and_reviewer(phase):
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
    assert (
        "Earlier approvals missing price or department: laptop"
        in client.calls[phase]["messages"][0]["content"]
    )
