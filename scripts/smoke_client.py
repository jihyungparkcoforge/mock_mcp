"""Minimal MCP client: start the server over stdio, list tools, call get_employee_info.

Run:  uv run python scripts/smoke_client.py [EMPLOYEE_ID]

This is the Suggested Approach step 3 evidence (one-tool end-to-end check).
"""

from __future__ import annotations

import asyncio
import sys


async def main(employee_id: str) -> None:
    # TODO:
    #   from mcp import ClientSession, StdioServerParameters
    #   from mcp.client.stdio import stdio_client
    #   params = StdioServerParameters(command=sys.executable, args=["-m", "equipment_mcp.server"])
    #   async with stdio_client(params) as (read, write):
    #       async with ClientSession(read, write) as session:
    #           await session.initialize()
    #           print tool names from await session.list_tools()
    #           print await session.call_tool("get_employee_info", {"employee_id": employee_id})
    pass


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "E001"))
