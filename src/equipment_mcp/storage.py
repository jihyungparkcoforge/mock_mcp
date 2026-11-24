"""Default JSON data and SQLite ticket wiring for the application."""

from __future__ import annotations

import os
from pathlib import Path

from equipment_mcp.repositories import JsonEquipmentData, SqliteTicketStore

DATA_DIR = Path(__file__).parent / "data"
EMPLOYEES_PATH = DATA_DIR / "employees.json"
POLICIES_PATH = DATA_DIR / "policies.json"
TICKET_DB_PATH = DATA_DIR / "tickets.sqlite3"
CATALOG_PATH = DATA_DIR / "catalog.json"
BUDGETS_PATH = DATA_DIR / "budgets.json"
INVENTORY_PATH = DATA_DIR / "inventory.json"
OPEN_REQUESTS_PATH = DATA_DIR / "open_requests.json"


def default_data() -> JsonEquipmentData:
    return JsonEquipmentData(
        {
            "employees": EMPLOYEES_PATH,
            "policies": POLICIES_PATH,
            "catalog": CATALOG_PATH,
            "budgets": BUDGETS_PATH,
            "inventory": INVENTORY_PATH,
            "open_requests": OPEN_REQUESTS_PATH,
        }
    )


def default_tickets() -> SqliteTicketStore:
    return SqliteTicketStore(Path(os.environ.get("EQUIPMENT_TICKET_DB") or TICKET_DB_PATH))
