"""Translate MCP sessions and wire results to the application's tool interface."""

import json
from typing import Any

from equipment_mcp.formatting import tool_result_text
from equipment_mcp.models import ToolDefinition, ToolExecutionError


class McpToolExecutor:
    def __init__(self, session, definitions: list[ToolDefinition]):
        self.session = session
        self._definitions = list(definitions)

    @property
    def definitions(self) -> list[ToolDefinition]:
        return list(self._definitions)

    @classmethod
    async def discover(cls, session):
        from mcp.types import PaginatedRequestParams

        definitions = []
        cursor = None
        while True:
            page = await session.list_tools(
                params=PaginatedRequestParams(cursor=cursor) if cursor else None
            )
            definitions.extend(
                ToolDefinition(t.name, t.description or "", t.input_schema) for t in page.tools
            )
            cursor = page.next_cursor
            if not cursor:
                return cls(session, definitions)

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in {t.name for t in self._definitions}:
            raise ToolExecutionError(f"Unknown tool {name!r}")
        # Protocol/connection errors propagate, preserving the workflow's partial results.
        response = await self.session.call_tool(name, arguments)
        text = tool_result_text(response.content)
        if response.is_error:
            raise ToolExecutionError(text)
        result = response.structured_content
        if result is None:
            try:
                result = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ToolExecutionError("Tool returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ToolExecutionError("Tool result must be an object")
        return result
