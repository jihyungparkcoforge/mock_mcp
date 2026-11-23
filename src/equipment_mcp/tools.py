"""Equipment request logic. Lookup failures return {"error": ...} instead of raising."""

from __future__ import annotations

import copy
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
EMPLOYEES_PATH = DATA_DIR / "employees.json"
POLICIES_PATH = DATA_DIR / "policies.json"
ESCALATION_LOG_PATH = DATA_DIR / "escalations.jsonl"


def load_employees() -> dict[str, Any]:
    with EMPLOYEES_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def load_policies() -> dict[str, Any]:
    with POLICIES_PATH.open(encoding="utf-8") as f:
        return json.load(f)


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


def get_employee_info(employee_id: str, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    eid = normalize_employee_id(employee_id)

    record = load_employees().get(eid)
    if record is None:
        return {"error": "employee_not_found", "employee_id": eid}

    hire_date = date.fromisoformat(record["hire_date"])
    return {
        "employee_id": eid,
        "name": record["name"],
        "role": record["role"],
        "hire_date": record["hire_date"],
        "tenure_years": round((today - hire_date).days / 365.25, 1),
        "equipment": [dict(unit) for unit in record["equipment"]],
    }


def get_policy_limits(role: str) -> dict[str, Any]:
    role_name = normalize_key(role)

    record = load_policies().get(role_name)
    if record is None:
        return {
            "error": "role_not_found",
            "role": role_name,
            "available_roles": sorted(load_policies()),
        }

    return {
        "role": role_name,
        "items": copy.deepcopy(record),
    }


def check_request_eligibility(
    employee_id: str, item: str, today: date | None = None
) -> dict[str, Any]:
    today = today or date.today()
    eid = normalize_employee_id(employee_id)
    item_name = normalize_key(item)
    result: dict[str, Any] = {"employee_id": eid, "item": item_name}

    employee = load_employees().get(eid)
    if employee is None:
        return result | {
            "eligible": None,
            "status": "employee_not_found",
            "reason": f"No employee with ID {eid!r}.",
        }

    role = employee["role"]
    role_policy = load_policies().get(role)
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


def flag_for_human_review(
    employee_id: str,
    request: str,
    reason: str,
    log_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not request.strip():
        return {"error": "request_required"}
    if not reason.strip():
        return {"error": "reason_required"}

    log_path = log_path or ESCALATION_LOG_PATH
    now = now or datetime.now()

    existing = 0
    if log_path.exists():
        with log_path.open(encoding="utf-8") as f:
            existing = sum(1 for line in f if line.strip())

    ticket = {
        "ticket_id": f"ESC-{existing + 1:04d}",
        "status": "pending_review",
        "employee_id": normalize_employee_id(employee_id),
        "request": request,
        "reason": reason,
        "created_at": now.isoformat(timespec="seconds"),
    }

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ticket, ensure_ascii=False) + "\n")
    return ticket
