"""Composition root: environment configuration and external resource ownership."""

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

from equipment_mcp.formatting import format_trace_step
from equipment_mcp.foundry import FoundryDecisionModel
from equipment_mcp.mcp_executor import McpToolExecutor
from equipment_mcp.prompts import MAX_TOKENS
from equipment_mcp.workflow import EquipmentWorkflow

DEFAULT_MODEL = "claude-opus-5-5"


@dataclass(frozen=True)
class Settings:
    model: str = DEFAULT_MODEL
    max_tokens: int = MAX_TOKENS

    @classmethod
    def from_environment(cls):
        return cls(model=os.environ.get("FOUNDRY_MODEL", DEFAULT_MODEL))


def make_llm_client():
    from anthropic import AsyncAnthropicFoundry

    return AsyncAnthropicFoundry()


@asynccontextmanager
async def open_workflow(settings: Settings | None = None):
    from mcp import ClientSession

    from equipment_mcp.transport import open_transport

    settings = settings or Settings.from_environment()
    async with make_llm_client() as client:
        async with open_transport() as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await McpToolExecutor.discover(session)
                model = FoundryDecisionModel(client, settings.model, settings.max_tokens)
                yield EquipmentWorkflow(model, tools)


async def run_agent(request: str, employee_id: str, max_steps: int = 14):
    async with open_workflow() as workflow:
        return await workflow.run(request, employee_id, max_steps)


async def main() -> None:
    from equipment_mcp.demo_requests import DEMO_REQUESTS

    for i, demo in enumerate(DEMO_REQUESTS, start=1):
        print("=" * 78)
        print(f"Request {i} | employee {demo['employee_id']} | expected: {demo['expected']}")
        print(f"> {demo['request']}")
        print("-" * 78)
        result = await run_agent(demo["request"], demo["employee_id"])
        for step in result.trace:
            print(format_trace_step(step))
        print("-" * 78)
        for decision in result.decisions:
            print(f"REQUEST  : {decision.request_id} | {decision.item}")
            print(f"DECISION : {decision.decision}")
            if decision.ticket_id:
                print(f"TICKET   : {decision.ticket_id}")
            print(f"RESPONSE : {decision.response}")
        verdicts = ", ".join(r["verdict"] for r in result.reflections)
        print(f"REFLECT  : {verdicts}")
        match = (
            "OK"
            if len(result.decisions) == 1 and result.decision == demo["expected"]
            else "MISMATCH"
        )
        print(f"CHECK    : {match}")
        print()
