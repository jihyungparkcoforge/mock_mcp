"""Use SDK MCP tools; expose JSON facts to the equipment workflow."""

import json
from typing import Any

from anthropic.lib.tools import BetaAsyncFunctionTool, ToolError
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp.types import PaginatedRequestParams

from equipment_mcp.formatting import tool_result_text
from equipment_mcp.models import ToolExecutionError


class McpToolExecutor:
    def __init__(self, tools: list[BetaAsyncFunctionTool]):
        self._tools = {tool.name: tool for tool in tools}

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return [tool.to_dict() for tool in self._tools.values()]

    @classmethod
    async def discover(cls, session):
        tools = []
        cursor = None
        while True:
            page = await session.list_tools(
                params=PaginatedRequestParams(cursor=cursor) if cursor else None
            )
            tools.extend(async_mcp_tool(tool, session) for tool in page.tools)
            cursor = page.next_cursor
            if not cursor:
                return cls(tools)

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self._tools:
            raise ToolExecutionError(f"Unknown tool {name!r}")
        try:
            content = await self._tools[name].call(arguments)
        except ToolError as exc:
            raise ToolExecutionError(tool_result_text(exc.content)) from exc
        # The SDK handles MCP conversion; our policy needs JSON objects, not content blocks.
        # Connection failures propagate, preserving the workflow's partial results.
        try:
            result = json.loads(tool_result_text(content))
        except json.JSONDecodeError as exc:
            raise ToolExecutionError("Tool returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ToolExecutionError("Tool result must be an object")
        return result
