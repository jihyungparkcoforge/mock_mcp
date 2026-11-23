"""Plain-Python business logic for the equipment request system.

These functions are tested directly (tests/test_tools.py) and wrapped as MCP
tools in server.py. Keep them free of any MCP imports.

Error convention: lookups that fail return a dict with an "error" key instead
of raising, so the agent can observe the failure and react to it.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from logging import raiseExceptions
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
EMPLOYEES_PATH = DATA_DIR / "employees.json"
POLICIES_PATH = DATA_DIR / "policies.json"
ESCALATION_LOG_PATH = DATA_DIR / "escalations.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_employees() -> dict[str, Any]:
    """Load employees.json as a dict keyed by employee ID.

    Read on every call (no cache) so edits to the JSON show up without a restart.
    A missing or malformed file raises (FileNotFoundError / json.JSONDecodeError):
    that is a setup bug, not something the agent should see as a normal result.
    """
    with EMPLOYEES_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def load_policies() -> dict[str, Any]:
    """Load policies.json as a dict keyed by role."""
    with POLICIES_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def normalize_employee_id(employee_id: str) -> str:
    """Strip whitespace and upper-case an employee ID (" e001 " -> "E001")."""
    return employee_id.strip().upper()


def normalize_key(value: str) -> str:
    """Strip whitespace and lower-case a role or item name (" Laptop " -> "laptop")."""
    return value.strip().lower()


# ---------------------------------------------------------------------------
# Tool 1
# ---------------------------------------------------------------------------

def get_employee_info(employee_id: str, today: date | None = None) -> dict[str, Any]:
    """Return role, tenure and current equipment for an employee.

    Success:
        {
            "employee_id": "E001",
            "name": "Alice Kim",
            "role": "standard",
            "hire_date": "2021-02-01",
            "tenure_years": 5.8,          # round(days / 365.25, 1)
            "equipment": [{"item": "laptop", "acquired": "2022-01-10"}, ...],
        }
    Unknown or empty ID:
        {"error": "employee_not_found", "employee_id": <normalized id>}
    """

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





# ---------------------------------------------------------------------------
# Tool 2
# ---------------------------------------------------------------------------

def get_policy_limits(role: str) -> dict[str, Any]:
    """Return what a role is eligible for and how often.

    Success:
        {
            "role": "standard",
            "items": {
                "monitor": {"max_quantity": 1, "refresh_years": 3},
                "laptop": {"max_quantity": 1, "refresh_years": 4},
            },
        }
    Unknown role:
        {"error": "role_not_found", "role": <normalized role>,
         "available_roles": ["engineer", "manager", "standard"]}   # sorted

    The returned dict must be a copy: mutating it must not change later results.
    """
    pass


# ---------------------------------------------------------------------------
# Tool 3
# ---------------------------------------------------------------------------

def check_request_eligibility(
    employee_id: str, item: str, today: date | None = None
) -> dict[str, Any]:
    """Check whether a request for `item` falls within the employee's role policy.

    Rules:
      * held < max_quantity                        -> eligible, "eligible_new"
      * held >= max_quantity and the OLDEST unit's
        acquired + refresh_years <= today          -> eligible, "eligible_refresh"
      * otherwise                                  -> not eligible, "not_eligible"
      * item not in the role's policy              -> eligible None, "not_in_policy"
      * unknown employee                           -> eligible None, "employee_not_found"

    Result keys (always present):
        employee_id, item, eligible (True/False/None), status, reason (human-readable)
    Extra keys when the item is in policy:
        currently_held, max_quantity, refresh_years,
        next_eligible_date ("YYYY-MM-DD" when status == "not_eligible", else None)
    """
    pass


# ---------------------------------------------------------------------------
# Tool 4 (side-effecting)
# ---------------------------------------------------------------------------

def flag_for_human_review(
    employee_id: str,
    request: str,
    reason: str,
    log_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Escalate a request: append one JSON line to the escalation log.

    Success (the same dict is written as one line to the log):
        {
            "ticket_id": "ESC-0001",     # sequential: number of existing lines + 1
            "status": "pending_review",
            "employee_id": "E003",       # normalized
            "request": "...",
            "reason": "...",
            "created_at": "2026-11-23T10:00:00",   # now.isoformat(timespec="seconds")
        }
    Empty/blank request or reason (nothing is written):
        {"error": "request_required"} / {"error": "reason_required"}
    """
    pass
