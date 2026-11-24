"""Default JSON wiring and legacy function API; business rules live in equipment.py."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from equipment_mcp import equipment
from equipment_mcp.equipment import (
    add_years as add_years,
)
from equipment_mcp.equipment import (
    normalize_employee_id as normalize_employee_id,
)
from equipment_mcp.equipment import (
    normalize_key as normalize_key,
)
from equipment_mcp.repositories import JsonEquipmentData, JsonTicketStore

DATA_DIR = Path(__file__).parent / "data"
EMPLOYEES_PATH = DATA_DIR / "employees.json"
POLICIES_PATH = DATA_DIR / "policies.json"
ESCALATION_LOG_PATH = DATA_DIR / "escalations.jsonl"
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


def load_json(path: Path) -> Any:
    return JsonEquipmentData({"data": path}).read("data")


def load_employees() -> dict[str, Any]:
    return load_json(EMPLOYEES_PATH)


def load_policies() -> dict[str, Any]:
    return load_json(POLICIES_PATH)


def get_employee_info(employee_id: str, today: date | None = None) -> dict[str, Any]:
    return equipment.get_employee_info(
        employee_id, data=default_data(), today=today or date.today()
    )


def get_policy_limits(role: str) -> dict[str, Any]:
    return equipment.get_policy_limits(role, data=default_data())


def check_request_eligibility(
    employee_id: str, item: str, today: date | None = None
) -> dict[str, Any]:
    return equipment.check_request_eligibility(
        employee_id, item, data=default_data(), today=today or date.today()
    )


def get_item_catalog(item: str) -> dict[str, Any]:
    return equipment.get_item_catalog(item, data=default_data())


def get_department_budget(department: str) -> dict[str, Any]:
    return equipment.get_department_budget(department, data=default_data())


def check_inventory(item: str) -> dict[str, Any]:
    return equipment.check_inventory(item, data=default_data())


def get_open_requests(employee_id: str) -> dict[str, Any]:
    return equipment.get_open_requests(employee_id, data=default_data())


def flag_for_human_review(
    employee_id: str,
    request: str,
    reason: str,
    log_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    return equipment.flag_for_human_review(
        employee_id,
        request,
        reason,
        tickets=JsonTicketStore(log_path or ESCALATION_LOG_PATH),
        now=now or datetime.now(),
    )
