"""Submission and item state, including trace recording and budget reservations."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any
from uuid import uuid4

from equipment_mcp.formatting import compact, reflection_summary
from equipment_mcp.models import (
    AgentResult,
    DecisionContext,
    EquipmentRequest,
    ItemDecision,
    ToolCall,
    ToolResult,
    TraceStep,
)
from equipment_mcp.policy import validate_submission_input


@dataclass
class RunState:
    """State owned by one submission; never shared between workflow.run calls."""

    employee_id: str
    max_steps: int
    result: AgentResult
    reserved_budget: dict[str, float] = field(default_factory=dict)
    unpriced_approvals: list[str] = field(default_factory=list)

    @classmethod
    def create(
        cls, request: str, employee_id: str, max_steps: int, submission_id: str | None
    ) -> RunState:
        validate_submission_input(request, employee_id, max_steps, submission_id)
        return cls(
            employee_id=employee_id.strip().upper(),
            max_steps=max_steps,
            result=AgentResult(
                request=request,
                decisions=[],
                submission_id=submission_id if submission_id is not None else uuid4().hex,
            ),
        )

    def context_for(self, task: EquipmentRequest, today: date) -> DecisionContext:
        """Snapshot earlier approvals so later items cannot mutate an existing context."""
        return DecisionContext(
            task=task,
            original=self.result.request,
            employee_id=self.employee_id,
            today=today,
            reserved_budget=dict(self.reserved_budget),
            unpriced_approvals=tuple(self.unpriced_approvals),
        )

    def record_requests(self, tasks: list[EquipmentRequest]) -> None:
        self.result.trace.append(
            TraceStep("Requests", json.dumps([asdict(task) for task in tasks], ensure_ascii=False))
        )

    def reserve_budget(self, state: ItemState) -> None:
        if state.final is None or state.final.decision != "approve":
            return
        department = state.facts.get("employee", {}).get("department")
        item = state.context.task.item
        price = state.facts.get("catalog", {}).get(item, {}).get("price")
        if department is None or price is None:
            self.unpriced_approvals.append(item)
            state.record("Observation", "Approval cost not reserved: missing price or department.")
            return
        self.reserved_budget[department] = self.reserved_budget.get(department, 0) + price


@dataclass
class ItemState:
    """Facts, tickets and review progress belonging to exactly one request item."""

    context: DecisionContext
    output: AgentResult
    facts: dict[str, Any] = field(default_factory=dict)
    observations: list[TraceStep] = field(default_factory=list)
    created_tickets: list[str] = field(default_factory=list)
    ticket_result: str | None = None
    final: ItemDecision | None = None
    rejections: int = 0

    def __post_init__(self) -> None:
        self.observations.append(
            TraceStep(
                "Observation",
                "Budget reserved by earlier approvals in this submission: "
                + json.dumps(self.context.reserved_budget, ensure_ascii=False),
            )
        )

    def record(self, kind: str, content: str) -> None:
        task = self.context.task
        self.output.trace.append(TraceStep(kind, f"[{task.request_id}: {task.item}] {content}"))

    def record_action(self, name: str, arguments: dict[str, Any]) -> None:
        self.record("Action", f"{name}({json.dumps(arguments, ensure_ascii=False)})")

    def record_thoughts(self, thoughts: list[str]) -> None:
        for thought in thoughts:
            self.record("Thought", thought)

    def record_result(self, call: ToolCall, result: ToolResult) -> None:
        text = f"{call.name}: {compact(result.content)}"
        self.record("Observation", text)
        if call.name != "submit_decision":
            self.observations.append(TraceStep("Observation", text))

    def record_review(self, review: dict[str, Any]) -> None:
        task = self.context.task
        self.output.reflections.append({**review, "request_id": task.request_id, "item": task.item})
        self.record("Reflection", reflection_summary(review))
