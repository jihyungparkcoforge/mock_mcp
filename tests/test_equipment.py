import asyncio
import sqlite3
from datetime import date, datetime

import pytest
from mcp.types import CallToolResult

from equipment_mcp import equipment
from equipment_mcp.repositories import (
    JsonEquipmentData,
    MemoryEquipmentData,
    MemoryTicketStore,
    SqliteTicketStore,
)
from equipment_mcp.server import create_server
from tests.mcp_helpers import require_tool_result, tool_result_json


# Use real in-memory repositories to isolate business rules from filesystem data.
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


@pytest.mark.parametrize(
    "today,expected",
    [(date(2026, 11, 23), "eligible_refresh"), (date(2026, 11, 22), "not_eligible")],
    ids=["refresh-date", "day-before-refresh"],
)
def test_memory_eligibility_uses_injected_clock(today, expected):
    result = equipment.check_request_eligibility("e001", "laptop", data=sample_data(), today=today)
    assert result["status"] == expected


def test_memory_budget_returns_remaining_balance():
    assert equipment.get_department_budget("it", data=sample_data())["remaining"] == 2000


def test_memory_catalog_normalizes_item():
    assert equipment.get_item_catalog("Laptop", data=sample_data())["price"] == 1400


def test_memory_inventory_returns_stock():
    assert equipment.check_inventory("laptop", data=sample_data())["in_stock"] == 2


def test_memory_open_requests_defaults_to_empty():
    assert equipment.get_open_requests("E001", data=sample_data())["open_requests"] == []


@pytest.fixture
def memory_ticket():
    tickets = MemoryTicketStore()
    ticket = equipment.flag_for_human_review(
        "e001",
        "Original\nrequest",
        "review",
        data=sample_data(),
        tickets=tickets,
        now=datetime(2026, 11, 23, 10),
    )
    return ticket, tickets


def test_memory_ticket_returns_allocated_id(memory_ticket):
    ticket, _ = memory_ticket
    assert ticket["ticket_id"] == "ESC-0001"


def test_memory_ticket_uses_injected_clock(memory_ticket):
    _, tickets = memory_ticket
    assert tickets.tickets[0]["created_at"] == "2026-11-23T10:00:00"


def test_memory_ticket_preserves_original_request(memory_ticket):
    _, tickets = memory_ticket
    assert tickets.tickets[0]["request"] == "Original\nrequest"


def test_memory_ticket_rejects_blank_reason(memory_ticket):
    _, tickets = memory_ticket
    result = equipment.flag_for_human_review(
        "E001", "request", " ", data=sample_data(), tickets=tickets
    )
    assert result == {"error": "reason_required"}


def test_rejected_memory_ticket_does_not_append(memory_ticket):
    _, tickets = memory_ticket
    equipment.flag_for_human_review("E001", "request", " ", data=sample_data(), tickets=tickets)
    assert len(tickets.tickets) == 1


def test_mcp_catalog_uses_injected_memory_data():
    data = sample_data()
    server = create_server(data, MemoryTicketStore())
    result = asyncio.run(server.call_tool("get_item_catalog", {"item": "laptop"}))
    assert tool_result_json(result) == equipment.get_item_catalog("laptop", data=data)


@pytest.fixture
def mcp_memory_ticket() -> tuple[CallToolResult, MemoryTicketStore]:
    tickets = MemoryTicketStore()
    server = create_server(sample_data(), tickets)
    result = asyncio.run(
        server.call_tool(
            "flag_for_human_review",
            {"employee_id": "E001", "request": "New laptop", "reason": "review"},
        )
    )
    return require_tool_result(result), tickets


def test_mcp_memory_ticket_succeeds(mcp_memory_ticket):
    result, _ = mcp_memory_ticket
    assert not result.is_error


def test_mcp_memory_ticket_returns_id(mcp_memory_ticket):
    result, _ = mcp_memory_ticket
    assert tool_result_json(result)["ticket_id"] == "ESC-0001"


def test_mcp_memory_ticket_is_stored(mcp_memory_ticket):
    _, tickets = mcp_memory_ticket
    assert len(tickets.tickets) == 1


def test_json_repository_construction_does_not_create_file(tmp_path):
    path = tmp_path / "catalog.json"
    JsonEquipmentData({"catalog": path})
    assert not path.exists()


def test_sqlite_repository_construction_does_not_create_file(tmp_path):
    tickets = SqliteTicketStore(tmp_path / "tickets.sqlite3")
    assert not tickets.path.exists()


def test_json_repository_reads_file_created_after_construction(tmp_path):
    path = tmp_path / "catalog.json"
    data = JsonEquipmentData({"catalog": path})
    path.write_text('{"items": {}}')
    assert data.read("catalog") == {"items": {}}


@pytest.fixture
def sqlite_tickets(tmp_path):
    tickets = SqliteTicketStore(tmp_path / "tickets.sqlite3")
    first = tickets.append({"request": "one"})
    second = tickets.append({"request": "two"})
    return tickets, [first, second]


@pytest.mark.parametrize("index,expected", [(0, "ESC-0001"), (1, "ESC-0002")])
def test_sqlite_repository_allocates_sequential_ids(sqlite_tickets, index, expected):
    _, results = sqlite_tickets
    assert results[index]["ticket_id"] == expected


def test_sqlite_repository_persists_both_tickets(sqlite_tickets):
    tickets, _ = sqlite_tickets
    with sqlite3.connect(tickets.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 2


@pytest.fixture
def unknown_employee_ticket() -> tuple[CallToolResult, MemoryTicketStore]:
    tickets = MemoryTicketStore()
    server = create_server(sample_data(), tickets)
    result = asyncio.run(
        server.call_tool(
            "flag_for_human_review",
            {"employee_id": " e999 ", "request": "Laptop", "reason": "Needs review"},
        )
    )
    return require_tool_result(result), tickets


def test_mcp_ticket_tool_rejects_unknown_employee(unknown_employee_ticket):
    result, _ = unknown_employee_ticket
    assert tool_result_json(result) == {
        "error": "employee_not_found",
        "employee_id": "E999",
    }


def test_mcp_unknown_employee_ticket_is_not_written(unknown_employee_ticket):
    _, tickets = unknown_employee_ticket
    assert tickets.tickets == []
