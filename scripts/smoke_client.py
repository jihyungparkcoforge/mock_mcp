"""Connect to the MCP server and call get_employee_info.

Run: uv run python scripts/smoke_client.py [EMPLOYEE_ID]
Uses MCP_SERVER_URL (Streamable HTTP) if set, otherwise starts the server over stdio.
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv
from mcp import Client

from equipment_mcp.transport import server_url, stdio_params


async def main(employee_id: str) -> None:
    print("Transport:", server_url() or "stdio")
    async with Client(server_url() or stdio_params(), mode="legacy") as client:
        tools = await client.list_tools()
        print("Registered tools:", [t.name for t in tools.tools])

        result = await client.call_tool("get_employee_info", {"employee_id": employee_id})
        print(f"get_employee_info({employee_id!r}) -> is_error={result.is_error}")
        print(result.content[0].text)


if __name__ == "__main__":
    load_dotenv()
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "E001"))
