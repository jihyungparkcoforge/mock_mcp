"""Equipment business operations over injected data and ticket repositories."""

from __future__ import annotations

import copy
from datetime import date, datetime
from typing import Any

from equipment_mcp.ports import EquipmentData, TicketStore


def normalize_employee_id(employee_id: str) -> str:
    return employee_id.strip().upper()


def normalize_key(value: str) -> str:
    return value.strip().lower()


def add_years(d: date, years: int) -> date:
    # Feb 29 -> Feb 28 in non-leap years
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def get_employee_info(
    employee_id: str, *, data: EquipmentData, today: date | None = None
) -> dict[str, Any]:
    today = today or date.today()
    eid = normalize_employee_id(employee_id)

    record = data.read("employees").get(eid)
    if record is None:
        return {"error": "employee_not_found", "employee_id": eid}

    hire_date = date.fromisoformat(record["hire_date"])
    return {
        "employee_id": eid,
        "name": record["name"],
        "role": record["role"],
        "department": record["department"],
        "employment_status": record["employment_status"],
        "hire_date": record["hire_date"],
        "tenure_years": round((today - hire_date).days / 365.25, 1),
        "equipment": [dict(unit) for unit in record["equipment"]],
    }


def get_policy_limits(role: str, *, data: EquipmentData) -> dict[str, Any]:
    role_name = normalize_key(role)

    record = data.read("policies").get(role_name)
    if record is None:
        return {
            "error": "role_not_found",
            "role": role_name,
            "available_roles": sorted(data.read("policies")),
        }

    return {
        "role": role_name,
        "items": copy.deepcopy(record),
    }


def check_request_eligibility(
    employee_id: str, item: str, *, data: EquipmentData, today: date | None = None
) -> dict[str, Any]:
    today = today or date.today()
    eid = normalize_employee_id(employee_id)
    item_name = normalize_key(item)
    result: dict[str, Any] = {"employee_id": eid, "item": item_name}

    employee = data.read("employees").get(eid)
    if employee is None:
        return result | {
            "eligible": None,
            "status": "employee_not_found",
            "reason": f"No employee with ID {eid!r}.",
        }

    role = employee["role"]
    role_policy = data.read("policies").get(role)
    if role_policy is None:
        return result | {
            "eligible": None,
            "status": "role_not_found",
            "reason": f"Role {role!r} has no equipment policy.",
        }

    limit = role_policy.get(item_name)
    if limit is None:
        return result | {
            "eligible": None,
            "status": "not_in_policy",
            "reason": f"{item_name!r} is not covered by the {role} policy.",
        }

    max_qty = limit["max_quantity"]
    refresh_years = limit["refresh_years"]
    acquired = [
        date.fromisoformat(unit["acquired"])
        for unit in employee["equipment"]
        if unit["item"] == item_name
    ]
    held = len(acquired)
    result.update(
        {
            "currently_held": held,
            "max_quantity": max_qty,
            "refresh_years": refresh_years,
            "next_eligible_date": None,
        }
    )

    if held < max_qty:
        return result | {
            "eligible": True,
            "status": "eligible_new",
            "reason": f"Holds {held} of {max_qty} allowed {item_name}(s) for role {role}.",
        }

    oldest = min(acquired)
    due = add_years(oldest, refresh_years)
    if due <= today:
        return result | {
            "eligible": True,
            "status": "eligible_refresh",
            "reason": (
                f"Oldest {item_name} acquired {oldest.isoformat()}; "
                f"{refresh_years}-year refresh was due {due.isoformat()}."
            ),
        }

    return result | {
        "eligible": False,
        "status": "not_eligible",
        "reason": (
            f"At the {max_qty}-unit limit and oldest {item_name} "
            f"({oldest.isoformat()}) is not due for refresh until {due.isoformat()}."
        ),
        "next_eligible_date": due.isoformat(),
    }


def get_item_catalog(item: str, *, data: EquipmentData) -> dict[str, Any]:
    item_name = normalize_key(item)
    catalog = data.read("catalog")
    entry = catalog["items"].get(item_name)
    if entry is None:
        return {"error": "item_not_in_catalog", "item": item_name}
    return {
        "item": item_name,
        "price": entry["price"],
        "currency": catalog["currency"],
        "auto_approve_limit": catalog["auto_approve_limit"],
    }


def get_department_budget(department: str, *, data: EquipmentData) -> dict[str, Any]:
    budgets = data.read("budgets")
    wanted = department.strip()
    name = next((d for d in budgets if d.lower() == wanted.lower()), None)
    if name is None:
        return {
            "error": "department_not_found",
            "department": wanted,
            "available_departments": sorted(budgets),
        }
    entry = budgets[name]
    return {
        "department": name,
        "annual_budget": entry["annual_budget"],
        "spent": entry["spent"],
        "remaining": entry["annual_budget"] - entry["spent"],
    }


def check_inventory(item: str, *, data: EquipmentData) -> dict[str, Any]:
    item_name = normalize_key(item)
    entry = data.read("inventory").get(item_name)
    if entry is None:
        return {"error": "item_not_in_inventory", "item": item_name}
    return {
        "item": item_name,
        "in_stock": entry["in_stock"],
        "restock_date": entry["restock_date"],
    }


def get_open_requests(employee_id: str, *, data: EquipmentData) -> dict[str, Any]:
    eid = normalize_employee_id(employee_id)
    if eid not in data.read("employees"):
        return {"error": "employee_not_found", "employee_id": eid}
    requests = data.read("open_requests").get(eid, [])
    return {"employee_id": eid, "open_requests": [dict(r) for r in requests]}


def flag_for_human_review(
    employee_id: str,
    request: str,
    reason: str,
    *,
    tickets: TicketStore,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not request.strip():
        return {"error": "request_required"}
    if not reason.strip():
        return {"error": "reason_required"}
    return tickets.append(
        {
            "status": "pending_review",
            "employee_id": normalize_employee_id(employee_id),
            "request": request,
            "reason": reason,
            "created_at": (now or datetime.now()).isoformat(timespec="seconds"),
        }
    )
