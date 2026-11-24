"""Request orchestration; depends only on application types and injected capabilities."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any
from uuid import uuid4

from equipment_mcp.formatting import compact, reflection_summary
from equipment_mcp.models import (
    AgentResult,
    DecisionContext,
    IncompleteRequestsError,
    ItemDecision,
    ToolCall,
    ToolExecutionError,
    ToolResult,
    TraceStep,
)
from equipment_mcp.policy import (
    apply_reflection,
    record_fact,
    validate_decision,
    validate_requests,
)
from equipment_mcp.ports import LLMAdapter, ToolExecutor

READ_ONLY_TOOLS = frozenset(
    {
        "get_employee_info",
        "get_policy_limits",
        "check_request_eligibility",
        "get_item_catalog",
        "get_department_budget",
        "check_inventory",
        "get_open_requests",
    }
)


@dataclass
class _RunState:
    """Shared by items in one submission, never stored on the workflow instance."""

    result: AgentResult
    reserved_budget: dict[str, float] = field(default_factory=dict)
    unpriced_approvals: list[str] = field(default_factory=list)


@dataclass
class _ItemState:
    """Facts, tickets and review progress belonging to exactly one item."""

    context: DecisionContext
    output: AgentResult
    facts: dict[str, Any] = field(default_factory=dict)
    observations: list[TraceStep] = field(default_factory=list)
    created_tickets: list[str] = field(default_factory=list)
    ticket_result: str | None = None
    final: ItemDecision | None = None
    rejections: int = 0

    def record(self, kind: str, content: str) -> None:
        task = self.context.task
        self.output.trace.append(TraceStep(kind, f"[{task.request_id}: {task.item}] {content}"))

    def record_result(self, use: ToolCall, result: ToolResult) -> None:
        text = f"{use.name}: {compact(result.content)}"
        self.record("Observation", text)
        if use.name != "submit_decision":
            self.observations.append(TraceStep("Observation", text))


def _tool_batches(calls: list[ToolCall], parallel_names: set[str]) -> Iterator[list[ToolCall]]:
    """Group consecutive independent reads; never cross a write or final decision."""
    batch: list[ToolCall] = []
    names: set[str] = set()
    for call in calls:
        if call.name not in parallel_names or call.name in names:
            if batch:
                yield batch
                batch, names = [], set()
        if call.name in parallel_names:
            batch.append(call)
            names.add(call.name)
        else:
            yield [call]
    if batch:
        yield batch


class EquipmentWorkflow:
    def __init__(
        self, llm: LLMAdapter, tools: ToolExecutor, *, clock: Callable[[], date] = date.today
    ):
        self.llm = llm
        self.tools = tools
        self.clock = clock

    async def run(
        self,
        request: str,
        employee_id: str,
        max_steps: int = 14,
        *,
        submission_id: str | None = None,
    ) -> AgentResult:
        if not request.strip() or not employee_id.strip() or max_steps < 1:
            raise ValueError("Request, employee ID and a positive step limit are required")
        employee_id = employee_id.strip().upper()
        if submission_id is not None and (
            not isinstance(submission_id, str) or not submission_id.strip()
        ):
            raise ValueError("submission_id must be a nonempty string")
        submission_id = submission_id if submission_id is not None else uuid4().hex

        run = _RunState(AgentResult(request=request, decisions=[], submission_id=submission_id))

        tasks = await self.llm.split_requests(request)
        validate_requests(request, tasks)

        today = self.clock()
        run.result.trace.append(
            TraceStep("Requests", json.dumps([asdict(t) for t in tasks], ensure_ascii=False))
        )

        for index, task in enumerate(tasks):
            context = DecisionContext(
                task,
                request,
                employee_id,
                today,
                dict(run.reserved_budget),
                tuple(run.unpriced_approvals),
            )
            try:
                decision = await self.run_item(context, run, max_steps)
            except Exception as exc:
                raise IncompleteRequestsError(
                    run.result, [t.request_id for t in tasks[index:]]
                ) from exc
            run.result.decisions.append(decision)
        return run.result

    async def run_item(
        self, context: DecisionContext, run: _RunState, max_steps: int
    ) -> ItemDecision:
        """Ask for actions, execute them, and repeat until this item is finalized."""
        state = _ItemState(context, run.result)
        state.observations.append(
            TraceStep(
                "Observation",
                "Budget reserved by earlier approvals in this submission: "
                + json.dumps(context.reserved_budget, ensure_ascii=False),
            )
        )

        definitions = self.tools.definitions
        session = self.llm.start_session(context, definitions)
        tool_names = {tool["name"] for tool in definitions}
        previous_results = None

        for _ in range(max_steps):
            turn = await session.next_actions(previous_results)
            for thought in turn.thoughts:
                state.record("Thought", thought)
            previous_results = await self._execute_calls(turn.calls, state, tool_names)
            if state.final is not None:
                self._reserve_budget(state, run)
                return state.final
        raise RuntimeError(
            f"No valid decision for {context.task.request_id} after {max_steps} steps"
        )

    async def _execute_calls(
        self, calls: list[ToolCall], state: _ItemState, tool_names: set[str]
    ) -> list[ToolResult]:
        """Execute read batches concurrently and all other calls in their original order."""
        results = []
        for batch in _tool_batches(calls, tool_names & READ_ONLY_TOOLS):
            if state.final is None and len(batch) > 1:
                batch_results = await self._execute_parallel(batch, state)
            else:
                batch_results = []
                for use in batch:
                    if state.final is not None:
                        result = ToolResult(
                            use.call_id, "Request already finalized; additional call ignored.", True
                        )
                    elif use.name == "submit_decision":
                        result = await self._submit_decision(use, state)
                    elif use.name in tool_names:
                        result = await self._execute_tool(use, state)
                    else:
                        result = ToolResult(use.call_id, f"Unknown tool {use.name!r}.", True)
                    batch_results.append(result)
            for use, result in zip(batch, batch_results, strict=True):
                state.record_result(use, result)
                results.append(result)
        return results

    async def _execute_parallel(self, batch: list[ToolCall], state: _ItemState) -> list[ToolResult]:
        state.record(
            "Execution", f"PARALLEL {len(batch)} reads: " + ", ".join(use.name for use in batch)
        )
        # Unexpected failures cancel and await sibling reads before clients can close.
        async with asyncio.TaskGroup() as group:
            pending = [group.create_task(self._execute_tool(use, state)) for use in batch]
        results = [task.result() for task in pending]
        return results

    async def _execute_tool(self, use: ToolCall, state: _ItemState) -> ToolResult:
        try:
            text = await self._call_business(use.name, use.arguments, state)
            return ToolResult(use.call_id, text)
        except (ToolExecutionError, ValueError, KeyError, TypeError) as exc:
            return ToolResult(use.call_id, str(exc), is_error=True)

    async def _call_business(self, name: str, args: dict[str, Any], state: _ItemState) -> str:
        context = state.context
        task = context.task
        args = dict(args)
        if "employee_id" in args and args["employee_id"].strip().upper() != context.employee_id:
            raise ValueError("Tool employee_id does not match the current employee")
        if "item" in args and args["item"].strip().lower() != task.item:
            raise ValueError("Tool item does not match the current request")
        if name == "flag_for_human_review":
            if state.ticket_result is not None:
                return state.ticket_result
            args["employee_id"] = context.employee_id
            args["request"] = context.original
            args["idempotency_key"] = hashlib.sha256(
                json.dumps(
                    [state.output.submission_id, context.employee_id, task.item], ensure_ascii=False
                ).encode()
            ).hexdigest()
            if not isinstance(args.get("reason"), str) or not args["reason"].strip():
                raise ValueError("Escalation reason is empty")
            args["reason"] = f"[{task.request_id}: {task.item}] {args['reason']}"
        state.record("Action", f"{name}({json.dumps(args, ensure_ascii=False)})")
        result = await self.tools.call(name, args)
        text = json.dumps(result, ensure_ascii=False)
        record_fact(state.facts, name, result)
        if name == "flag_for_human_review" and result.get("ticket_id"):
            state.ticket_result = text
            state.created_tickets.append(result["ticket_id"])
        return text

    async def _submit_decision(self, use: ToolCall, state: _ItemState) -> ToolResult:
        """Validate the submission, review the draft, then accept, retry or escalate."""
        task = state.context.task
        draft = dict(use.arguments)
        state.record("Action", f"submit_decision({json.dumps(draft, ensure_ascii=False)})")
        problems = validate_decision(draft, state.created_tickets)
        if draft.get("request_id") != task.request_id:
            problems.append("request_id does not match the current request")
        if draft.get("item") != task.item:
            problems.append("item does not match the current request")
        if state.created_tickets and draft.get("decision") != "escalate":
            problems.append("this request already has a review ticket; submit escalate")
        if problems:
            return ToolResult(use.call_id, "Decision rejected: " + "; ".join(problems), True)
        review = asdict(await self.llm.review(state.context, draft, state.observations))
        state.output.reflections.append(
            {**review, "request_id": task.request_id, "item": task.item}
        )
        state.record("Reflection", reflection_summary(review))
        action, reviewed = apply_reflection(draft, review, state.rejections)
        if action == "retry":
            state.rejections += 1
            return ToolResult(
                use.call_id, "Review rejected the decision: " + "; ".join(review["issues"]), True
            )
        # Force escalate if reflector still reject after second reflection
        if action == "escalate":
            text = await self._force_escalation(review["issues"], state)
            return ToolResult(use.call_id, text)
        state.final = ItemDecision(**reviewed)
        return ToolResult(use.call_id, "Decision recorded.")

    async def _force_escalation(self, issues: list[str], state: _ItemState) -> str:
        task = state.context.task
        text = await self._call_business(
            "flag_for_human_review",
            {
                "employee_id": state.context.employee_id,
                "request": state.context.original,
                "reason": "Automated review repeatedly rejected the decision: " + "; ".join(issues),
            },
            state,
        )
        ticket = json.loads(text)
        if not ticket.get("ticket_id"):
            raise RuntimeError("Forced escalation did not create a review ticket")
        state.final = ItemDecision(
            request_id=task.request_id,
            item=task.item,
            decision="escalate",
            ticket_id=ticket["ticket_id"],
            response=f"Your {task.item} request has been sent to a reviewer. "
            f"Reference: {ticket['ticket_id']}.",
        )
        return text

    def _reserve_budget(self, state: _ItemState, run: _RunState) -> None:
        if state.final is None or state.final.decision != "approve":
            return
        department = state.facts.get("employee", {}).get("department")
        price = state.facts.get("catalog", {}).get(state.context.task.item, {}).get("price")
        if department is not None and price is not None:
            run.reserved_budget[department] = run.reserved_budget.get(department, 0) + price
        else:
            run.unpriced_approvals.append(state.context.task.item)
            state.record("Observation", "Approval cost not reserved: missing price or department.")
