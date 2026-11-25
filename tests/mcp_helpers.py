"""Check MCP response variants before reading the JSON used by server tests."""

import json
from typing import Any

from mcp.types import CallToolResult, InputRequiredResult, TextContent


def require_tool_result(result: CallToolResult | InputRequiredResult) -> CallToolResult:
    # These tools finish immediately; an input request is an unexpected test result.
    assert isinstance(result, CallToolResult), (
        f"Expected CallToolResult, got {type(result).__name__}"
    )
    return result


def tool_result_json(result: CallToolResult | InputRequiredResult) -> dict[str, Any]:
    result = require_tool_result(result)
    assert result.content, "Expected JSON text in the MCP result"
    content = result.content[0]
    assert isinstance(content, TextContent), f"Expected TextContent, got {type(content).__name__}"
    return json.loads(content.text)
