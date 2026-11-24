"""Real MCP Streamable HTTP messages over an in-process ASGI transport; no LLM."""

import asyncio
import sqlite3
from threading import Barrier

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from equipment_mcp.mcp_executor import McpToolExecutor
from equipment_mcp.repositories import MemoryEquipmentData, MemoryTicketStore, SqliteTicketStore
from equipment_mcp.server import create_server


def test_concurrent_http_sessions_allocate_and_reuse_tickets(tmp_path):
    path = tmp_path / "tickets.sqlite3"
    server = create_server(MemoryEquipmentData({}), SqliteTicketStore(path))
    app = server.streamable_http_app(json_response=True, stateless_http=True)

    async def batch(keys):
        ready = 0
        start = asyncio.Event()

        async def request(key):
            nonlocal ready
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
                async with streamable_http_client(
                    "http://127.0.0.1:8000/mcp",
                    http_client=http,
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await McpToolExecutor.discover(session)
                        ready += 1
                        if ready == len(keys):
                            start.set()
                        await start.wait()
                        return await tools.call(
                            "flag_for_human_review",
                            {
                                "employee_id": "E001",
                                "request": "New webcam",
                                "reason": "Not in policy",
                                "idempotency_key": key,
                            },
                        )

        return await asyncio.gather(*(request(key) for key in keys))

    async def run():
        async with asyncio.timeout(30), server.session_manager.run():
            different = await batch([f"request-{i}" for i in range(20)])
            assert len({t["ticket_id"] for t in different}) == 20
            retries = await batch(["request-0"] * 20)
            assert all(t == different[0] for t in retries)

    asyncio.run(run())
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 20


def test_independent_reads_overlap_on_one_http_session():
    barrier = Barrier(3)

    class ConcurrentData(MemoryEquipmentData):
        def read(self, table):
            # All three MCP calls must reach the server before any can finish.
            barrier.wait(timeout=5)
            return super().read(table)

    data = ConcurrentData(
        {
            "catalog": {
                "items": {"headset": {"price": 120}},
                "currency": "USD",
                "auto_approve_limit": 2000,
            },
            "inventory": {"monitor": {"in_stock": 2, "restock_date": None}},
            "budgets": {"Finance": {"annual_budget": 1000, "spent": 200}},
        }
    )
    server = create_server(data, MemoryTicketStore())
    app = server.streamable_http_app(json_response=True, stateless_http=True)

    async def run():
        async with asyncio.timeout(15), server.session_manager.run():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
                async with streamable_http_client(
                    "http://127.0.0.1:8000/mcp", http_client=http
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await McpToolExecutor.discover(session)
                        catalog, inventory, budget = await asyncio.gather(
                            tools.call("get_item_catalog", {"item": "headset"}),
                            tools.call("check_inventory", {"item": "monitor"}),
                            tools.call("get_department_budget", {"department": "Finance"}),
                        )
                        assert catalog["price"] == 120
                        assert inventory["in_stock"] == 2
                        assert budget["remaining"] == 800

    asyncio.run(run())
