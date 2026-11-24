"""Pure approval and review policy; accepts facts rather than loading services."""

from __future__ import annotations

from typing import Any

from equipment_mcp.models import EquipmentRequest


def validate_requests(request: str, tasks: list[EquipmentRequest]) -> None:
    if not tasks:
        raise ValueError("No new equipment request found in the message")
    seen_items: set[str] = set()
    seen_ids: set[str] = set()
    for task in tasks:
        if (
            not task.item
            or task.item != task.item.strip().lower()
            or not task.text.strip()
            or task.text not in request
            or task.item in seen_items
            or not task.request_id
            or task.request_id in seen_ids
        ):
            raise ValueError("Invalid request extraction: empty, duplicate or ungrounded item")
        seen_items.add(task.item)
        seen_ids.add(task.request_id)


VALID_DECISIONS = ("approve", "deny", "escalate")


MAX_REFLECTION_REJECTIONS = 2


DECISION_RULES = """Approve only when every one of these holds:
- the role policy allows the item (eligibility status eligible_new or eligible_refresh)
- the employee's employment_status is active
- there is no open request for the same item
- the item's price is within the auto-approve limit
- the department's remaining budget covers the price
- the item is in stock

Deny when any of these holds:
- eligibility status not_eligible and the request gives no damage or failure reason
  (give the next eligible date)
- the employee is leaving
- an open request for the same item already exists (give its request ID)
- employee_not_found (ask the employee to confirm their ID)

Escalate when any of these holds:
- not_eligible but the request says the device is broken, damaged, or not working
- the item is not in policy or not in the catalog
- the price is above the auto-approve limit
- the remaining department budget is below the price
- the item is out of stock
- the employee is on leave
- role_not_found, missing data, or anything the data cannot settle

If both a deny reason and an escalate reason apply, deny.
If the data does not clearly support approve or deny, escalate. Never guess."""


def validate_decision(decision: dict[str, Any], created_tickets: list[str]) -> list[str]:
    """Return problems with a submit_decision input (empty list means valid)."""
    result = []

    if decision.get("decision") == "escalate" and decision.get("ticket_id") not in created_tickets:
        result.append("ticket_id is missing")

    if (
        decision.get("decision") == "approve" or decision.get("decision") == "deny"
    ) and decision.get("ticket_id"):
        result.append('"approve"/"deny" but a ticket_id is given')

    if not isinstance(decision.get("response"), str) or not decision["response"].strip():
        result.append("response is empty/blank")

    if decision.get("decision") not in VALID_DECISIONS:
        result.append("unknown decision")

    return result


FACT_TOOLS = {
    "get_employee_info": "employee",
    "check_request_eligibility": "eligibility",
    "get_open_requests": "open_requests",
    "get_item_catalog": "catalog",
    "get_department_budget": "budget",
    "check_inventory": "inventory",
}


def record_fact(facts: dict[str, Any], tool_name: str, result: dict[str, Any]) -> None:
    """Keep successful tool facts for per-submission budget accounting."""
    key = FACT_TOOLS.get(tool_name)
    if key is None or "error" in result:
        return
    if key in ("employee", "open_requests"):
        facts[key] = result
    elif key == "budget":
        facts.setdefault(key, {})[result["department"]] = result
    else:
        facts.setdefault(key, {})[result["item"]] = result


def apply_reflection(
    draft: dict[str, Any], review: dict[str, Any], rejections: int
) -> tuple[str, dict[str, Any]]:
    """Decide what to do with a reviewed draft.

    Returns (action, final) where action is "accept", "retry", or "escalate".
    `rejections` counts earlier rejections in this run.
    """
    final = dict(draft)
    verdict = review["verdict"]

    if verdict == "rejected" and draft["decision"] != "escalate":
        if rejections + 1 > MAX_REFLECTION_REJECTIONS:
            return "escalate", final
        return "retry", final

    if verdict == "revised" and review.get("revised_response"):
        final["response"] = review["revised_response"]
    return "accept", final
