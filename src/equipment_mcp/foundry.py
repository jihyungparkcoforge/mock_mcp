"""Foundry message encoding and decoding. The caller owns the injected client."""

import copy
import json
from typing import Any

from equipment_mcp.models import (
    DecisionContext,
    EquipmentRequest,
    ModelTurn,
    ReviewResult,
    ToolCall,
    ToolResult,
    TraceStep,
)
from equipment_mcp.policy import validate_requests
from equipment_mcp.prompts import (
    LOOP_REFLECTION_PROMPT,
    MAX_TOKENS,
    REFLECTION_PROMPT,
    REFLECTION_SCHEMA,
    REQUEST_SPLIT_PROMPT,
    REQUEST_SPLIT_SCHEMA,
    SUBMIT_DECISION_TOOL,
    SYSTEM_PROMPT,
)


def request_scope(context: DecisionContext) -> str:
    task = context.task
    return (
        f"Current request ID: {task.request_id}\nCurrent item: {task.item}\n"
        f"Request excerpt: {task.text}\nOriginal employee message: {context.original}\n"
        "Decide ONLY this request and item. Other items are context, not decisions to submit."
        + (
            "\nEarlier approvals missing price or department: "
            + ", ".join(context.unpriced_approvals)
            + ". Reserved totals are incomplete. Do not assume missing costs are zero;"
            + " escalate if this prevents confirming enough budget for the current item."
            if context.unpriced_approvals
            else ""
        )
    )


def check_stop(message) -> None:
    if message.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"Model stopped: {message.stop_reason}")


def response_json(message) -> dict[str, Any]:
    check_stop(message)
    return json.loads(next(b.text for b in message.content if b.type == "text"))


def cached_system(prompt: str) -> list[dict[str, Any]]:
    """Keep a reusable system prefix separate from the growing conversation."""
    return [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]


class FoundryAdapter:
    def __init__(self, client, deployment_name: str, max_tokens: int = MAX_TOKENS):
        self.client = client
        self.deployment_name = deployment_name
        self.max_tokens = max_tokens

    async def split_requests(self, request: str) -> list[EquipmentRequest]:
        message = await self.client.messages.create(
            model=self.deployment_name,
            max_tokens=self.max_tokens,
            cache_control={"type": "ephemeral"},
            system=cached_system(REQUEST_SPLIT_PROMPT),
            messages=[{"role": "user", "content": request}],
            output_config={"format": {"type": "json_schema", "schema": REQUEST_SPLIT_SCHEMA}},
        )
        payload = response_json(message)
        tasks = [
            EquipmentRequest(f"r{i}", e["item"].strip().lower(), e["text"])
            for i, e in enumerate(payload["requests"], 1)
        ]
        validate_requests(request, tasks)
        return tasks

    def start_session(self, context: DecisionContext, tools: list[dict[str, Any]]):
        return FoundrySession(self, context, tools)

    async def review(
        self, context: DecisionContext, draft: dict[str, Any], observations: list[TraceStep]
    ) -> ReviewResult:
        request = request_scope(context)
        observed = "\n".join(f"- {step.content}" for step in observations)
        prompt = (
            f"Employee request:\n{request}\n\nTool observations:\n{observed}\n\n"
            f"Draft decision: {draft['decision']}\nDraft ticket_id: {draft['ticket_id']}\n"
            f"Draft reply to the employee:\n{draft['response']}"
        )
        message = await self.client.messages.create(
            model=self.deployment_name,
            max_tokens=self.max_tokens,
            cache_control={"type": "ephemeral"},
            system=cached_system(REFLECTION_PROMPT),
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": REFLECTION_SCHEMA}},
        )
        return ReviewResult(**response_json(message))


class FoundrySession:
    """Provider conversation state stays here, including opaque thinking blocks."""

    def __init__(
        self, adapter: FoundryAdapter, context: DecisionContext, tools: list[dict[str, Any]]
    ):
        self.adapter = adapter
        self.context = context
        submission = copy.deepcopy(SUBMIT_DECISION_TOOL)
        props = submission["input_schema"]["properties"]
        props["request_id"]["enum"] = [context.task.request_id]
        props["item"]["enum"] = [context.task.item]
        self.definitions = copy.deepcopy(tools)
        if self.definitions:
            # The common MCP prefix survives changes to the per-item submission schema.
            self.definitions[-1]["cache_control"] = {"type": "ephemeral"}
        self.definitions.append(submission)
        self.messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": f"Today is {context.today.isoformat()}.\n"
                f"Employee ID: {context.employee_id}\n"
                + request_scope(context)
                + "\nBudget reserved by earlier approvals in this submission: "
                + json.dumps(context.reserved_budget, ensure_ascii=False),
            }
        ]

    async def next_actions(self, results: list[ToolResult] | None = None) -> ModelTurn:
        if results is not None:
            if results:
                content = [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.call_id,
                        "content": r.content,
                        "is_error": r.is_error,
                    }
                    for r in results
                ]
            else:
                content = []
            content.append({"type": "text", "text": LOOP_REFLECTION_PROMPT})
            self.messages.append({"role": "user", "content": content})
        message = await self.adapter.client.messages.create(
            model=self.adapter.deployment_name,
            max_tokens=self.adapter.max_tokens,
            cache_control={"type": "ephemeral"},
            system=cached_system(
                SYSTEM_PROMPT
                + (
                    "\nHandle only the current request ID and item. "
                    "Submit exactly one decision for it. "
                    "When calling flag_for_human_review, the application attaches "
                    "the original message "
                    "verbatim and adds this request ID and item to the reason. "
                    "Repeated flag calls reuse this request's ticket. "
                    "Earlier approvals in this submission reserve budget: subtract "
                    "those reservations from the department's remaining budget before approving."
                )
            ),
            tools=self.definitions,
            thinking={"type": "adaptive", "display": "summarized"},
            messages=self.messages,
        )
        check_stop(message)
        self.messages.append({"role": "assistant", "content": message.content})
        calls, thoughts = [], []
        for block in message.content:
            if block.type == "tool_use":
                calls.append(ToolCall(block.id, block.name, dict(block.input)))
            elif block.type == "thinking" and block.thinking.strip():
                thoughts.append(block.thinking.strip())
            elif block.type == "text" and block.text.strip():
                thoughts.append(block.text.strip())
        return ModelTurn(calls, thoughts)
