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
    """Keep successful results of the approval-relevant tools, keyed for approval_gaps."""
    key = FACT_TOOLS.get(tool_name)
    if key is None or "error" in result:
        return
    if key in ("employee", "open_requests"):
        facts[key] = result
    elif key == "budget":
        facts.setdefault(key, {})[result["department"]] = result
    else:
        facts.setdefault(key, {})[result["item"]] = result


def approval_gaps(
    item: str, facts: dict[str, Any], reserved_budget: dict[str, float] | None = None
) -> list[str]:
    """Reasons an approval of `item` is not backed by what the tools returned."""
    item = item.strip().lower()
    gaps = []

    employee = facts.get("employee")
    if employee is None:
        gaps.append("employee info not checked")
    elif employee["employment_status"] != "active":
        gaps.append(f"employment_status is {employee['employment_status']}")

    eligibility = facts.get("eligibility", {}).get(item)
    if eligibility is None:
        gaps.append(f"eligibility not checked for {item}")
    elif eligibility["eligible"] is not True:
        gaps.append(f"eligibility status is {eligibility['status']}")

    open_requests = facts.get("open_requests")
    if open_requests is None:
        gaps.append("open requests not checked")
    else:
        for req in open_requests["open_requests"]:
            if req["item"] == item:
                gaps.append(f"open request {req['request_id']} already exists for {item}")

    catalog = facts.get("catalog", {}).get(item)
    if catalog is None:
        gaps.append(f"price not checked for {item}")
    elif catalog["price"] > catalog["auto_approve_limit"]:
        gaps.append(
            f"price {catalog['price']} is above the auto-approve limit "
            f"{catalog['auto_approve_limit']}"
        )

    if employee is not None:
        budget = facts.get("budget", {}).get(employee["department"])
        if budget is None:
            gaps.append(f"budget not checked for department {employee['department']}")
        elif catalog is not None:
            reserved = (reserved_budget or {}).get(employee["department"], 0)
            available = budget["remaining"] - reserved
            if available < catalog["price"]:
                gaps.append(
                    f"remaining budget {available} after earlier approvals "
                    f"is below price {catalog['price']}"
                )

    inventory = facts.get("inventory", {}).get(item)
    if inventory is None:
        gaps.append(f"inventory not checked for {item}")
    elif inventory["in_stock"] <= 0:
        gaps.append(f"{item} is out of stock")

    return gaps


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
