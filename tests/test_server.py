import asyncio
import json

from equipment_mcp.server import mcp

EXPECTED_TOOLS = {
    "get_employee_info",
    "get_policy_limits",
    "check_request_eligibility",
    "flag_for_human_review",
}


def _call(name, args):
    result = asyncio.run(mcp.call_tool(name, args))
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


def test_registers_all_four_tools():
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == EXPECTED_TOOLS


def test_tools_have_descriptions():
    for tool in asyncio.run(mcp.list_tools()):
        assert tool.description and tool.description.strip(), tool.name


def test_flag_tool_hides_internal_params():
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    props = set(tools["flag_for_human_review"].input_schema["properties"])
    assert props == {"employee_id", "request", "reason"}


def test_get_employee_info_through_server():
    assert _call("get_employee_info", {"employee_id": "E001"})["role"] == "standard"


def test_get_policy_limits_through_server():
    assert _call("get_policy_limits", {"role": "manager"})["role"] == "manager"


def test_check_request_eligibility_through_server():
    r = _call("check_request_eligibility", {"employee_id": "E003", "item": "webcam"})
    assert r["status"] == "not_in_policy"


def test_flag_for_human_review_through_server(tmp_path, monkeypatch):
    log = tmp_path / "escalations.jsonl"
    monkeypatch.setattr("equipment_mcp.tools.ESCALATION_LOG_PATH", log)
    ticket = _call(
        "flag_for_human_review",
        {"employee_id": "E003", "request": "4K webcam", "reason": "Item not in policy"},
    )
    assert ticket["ticket_id"] == "ESC-0001"
    assert ticket["status"] == "pending_review"
    assert len(log.read_text().splitlines()) == 1
