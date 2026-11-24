"""Composition root: environment configuration and external resource ownership."""

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

from equipment_mcp.foundry import FoundryAdapter
from equipment_mcp.mcp_executor import McpToolExecutor
from equipment_mcp.prompts import MAX_TOKENS
from equipment_mcp.workflow import EquipmentWorkflow


@dataclass(frozen=True)
class Settings:
    model: str
    max_tokens: int = MAX_TOKENS

    @classmethod
    def from_environment(cls):
        model = os.environ.get("FOUNDRY_MODEL", "").strip()
        if not model:
            raise ValueError(
                "Set FOUNDRY_MODEL in your environment or .env before running the agent."
            )
        return cls(model=model)


def make_llm_client():
    from anthropic import AsyncAnthropicFoundry

    return AsyncAnthropicFoundry()


@asynccontextmanager
async def open_workflow(settings: Settings | None = None):
    from mcp import Client

    from equipment_mcp.transport import server_url, stdio_params

    settings = settings or Settings.from_environment()
    async with (
        make_llm_client() as client,
        Client(server_url() or stdio_params(), mode="legacy") as mcp,
    ):
        tools = await McpToolExecutor.discover(mcp.session)
        llm = FoundryAdapter(client, settings.model, settings.max_tokens)
        yield EquipmentWorkflow(llm, tools)


async def run_agent(
    request: str,
    employee_id: str,
    max_steps: int = 14,
    *,
    submission_id: str | None = None,
):
    async with open_workflow() as workflow:
        return await workflow.run(request, employee_id, max_steps, submission_id=submission_id)
