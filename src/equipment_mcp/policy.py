"""Pure approval and review policy; accepts facts rather than loading services."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from equipment_mcp.models import DecisionContext, EquipmentRequest


def validate_submission_input(
    request: str, employee_id: str, max_steps: int, submission_id: str | None
) -> None:
    """Reject invalid submission input before model calls or run state creation."""
    if not request.strip() or not employee_id.strip() or max_steps < 1:
        raise ValueError("Request, employee ID and a positive step limit are required")
    if submission_id is not None and (
        not isinstance(submission_id, str) or not submission_id.strip()
    ):
        raise ValueError("submission_id must be a nonempty string")


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


def validate_tool_scope(arguments: dict[str, Any], context: DecisionContext) -> None:
    """Reject supplied identifiers that target another employee or request item."""
    if "employee_id" in arguments and not isinstance(arguments["employee_id"], str):
        raise ValueError("Tool employee_id must be a string")
    if "item" in arguments and not isinstance(arguments["item"], str):
        raise ValueError("Tool item must be a string")
    if (
        "employee_id" in arguments
        and arguments["employee_id"].strip().upper() != context.employee_id
    ):
        raise ValueError("Tool employee_id does not match the current employee")
    if "item" in arguments and arguments["item"].strip().lower() != context.task.item:
        raise ValueError("Tool item does not match the current request")


def review_ticket_arguments(
    arguments: dict[str, Any], context: DecisionContext, submission_id: str | None
) -> dict[str, Any]:
    """Bind a ticket to the original request and a stable host-owned retry key."""
    task = context.task
    arguments = dict(arguments)
    arguments["employee_id"] = context.employee_id
    arguments["request"] = context.original
    arguments["idempotency_key"] = hashlib.sha256(
        json.dumps([submission_id, context.employee_id, task.item], ensure_ascii=False).encode()
    ).hexdigest()
    reason = arguments.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Escalation reason is empty")
    arguments["reason"] = f"[{task.request_id}: {task.item}] {reason}"
    return arguments


def validate_decision(decision: dict[str, Any], created_tickets: list[str]) -> list[str]:
    """Return problems with a submit_decision input (empty list means valid)."""
    result = []
    outcome = decision.get("decision")

    if outcome == "escalate" and decision.get("ticket_id") not in created_tickets:
        result.append("ticket_id is missing")

    if outcome in ("approve", "deny") and decision.get("ticket_id"):
        result.append('"approve"/"deny" but a ticket_id is given')

    if not isinstance(decision.get("response"), str) or not decision["response"].strip():
        result.append("response is empty/blank")

    if outcome not in VALID_DECISIONS:
        result.append("unknown decision")

    return result


def validate_item_decision(
    decision: dict[str, Any], task: EquipmentRequest, created_tickets: list[str]
) -> list[str]:
    """Validate a decision against the current item and its review tickets."""
    problems = validate_decision(decision, created_tickets)
    if decision.get("request_id") != task.request_id:
        problems.append("request_id does not match the current request")
    if decision.get("item") != task.item:
        problems.append("item does not match the current request")
    if created_tickets and decision.get("decision") != "escalate":
        problems.append("this request already has a review ticket; submit escalate")
    return problems


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
