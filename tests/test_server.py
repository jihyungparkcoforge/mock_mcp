import asyncio
import sqlite3
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from equipment_mcp.server import create_server
from equipment_mcp.storage import default_data, default_tickets
from tests.mcp_helpers import require_tool_result, tool_result_json

EXPECTED_TOOLS = {
    "get_employee_info",
    "get_policy_limits",
    "check_request_eligibility",
    "flag_for_human_review",
    "get_item_catalog",
    "get_department_budget",
    "check_inventory",
    "get_open_requests",
}


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("EQUIPMENT_TICKET_DB", str(tmp_path / "tickets.sqlite3"))
    return create_server(default_data(), default_tickets())


def _call(mcp: MCPServer, name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = require_tool_result(asyncio.run(mcp.call_tool(name, args)))
    assert not result.is_error, result.content
    return tool_result_json(result)


def test_registers_all_tools(mcp):
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == EXPECTED_TOOLS


@pytest.mark.parametrize("name", sorted(EXPECTED_TOOLS))
def test_tools_have_descriptions(mcp, name):
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    assert tools[name].description and tools[name].description.strip()


def test_flag_tool_hides_internal_params(mcp):
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    props = set(tools["flag_for_human_review"].input_schema["properties"])
    assert props == {"employee_id", "request", "reason", "idempotency_key"}


def test_flag_tool_idempotency_key_is_optional(mcp):
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert "idempotency_key" not in tools["flag_for_human_review"].input_schema["required"]


def test_get_employee_info_through_server(mcp):
    assert _call(mcp, "get_employee_info", {"employee_id": "E001"})["role"] == "standard"


def test_get_policy_limits_through_server(mcp):
    assert _call(mcp, "get_policy_limits", {"role": "manager"})["role"] == "manager"


def test_check_request_eligibility_through_server(mcp):
    r = _call(mcp, "check_request_eligibility", {"employee_id": "E003", "item": "webcam"})
    assert r["status"] == "not_in_policy"


@pytest.fixture
def server_ticket(mcp, tmp_path):
    database = tmp_path / "tickets.sqlite3"
    ticket = _call(
        mcp,
        "flag_for_human_review",
        {"employee_id": "E003", "request": "4K webcam", "reason": "Item not in policy"},
    )
    return ticket, database


def test_server_ticket_returns_id(server_ticket):
    ticket, _ = server_ticket
    assert ticket["ticket_id"] == "ESC-0001"


def test_server_ticket_is_pending_review(server_ticket):
    ticket, _ = server_ticket
    assert ticket["status"] == "pending_review"


def test_server_ticket_is_persisted(server_ticket):
    _, database = server_ticket
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1


def test_catalog_through_server(mcp):
    assert _call(mcp, "get_item_catalog", {"item": "monitor"})["price"] == 300


def test_budget_through_server(mcp):
    assert _call(mcp, "get_department_budget", {"department": "Operations"})["remaining"] == 150


def test_inventory_through_server(mcp):
    assert _call(mcp, "check_inventory", {"item": "headset"})["in_stock"] == 0


def test_open_requests_through_server(mcp):
    assert _call(mcp, "get_open_requests", {"employee_id": "E002"})["open_requests"][0]["item"] == (
        "monitor"
    )
