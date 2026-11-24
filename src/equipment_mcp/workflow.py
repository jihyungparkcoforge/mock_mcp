"""Request orchestration; depends only on application types and injected capabilities."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import date
from typing import Any

from equipment_mcp.formatting import compact, reflection_summary
from equipment_mcp.models import (
    AgentResult,
    DecisionContext,
    EquipmentRequest,
    IncompleteRequestsError,
    ItemDecision,
    ToolExecutionError,
    ToolResult,
    TraceStep,
)
from equipment_mcp.policy import (
    apply_reflection,
    approval_gaps,
    record_fact,
    validate_decision,
    validate_requests,
)
from equipment_mcp.ports import DecisionModel, ToolExecutor


async def _decide_item(
    model: DecisionModel,
    tools: ToolExecutor,
    task: EquipmentRequest,
    original: str,
    employee_id: str,
    reserved_budget: dict[str, float],
    trace: list[TraceStep],
    reflections: list[dict[str, Any]],
    max_steps: int,
    today: date,
) -> ItemDecision:
    """One task owns its facts, review retries, ticket and immutable final decision."""
    facts: dict[str, Any] = {}
    observations: list[TraceStep] = []
    created_tickets: list[str] = []
    ticket_result: str | None = None
    final: dict[str, Any] | None = None
    rejections = 0
    context = DecisionContext(task, original, employee_id, today, dict(reserved_budget))
    session = model.start_session(context, tools.definitions)
    tool_names = {t.name for t in tools.definitions}
    observations.append(
        TraceStep(
            "Observation",
            "Budget reserved by earlier approvals in this submission: "
            + json.dumps(reserved_budget, ensure_ascii=False),
        )
    )
    previous_results: list[ToolResult] | None = None

    def record(kind: str, content: str) -> None:
        trace.append(TraceStep(kind, f"[{task.request_id}: {task.item}] {content}"))

    async def call_business(name: str, args: dict[str, Any]) -> str:
        nonlocal ticket_result
        args = dict(args)
        if "employee_id" in args and args["employee_id"].strip().upper() != employee_id:
            raise ValueError("Tool employee_id does not match the current employee")
        if "item" in args and args["item"].strip().lower() != task.item:
            raise ValueError("Tool item does not match the current request")
        if name == "flag_for_human_review":
            if ticket_result is not None:
                return ticket_result
            args["employee_id"] = employee_id
            args["request"] = original
            if not isinstance(args.get("reason"), str) or not args["reason"].strip():
                raise ValueError("Escalation reason is empty")
            args["reason"] = f"[{task.request_id}: {task.item}] {args['reason']}"
        record("Action", f"{name}({json.dumps(args, ensure_ascii=False)})")
        result = await tools.call(name, args)
        text = json.dumps(result, ensure_ascii=False)
        record_fact(facts, name, result)
        if name == "flag_for_human_review" and result.get("ticket_id"):
            ticket_result = text
            created_tickets.append(result["ticket_id"])
        return text

    for _ in range(max_steps):
        turn = await session.next_actions(previous_results)
        for thought in turn.thoughts:
            record("Thought", thought)
        uses = turn.calls
        results = []
        for use in uses:
            is_error = False
            # Do not execute trailing calls (including side effects) after a final was accepted.
            if final is not None:
                is_error, text = True, "Request already finalized; additional call ignored."
            elif use.name == "submit_decision":
                draft = dict(use.arguments)
                record("Action", f"submit_decision({json.dumps(draft, ensure_ascii=False)})")
                problems = validate_decision(draft, created_tickets)
                if draft.get("request_id") != task.request_id:
                    problems.append("request_id does not match the current request")
                if draft.get("item") != task.item:
                    problems.append("item does not match the current request")
                if created_tickets and draft.get("decision") != "escalate":
                    problems.append("this request already has a review ticket; submit escalate")
                if draft.get("decision") == "approve":
                    problems += approval_gaps(task.item, facts, reserved_budget)
                if problems:
                    is_error, text = True, "Decision rejected: " + "; ".join(problems)
                else:
                    review = asdict(await model.review(context, draft, observations))
                    reflections.append({**review, "request_id": task.request_id, "item": task.item})
                    record("Reflection", reflection_summary(review))
                    action, reviewed = apply_reflection(draft, review, rejections)
                    if action == "accept":
                        final, text = reviewed, "Decision recorded."
                    elif action == "retry":
                        rejections += 1
                        is_error, text = (
                            True,
                            "Review rejected the decision: " + "; ".join(review["issues"]),
                        )
                    else:
                        text = await call_business(
                            "flag_for_human_review",
                            {
                                "employee_id": employee_id,
                                "request": original,
                                "reason": "Automated review repeatedly rejected the decision: "
                                + "; ".join(review["issues"]),
                            },
                        )
                        ticket = json.loads(text)
                        if not ticket.get("ticket_id"):
                            raise RuntimeError("Forced escalation did not create a review ticket")
                        final = {
                            "request_id": task.request_id,
                            "item": task.item,
                            "decision": "escalate",
                            "ticket_id": ticket["ticket_id"],
                            "response": f"Your {task.item} request has been sent to a reviewer. "
                            f"Reference: {ticket['ticket_id']}.",
                        }
            elif use.name in tool_names:
                try:
                    text = await call_business(use.name, use.arguments)
                except ToolExecutionError as exc:
                    is_error, text = True, str(exc)
                except (ValueError, KeyError, TypeError) as exc:
                    is_error, text = True, str(exc)
            else:
                is_error, text = True, f"Unknown tool {use.name!r}."
            record("Observation", compact(text))
            if use.name != "submit_decision":
                observations.append(TraceStep("Observation", compact(text)))
            results.append(ToolResult(use.call_id, text, is_error))
        previous_results = results
        if final is not None:
            if final["decision"] == "approve":
                department = facts["employee"]["department"]
                reserved_budget[department] = (
                    reserved_budget.get(department, 0) + facts["catalog"][task.item]["price"]
                )
            return ItemDecision(**final)
    raise RuntimeError(f"No valid decision for {task.request_id} after {max_steps} steps")


class EquipmentWorkflow:
    def __init__(
        self, model: DecisionModel, tools: ToolExecutor, *, clock: Callable[[], date] = date.today
    ):
        self.model = model
        self.tools = tools
        self.clock = clock

    async def run(self, request: str, employee_id: str, max_steps: int = 14) -> AgentResult:
        if not request.strip() or not employee_id.strip() or max_steps < 1:
            raise ValueError("Request, employee ID and a positive step limit are required")
        employee_id = employee_id.strip().upper()
        result = AgentResult(request=request, decisions=[])
        reserved_budget: dict[str, float] = {}
        tasks = await self.model.split_requests(request)
        validate_requests(request, tasks)
        today = self.clock()
        result.trace.append(
            TraceStep(
                "Requests",
                json.dumps(
                    [asdict(t) for t in tasks],
                    ensure_ascii=False,
                ),
            )
        )
        for index, task in enumerate(tasks):
            try:
                decision = await _decide_item(
                    self.model,
                    self.tools,
                    task,
                    request,
                    employee_id,
                    reserved_budget,
                    result.trace,
                    result.reflections,
                    max_steps,
                    today,
                )
            except Exception as exc:
                raise IncompleteRequestsError(
                    result, [t.request_id for t in tasks[index:]]
                ) from exc
            result.decisions.append(decision)
        return result
