"""Capabilities used by the workflow, expressed only in application types."""

from typing import Any, Protocol

from equipment_mcp.models import (
    DecisionContext,
    EquipmentRequest,
    ModelTurn,
    ReviewResult,
    ToolResult,
    TraceStep,
)


class LLMSession(Protocol):
    async def next_actions(self, results: list[ToolResult] | None = None) -> ModelTurn: ...


class LLMAdapter(Protocol):
    async def split_requests(self, request: str) -> list[EquipmentRequest]: ...

    def start_session(
        self, context: DecisionContext, tools: list[dict[str, Any]]
    ) -> LLMSession: ...

    async def review(
        self, context: DecisionContext, draft: dict[str, Any], observations: list[TraceStep]
    ) -> ReviewResult: ...


class ToolExecutor(Protocol):
    @property
    def definitions(self) -> list[dict[str, Any]]: ...

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class EquipmentData(Protocol):
    def read(self, table: str) -> dict[str, Any]: ...


class TicketStore(Protocol):
    def append(
        self, fields: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]: ...
