import asyncio
import json
import sqlite3
from datetime import date, datetime

from equipment_mcp import equipment
from equipment_mcp.repositories import (
    JsonEquipmentData,
    MemoryEquipmentData,
    MemoryTicketStore,
    SqliteTicketStore,
)
from equipment_mcp.server import create_server


def sample_data():
    return MemoryEquipmentData(
        {
            "employees": {
                "E001": {
                    "name": "Test User",
                    "role": "engineer",
                    "department": "IT",
                    "employment_status": "active",
                    "hire_date": "2020-01-01",
                    "equipment": [{"item": "laptop", "acquired": "2023-11-23"}],
                }
            },
            "policies": {"engineer": {"laptop": {"max_quantity": 1, "refresh_years": 3}}},
            "catalog": {
                "items": {"laptop": {"price": 1400}},
                "currency": "USD",
                "auto_approve_limit": 2000,
            },
            "budgets": {"IT": {"annual_budget": 5000, "spent": 3000}},
            "inventory": {"laptop": {"in_stock": 2, "restock_date": None}},
            "open_requests": {},
        }
    )


def test_business_rules_with_memory_data_and_injected_clock():
    data = sample_data()
    tickets = MemoryTicketStore()
    assert (
        equipment.check_request_eligibility("e001", "laptop", data=data, today=date(2026, 11, 23))[
            "status"
        ]
        == "eligible_refresh"
    )
    assert (
        equipment.check_request_eligibility("E001", "laptop", data=data, today=date(2026, 11, 22))[
            "status"
        ]
        == "not_eligible"
    )
    assert equipment.get_department_budget("it", data=data)["remaining"] == 2000
    assert equipment.get_item_catalog("Laptop", data=data)["price"] == 1400
    assert equipment.check_inventory("laptop", data=data)["in_stock"] == 2
    assert equipment.get_open_requests("E001", data=data)["open_requests"] == []
    assert (
        equipment.flag_for_human_review(
            "e001", "Original\nrequest", "review", tickets=tickets, now=datetime(2026, 11, 23, 10)
        )["ticket_id"]
        == "ESC-0001"
    )
    assert tickets.tickets[0]["created_at"] == "2026-11-23T10:00:00"
    assert tickets.tickets[0]["request"] == "Original\nrequest"
    assert equipment.flag_for_human_review("E001", "request", " ", tickets=tickets) == {
        "error": "reason_required"
    }
    assert len(tickets.tickets) == 1


def test_mcp_server_can_use_memory_repositories_without_global_patching():
    data = sample_data()
    tickets = MemoryTicketStore()
    server = create_server(data, tickets)

    async def run():
        catalog = await server.call_tool("get_item_catalog", {"item": "laptop"})
        assert json.loads(catalog.content[0].text) == equipment.get_item_catalog(
            "laptop", data=data
        )
        ticket = await server.call_tool(
            "flag_for_human_review",
            {
                "employee_id": "E001",
                "request": "New laptop",
                "reason": "review",
            },
        )
        assert not ticket.is_error
        assert json.loads(ticket.content[0].text)["ticket_id"] == "ESC-0001"
        assert len(tickets.tickets) == 1

    asyncio.run(run())


def test_repositories_are_lazy_and_keep_storage_outside_business_logic(tmp_path):
    path = tmp_path / "catalog.json"
    data = JsonEquipmentData({"catalog": path})
    tickets = SqliteTicketStore(tmp_path / "tickets.sqlite3")
    assert not path.exists() and not tickets.path.exists()
    path.write_text('{"items": {}}')
    assert data.read("catalog") == {"items": {}}
    assert tickets.append({"request": "one"})["ticket_id"] == "ESC-0001"
    assert tickets.append({"request": "two"})["ticket_id"] == "ESC-0002"
    with sqlite3.connect(tickets.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 2
