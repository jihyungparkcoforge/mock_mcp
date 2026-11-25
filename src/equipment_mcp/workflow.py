"""Request orchestration; depends only on application types and injected capabilities."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from dataclasses import asdict
from datetime import date
from typing import Any

from equipment_mcp.models import (
    AgentResult,
    DecisionContext,
    EquipmentRequest,
    IncompleteRequestsError,
    ItemDecision,
    ToolCall,
    ToolExecutionError,
    ToolResult,
)
from equipment_mcp.policy import (
    apply_reflection,
    record_fact,
    review_ticket_arguments,
    validate_item_decision,
    validate_requests,
    validate_tool_scope,
)
from equipment_mcp.ports import LLMAdapter, ToolExecutor
from equipment_mcp.workflow_state import ItemState, RunState

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
        """Prepare a submission, extract its items, and resolve them in order."""
        run_state = RunState.create(request, employee_id, max_steps, submission_id)
        tasks = await self._extract_requests(request)
        await self._run_requests(tasks, run_state)
        return run_state.result

    async def _extract_requests(self, request: str) -> list[EquipmentRequest]:
        tasks = await self.llm.split_requests(request)
        validate_requests(request, tasks)
        return tasks

    async def _run_requests(self, tasks: list[EquipmentRequest], run_state: RunState) -> None:
        """Preserve completed decisions if a later item fails."""
        today = self.clock()
        run_state.record_requests(tasks)
        for index, task in enumerate(tasks):
            context = run_state.context_for(task, today)
            try:
                decision = await self.run_item(context, run_state, run_state.max_steps)
            except Exception as exc:
                pending_ids = [pending.request_id for pending in tasks[index:]]
                raise IncompleteRequestsError(run_state.result, pending_ids) from exc
            run_state.result.decisions.append(decision)

    async def run_item(
        self, context: DecisionContext, run_state: RunState, max_steps: int
    ) -> ItemDecision:
        """Ask for actions, execute them, and repeat until this item is finalized."""
        state = ItemState(context, run_state.result)
        definitions = self.tools.definitions
        session = self.llm.start_session(context, definitions)
        tool_names = {tool["name"] for tool in definitions}
        previous_results = None

        for _ in range(max_steps):
            turn = await session.next_actions(previous_results)
            state.record_thoughts(turn.thoughts)
            previous_results = await self._execute_calls(turn.calls, state, tool_names)
            if state.final is not None:
                run_state.reserve_budget(state)
                return state.final
        raise RuntimeError(
            f"No valid decision for {context.task.request_id} after {max_steps} steps"
        )

    async def _execute_calls(
        self, calls: list[ToolCall], state: ItemState, tool_names: set[str]
    ) -> list[ToolResult]:
        """Execute read batches concurrently and all other calls in their original order."""
        results = []
        for batch in _tool_batches(calls, tool_names & READ_ONLY_TOOLS):
            if state.final is None and len(batch) > 1:
                batch_results = await self._execute_parallel(batch, state)
            else:
                batch_results = [
                    await self._execute_call(call, state, tool_names) for call in batch
                ]
            for call, result in zip(batch, batch_results, strict=True):
                state.record_result(call, result)
                results.append(result)
        return results

    async def _execute_call(
        self, call: ToolCall, state: ItemState, tool_names: set[str]
    ) -> ToolResult:
        """Dispatch one call, ignoring further actions once the request is finalized."""
        if state.final is not None:
            return ToolResult(
                call.call_id, "Request already finalized; additional call ignored.", True
            )
        if call.name == "submit_decision":
            return await self._submit_decision(call, state)
        if call.name not in tool_names:
            return ToolResult(call.call_id, f"Unknown tool {call.name!r}.", True)
        return await self._execute_tool(call, state)

    async def _execute_parallel(self, batch: list[ToolCall], state: ItemState) -> list[ToolResult]:
        state.record(
            "Execution", f"PARALLEL {len(batch)} reads: " + ", ".join(call.name for call in batch)
        )
        # Unexpected failures cancel and await sibling reads before clients can close.
        async with asyncio.TaskGroup() as group:
            pending = [group.create_task(self._execute_tool(call, state)) for call in batch]
        results = [task.result() for task in pending]
        return results

    async def _execute_tool(self, call: ToolCall, state: ItemState) -> ToolResult:
        try:
            text = await self._call_business(call.name, call.arguments, state)
            return ToolResult(call.call_id, text)
        except (ToolExecutionError, ValueError, KeyError, TypeError) as exc:
            return ToolResult(call.call_id, str(exc), is_error=True)

    async def _call_business(self, name: str, args: dict[str, Any], state: ItemState) -> str:
        args = dict(args)
        validate_tool_scope(args, state.context)
        if name == "flag_for_human_review":
            return await self._create_review_ticket(args, state)
        _, text = await self._invoke_tool(name, args, state)
        return text

    async def _invoke_tool(
        self, name: str, args: dict[str, Any], state: ItemState
    ) -> tuple[dict[str, Any], str]:
        """Execute and serialize a tool result before retaining its budget facts."""
        state.record_action(name, args)
        result = await self.tools.call(name, args)
        text = json.dumps(result, ensure_ascii=False)
        record_fact(state.facts, name, result)
        return result, text

    async def _create_review_ticket(self, args: dict[str, Any], state: ItemState) -> str:
        """Reuse this item's ticket or create one with host-owned request details."""
        if state.ticket_result is not None:
            return state.ticket_result
        args = review_ticket_arguments(args, state.context, state.output.submission_id)
        result, text = await self._invoke_tool("flag_for_human_review", args, state)
        if result.get("ticket_id"):
            state.ticket_result = text
            state.created_tickets.append(result["ticket_id"])
        return text

    async def _submit_decision(self, call: ToolCall, state: ItemState) -> ToolResult:
        """Validate the submission, review the draft, then accept, retry or escalate."""
        draft = dict(call.arguments)
        state.record_action("submit_decision", draft)
        problems = validate_item_decision(draft, state.context.task, state.created_tickets)
        if problems:
            return ToolResult(call.call_id, "Decision rejected: " + "; ".join(problems), True)
        review = asdict(await self.llm.review(state.context, draft, state.observations))
        state.record_review(review)
        return await self._apply_review(call.call_id, draft, review, state)

    async def _apply_review(
        self, call_id: str, draft: dict[str, Any], review: dict[str, Any], state: ItemState
    ) -> ToolResult:
        """Apply the review outcome: accept the draft, retry, or force escalation."""
        action, reviewed = apply_reflection(draft, review, state.rejections)
        if action == "retry":
            state.rejections += 1
            return ToolResult(
                call_id, "Review rejected the decision: " + "; ".join(review["issues"]), True
            )
        if action == "escalate":
            text = await self._force_escalation(review["issues"], state)
            return ToolResult(call_id, text)
        state.final = ItemDecision(**reviewed)
        return ToolResult(call_id, "Decision recorded.")

    async def _force_escalation(self, issues: list[str], state: ItemState) -> str:
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
