"""Capabilities used by the workflow, expressed only in application types."""

from typing import Any, Protocol

from equipment_mcp.models import (
    DecisionContext,
    EquipmentRequest,
    ModelTurn,
    ReviewResult,
    ToolDefinition,
    ToolResult,
    TraceStep,
)


class DecisionSession(Protocol):
    async def next_actions(self, results: list[ToolResult] | None = None) -> ModelTurn: ...


class DecisionModel(Protocol):
    async def split_requests(self, request: str) -> list[EquipmentRequest]: ...

    def start_session(
        self, context: DecisionContext, tools: list[ToolDefinition]
    ) -> DecisionSession: ...

    async def review(
        self, context: DecisionContext, draft: dict[str, Any], observations: list[TraceStep]
    ) -> ReviewResult: ...


class ToolExecutor(Protocol):
    @property
    def definitions(self) -> list[ToolDefinition]: ...

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class EquipmentData(Protocol):
    def read(self, table: str) -> dict[str, Any]: ...


class TicketStore(Protocol):
    def append(self, fields: dict[str, Any]) -> dict[str, Any]: ...
