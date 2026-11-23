"""ReAct agent that handles equipment requests through the MCP server.

Run:  uv run python -m equipment_mcp.agent

Env vars:
  ANTHROPIC_FOUNDRY_API_KEY   Foundry API key
  ANTHROPIC_FOUNDRY_RESOURCE  Foundry resource name
  FOUNDRY_MODEL               deployment name (default: claude-opus-5-5)

Trace mapping (Claude tool use -> ReAct):
  text / thinking-summary block -> Thought
  tool_use block                -> Action
  tool_result (from MCP)        -> Observation
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODEL = "claude-opus-5-5"
VALID_DECISIONS = ("approve", "deny", "escalate")

SYSTEM_PROMPT = """TODO: write the agent instructions.
- investigate with tools before deciding
- escalate (flag_for_human_review) instead of guessing; see plan.md escalation rules
- finish with a ```json block: {"decision": ..., "response": ..., "ticket_id": ...}
"""

REFLECTION_PROMPT = """TODO: write the reflection instructions.
- does the draft match what the tools returned?
- does it promise anything the data does not confirm?
"""


@dataclass
class TraceStep:
    kind: str      # "Thought" | "Action" | "Observation" | "Reflection"
    content: str


@dataclass
class AgentResult:
    request: str
    decision: str                      # one of VALID_DECISIONS
    response: str                      # final message to the employee
    ticket_id: str | None = None       # set when decision == "escalate"
    trace: list[TraceStep] = field(default_factory=list)
    reflection: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Pure helpers (unit tested in tests/test_agent_helpers.py)
# ---------------------------------------------------------------------------

def mcp_tool_to_anthropic(tool: Any) -> dict[str, Any]:
    """Convert an MCP Tool (name, description, input_schema -- snake_case in mcp 2.x) to a Claude tool definition:
    {"name": ..., "description": ..., "input_schema": ...}. Missing description -> "".
    """
    pass


def format_trace_step(step: TraceStep) -> str:
    """Render one step as "<kind>: <content>", e.g. "Action: get_employee_info({...})"."""
    pass


def parse_final_decision(text: str) -> dict[str, Any]:
    """Extract the LAST ```json fenced block from the model's final text.

    Returns {"decision", "response", "ticket_id"} (ticket_id may be None).
    Raises ValueError if there is no json block, it is invalid JSON,
    or decision is not one of VALID_DECISIONS.
    """
    pass


# ---------------------------------------------------------------------------
# LLM / MCP plumbing
# ---------------------------------------------------------------------------

def make_llm_client():
    """Build an AnthropicFoundry client from the env vars above."""
    pass


async def reflect(llm, model: str, request: str, draft: dict[str, Any],
                  observations: list[TraceStep]) -> dict[str, Any]:
    """Ask the model to check the draft against the observations.

    Returns {"verdict": "confirmed" | "revised", "issues": [...], "final": <draft dict>}.
    """
    pass


async def run_agent(request: str, employee_id: str, max_steps: int = 8) -> AgentResult:
    """Connect to the MCP server over stdio, run the ReAct loop, reflect, return the result.

    1. stdio_client(StdioServerParameters(command=..., args=["-m", "equipment_mcp.server"]))
    2. list_tools -> mcp_tool_to_anthropic
    3. loop: messages.create -> record Thought/Action -> session.call_tool -> Observation
    4. parse_final_decision -> reflect -> AgentResult
    Raise if max_steps is reached without a final decision (no silent fallback).
    """
    pass


async def main() -> None:
    """Run every request in demo_requests.DEMO_REQUESTS and print trace + decision."""
    pass


if __name__ == "__main__":
    asyncio.run(main())
