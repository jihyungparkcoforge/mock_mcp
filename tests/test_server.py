import asyncio
import json
import sqlite3

import pytest

from equipment_mcp.server import create_server
from equipment_mcp.storage import default_data, default_tickets

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


def _call(mcp, name, args):
    result = asyncio.run(mcp.call_tool(name, args))
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


def test_registers_all_tools(mcp):
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == EXPECTED_TOOLS


def test_tools_have_descriptions(mcp):
    for tool in asyncio.run(mcp.list_tools()):
        assert tool.description and tool.description.strip(), tool.name


def test_flag_tool_hides_internal_params(mcp):
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    props = set(tools["flag_for_human_review"].input_schema["properties"])
    assert props == {"employee_id", "request", "reason", "idempotency_key"}
    assert "idempotency_key" not in tools["flag_for_human_review"].input_schema["required"]


def test_get_employee_info_through_server(mcp):
    assert _call(mcp, "get_employee_info", {"employee_id": "E001"})["role"] == "standard"


def test_get_policy_limits_through_server(mcp):
    assert _call(mcp, "get_policy_limits", {"role": "manager"})["role"] == "manager"


def test_check_request_eligibility_through_server(mcp):
    r = _call(mcp, "check_request_eligibility", {"employee_id": "E003", "item": "webcam"})
    assert r["status"] == "not_in_policy"


def test_flag_for_human_review_through_server(mcp, tmp_path):
    database = tmp_path / "tickets.sqlite3"
    ticket = _call(
        mcp,
        "flag_for_human_review",
        {"employee_id": "E003", "request": "4K webcam", "reason": "Item not in policy"},
    )
    assert ticket["ticket_id"] == "ESC-0001"
    assert ticket["status"] == "pending_review"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1


def test_new_fact_tools_through_server(mcp):
    assert _call(mcp, "get_item_catalog", {"item": "monitor"})["price"] == 300
    assert _call(mcp, "get_department_budget", {"department": "Operations"})["remaining"] == 150
    assert _call(mcp, "check_inventory", {"item": "headset"})["in_stock"] == 0
    assert _call(mcp, "get_open_requests", {"employee_id": "E002"})["open_requests"][0]["item"] == (
        "monitor"
    )
