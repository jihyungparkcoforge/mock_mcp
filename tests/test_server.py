"""Checks that the MCP server registers the four tools and the wrappers delegate to tools.py.

Calls the MCPServer in-process (no subprocess), so it runs in CI without an LLM.
"""

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
    result = asyncio.run(mcp.call_tool(name, args))  # CallToolResult (mcp 2.x)
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
