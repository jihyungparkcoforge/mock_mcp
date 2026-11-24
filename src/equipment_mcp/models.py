"""Application values; no provider SDKs or I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ModelTurn:
    calls: list[ToolCall]
    thoughts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReviewResult:
    verdict: str
    issues: list[str]
    revised_response: str | None = None


@dataclass(frozen=True)
class DecisionContext:
    task: EquipmentRequest
    original: str
    employee_id: str
    today: date
    reserved_budget: dict[str, float]


class ToolExecutionError(RuntimeError):
    """A tool failed; the agent may recover using the error as an observation."""


@dataclass
class TraceStep:
    kind: str  # Thought / Action / Observation / Reflection
    content: str


@dataclass(frozen=True)
class EquipmentRequest:
    request_id: str
    item: str
    text: str


@dataclass(frozen=True)
class ItemDecision:
    request_id: str
    item: str
    decision: str
    response: str
    ticket_id: str | None = None


@dataclass
class AgentResult:
    request: str
    decisions: list[ItemDecision]
    trace: list[TraceStep] = field(default_factory=list)
    reflections: list[dict[str, Any]] = field(default_factory=list)

    @property
    def decision(self) -> str:
        """Preserve single-request callers without hiding a mixed result."""
        if len(self.decisions) != 1:
            raise ValueError("Multiple requests: read result.decisions instead")
        return self.decisions[0].decision

    @property
    def ticket_id(self) -> str | None:
        if len(self.decisions) != 1:
            raise ValueError("Multiple requests: read result.decisions instead")
        return self.decisions[0].ticket_id

    @property
    def response(self) -> str:
        if len(self.decisions) == 1:
            return self.decisions[0].response
        return "\n\n".join(f"{d.item}: {d.response}" for d in self.decisions)


class IncompleteRequestsError(RuntimeError):
    """Preserve completed decisions when a later request cannot finish."""

    def __init__(self, result: AgentResult, pending_request_ids: list[str]):
        self.result = result
        self.pending_request_ids = pending_request_ids
        super().__init__(f"Requests not completed: {', '.join(pending_request_ids)}")
