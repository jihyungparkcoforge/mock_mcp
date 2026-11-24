"""Compatibility imports and CLI entry point. Core modules do not depend on this file."""

import json
from dataclasses import asdict
from typing import Any

from equipment_mcp.app import DEFAULT_MODEL as DEFAULT_MODEL
from equipment_mcp.app import make_llm_client as make_llm_client
from equipment_mcp.app import run_agent as run_agent
from equipment_mcp.formatting import (
    compact as compact,
)
from equipment_mcp.formatting import (
    format_trace_step as format_trace_step,
)
from equipment_mcp.formatting import (
    reflection_summary as reflection_summary,
)
from equipment_mcp.formatting import (
    tool_result_text as tool_result_text,
)
from equipment_mcp.models import (
    AgentResult as AgentResult,
)
from equipment_mcp.models import (
    EquipmentRequest as EquipmentRequest,
)
from equipment_mcp.models import (
    IncompleteRequestsError as IncompleteRequestsError,
)
from equipment_mcp.models import (
    ItemDecision as ItemDecision,
)
from equipment_mcp.models import (
    TraceStep as TraceStep,
)
from equipment_mcp.policy import (
    DECISION_RULES as DECISION_RULES,
)
from equipment_mcp.policy import (
    FACT_TOOLS as FACT_TOOLS,
)
from equipment_mcp.policy import (
    MAX_REFLECTION_REJECTIONS as MAX_REFLECTION_REJECTIONS,
)
from equipment_mcp.policy import (
    VALID_DECISIONS as VALID_DECISIONS,
)
from equipment_mcp.policy import (
    apply_reflection as apply_reflection,
)
from equipment_mcp.policy import (
    approval_gaps as approval_gaps,
)
from equipment_mcp.policy import (
    record_fact as record_fact,
)
from equipment_mcp.policy import (
    validate_decision as validate_decision,
)
from equipment_mcp.prompts import (
    MAX_TOKENS as MAX_TOKENS,
)
from equipment_mcp.prompts import (
    REFLECTION_PROMPT as REFLECTION_PROMPT,
)
from equipment_mcp.prompts import (
    REFLECTION_SCHEMA as REFLECTION_SCHEMA,
)
from equipment_mcp.prompts import (
    REQUEST_SPLIT_PROMPT as REQUEST_SPLIT_PROMPT,
)
from equipment_mcp.prompts import (
    REQUEST_SPLIT_SCHEMA as REQUEST_SPLIT_SCHEMA,
)
from equipment_mcp.prompts import (
    SUBMIT_DECISION_TOOL as SUBMIT_DECISION_TOOL,
)
from equipment_mcp.prompts import (
    SYSTEM_PROMPT as SYSTEM_PROMPT,
)


def model_name() -> str:
    from equipment_mcp.app import Settings

    return Settings.from_environment().model


def __getattr__(name):
    # Retain the historical benchmark API without importing the SDK for pure helpers.
    if name == "AsyncAnthropicFoundry":
        from anthropic import AsyncAnthropicFoundry

        return AsyncAnthropicFoundry
    raise AttributeError(name)


async def split_requests(llm, model: str, request: str) -> list[EquipmentRequest]:
    from equipment_mcp.foundry import FoundryDecisionModel

    return await FoundryDecisionModel(llm, model).split_requests(request)


async def reflect(llm, model, request, draft, observations):
    from equipment_mcp.foundry import FoundryDecisionModel

    review = await FoundryDecisionModel(llm, model).review_text(request, draft, observations)
    return asdict(review)


async def force_escalation(
    mcp_tools: dict[str, Any],
    employee_id: str,
    request: str,
    review: dict[str, Any],
    trace: list[TraceStep],
) -> dict[str, Any]:
    """Escalate after the review kept rejecting the agent's decision."""
    reason = "Automated review repeatedly rejected the decision: " + "; ".join(review["issues"])
    args = {"employee_id": employee_id, "request": request, "reason": reason}
    trace.append(TraceStep("Action", f"flag_for_human_review({json.dumps(args)})"))
    text = tool_result_text(await mcp_tools["flag_for_human_review"].call(args))
    trace.append(TraceStep("Observation", compact(text)))
    ticket_id = json.loads(text)["ticket_id"]
    return {
        "decision": "escalate",
        "response": (
            f"Your request has been sent to a reviewer for a decision. Reference: {ticket_id}."
        ),
        "ticket_id": ticket_id,
    }


if __name__ == "__main__":
    import asyncio

    from dotenv import load_dotenv

    from equipment_mcp.app import main

    load_dotenv()
    asyncio.run(main())
