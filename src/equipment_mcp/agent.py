"""ReAct agent for equipment requests.

Run: uv run python -m equipment_mcp.agent
Env: ANTHROPIC_FOUNDRY_API_KEY, ANTHROPIC_FOUNDRY_RESOURCE, FOUNDRY_MODEL (optional)
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from anthropic import AsyncAnthropicFoundry
from anthropic.lib.tools import ToolError
from anthropic.lib.tools.mcp import async_mcp_tool
from dotenv import load_dotenv
from mcp import ClientSession

from equipment_mcp.transport import open_transport

DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000
VALID_DECISIONS = ("approve", "deny", "escalate")
MAX_REFLECTION_REJECTIONS = 2

DECISION_RULES = """Decision rules, by eligibility status:
- eligible_new or eligible_refresh: approve.
- not_eligible with no damage or failure reason in the request: deny, and give the
  next eligible date.
- not_eligible but the request says the device is broken, damaged, or not working:
  escalate, since an exception needs human judgment.
- not_in_policy: escalate, since no policy covers the item.
- employee_not_found: deny and ask the employee to confirm their ID.
- role_not_found, or anything the data cannot settle: escalate.
If the data does not clearly support approve or deny, escalate."""

SYSTEM_PROMPT = f"""You handle internal IT equipment requests for employees.

Before each tool call, write one short sentence saying what you need to find out and why.
Base every conclusion on tool results, never on assumptions.

{DECISION_RULES}

Your decision is reviewed before it is sent. If the review rejects it, look at the
issues, investigate further if needed, and submit a corrected decision.

The reply to the employee must only state facts the tools returned and must not
promise anything beyond the decision itself.
"""

REFLECTION_PROMPT = f"""You review a drafted decision on an IT equipment request before it is sent.

Compare the draft with the tool observations and check:
1. Every fact in the reply (dates, quantities, refresh cycles, ticket IDs) matches
   the observations exactly.
2. The reply does not promise or imply anything the observations do not confirm
   (delivery dates, a specific model, approval of an escalated request, exceptions).
3. The decision follows these rules given the observed eligibility status:

{DECISION_RULES}

Verdicts:
- "confirmed": everything holds. issues is empty, revised_response is null.
- "revised": the decision is right but the reply has problems. List them and give a
  corrected revised_response with the same decision.
- "rejected": the decision itself is wrong for the observed data. Explain why in
  issues and set revised_response to null. Never reject an "escalate" decision;
  use "revised" or "confirmed" for those.
"""

REFLECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["confirmed", "revised", "rejected"]},
        "issues": {"type": "array", "items": {"type": "string"}},
        "revised_response": {"type": ["string", "null"]},
    },
    "required": ["verdict", "issues", "revised_response"],
    "additionalProperties": False,
}


@dataclass
class TraceStep:
    kind: str  # Thought / Action / Observation / Reflection
    content: str


@dataclass
class AgentResult:
    request: str
    decision: str
    response: str
    ticket_id: str | None = None
    trace: list[TraceStep] = field(default_factory=list)
    reflections: list[dict[str, Any]] = field(default_factory=list)


# non-mcp tool for the final answer
SUBMIT_DECISION_TOOL: dict[str, Any] = {
    "name": "submit_decision",
    "description": (
        "Submit the final decision for this request. Call exactly once, after investigating. "
        "For 'escalate', first call flag_for_human_review and pass its ticket_id here."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": list(VALID_DECISIONS)},
            "response": {
                "type": "string",
                "description": "Message to the employee, citing the policy facts used.",
            },
            "ticket_id": {
                "type": ["string", "null"],
                "description": "Escalation ticket ID for 'escalate'; null otherwise.",
            },
        },
        "required": ["decision", "response", "ticket_id"],
        "additionalProperties": False,
    },
}


def format_trace_step(step: TraceStep) -> str:
    return step.kind + ": " + step.content


def validate_decision(decision: dict[str, Any], created_tickets: list[str]) -> list[str]:
    """Return problems with a submit_decision input (empty list means valid)."""
    result = []

    if decision.get("decision") == "escalate" and decision.get("ticket_id") not in created_tickets:
        result.append("ticket_id is missing")

    if (
        decision.get("decision") == "approve" or decision.get("decision") == "deny"
    ) and decision.get("ticket_id"):
        result.append('"approve"/"deny" but a ticket_id is given')

    if not str(decision.get("response")).strip():
        result.append("response is empty/blank")

    return result


def apply_reflection(
    draft: dict[str, Any], review: dict[str, Any], rejections: int
) -> tuple[str, dict[str, Any]]:
    """Decide what to do with a reviewed draft.

    Returns (action, final) where action is "accept", "retry", or "escalate".
    `rejections` counts earlier rejections in this run.
    """
    final = dict(draft)
    verdict = review["verdict"]

    if verdict == "rejected" and draft["decision"] != "escalate":
        if rejections + 1 > MAX_REFLECTION_REJECTIONS:
            return "escalate", final
        return "retry", final

    if verdict == "revised" and review.get("revised_response"):
        final["response"] = review["revised_response"]
    return "accept", final


def make_llm_client() -> AsyncAnthropicFoundry:
    return AsyncAnthropicFoundry()


def model_name() -> str:
    return os.environ.get("FOUNDRY_MODEL", DEFAULT_MODEL)


def tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    return "\n".join(parts)


def compact(text: str) -> str:
    try:
        return json.dumps(json.loads(text), ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        return text


async def reflect(
    llm, model: str, request: str, draft: dict[str, Any], observations: list[TraceStep]
) -> dict[str, Any]:
    """Review a draft against the observations. Returns verdict, issues, revised_response."""
    observed = "\n".join(f"- {step.content}" for step in observations)
    prompt = (
        f"Employee request:\n{request}\n\n"
        f"Tool observations:\n{observed}\n\n"
        f"Draft decision: {draft['decision']}\n"
        f"Draft ticket_id: {draft['ticket_id']}\n"
        f"Draft reply to the employee:\n{draft['response']}"
    )
    message = await llm.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=REFLECTION_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": REFLECTION_SCHEMA}},
    )
    if message.stop_reason == "refusal":
        raise RuntimeError("Reflection request was refused by the model.")
    text = next(block.text for block in message.content if block.type == "text")
    return json.loads(text)


def reflection_summary(review: dict[str, Any]) -> str:
    summary = review["verdict"]
    if review["issues"]:
        summary += " - " + "; ".join(review["issues"])
    return summary


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


async def run_agent(request: str, employee_id: str, max_steps: int = 8) -> AgentResult:
    llm = make_llm_client()
    model = model_name()
    trace: list[TraceStep] = []
    observations: list[TraceStep] = []
    created_tickets: list[str] = []
    reflections: list[dict[str, Any]] = []
    rejections = 0
    final: dict[str, Any] | None = None

    async with open_transport() as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = {
                t.name: async_mcp_tool(t, session) for t in (await session.list_tools()).tools
            }
            tool_defs = [t.to_dict() for t in mcp_tools.values()] + [SUBMIT_DECISION_TOOL]

            messages: list[dict[str, Any]] = [
                {
                    "role": "user",
                    "content": (
                        f"Today is {date.today().isoformat()}.\n"
                        f"Employee ID: {employee_id}\n"
                        f"Request: {request}"
                    ),
                }
            ]

            for _ in range(max_steps):
                message = await llm.messages.create(
                    model=model,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM_PROMPT,
                    tools=tool_defs,
                    thinking={"type": "adaptive", "display": "summarized"},
                    messages=messages,
                )
                if message.stop_reason == "refusal":
                    raise RuntimeError("The model refused the request.")
                if message.stop_reason == "max_tokens":
                    raise RuntimeError("Response hit max_tokens before finishing.")

                messages.append({"role": "assistant", "content": message.content})

                tool_uses = []
                for block in message.content:
                    if block.type == "thinking" and block.thinking.strip():
                        trace.append(TraceStep("Thought", block.thinking.strip()))
                    elif block.type == "text" and block.text.strip():
                        trace.append(TraceStep("Thought", block.text.strip()))
                    elif block.type == "tool_use":
                        trace.append(
                            TraceStep(
                                "Action",
                                f"{block.name}({json.dumps(block.input, ensure_ascii=False)})",
                            )
                        )
                        tool_uses.append(block)

                if not tool_uses:
                    messages.append(
                        {
                            "role": "user",
                            "content": "Finish by calling submit_decision.",
                        }
                    )
                    continue

                results = []
                for use in tool_uses:
                    is_error = False
                    if use.name == SUBMIT_DECISION_TOOL["name"]:
                        draft = dict(use.input)
                        problems = validate_decision(draft, created_tickets)
                        if problems:
                            is_error = True
                            text = "Decision rejected: " + "; ".join(problems)
                        else:
                            review = await reflect(llm, model, request, draft, observations)
                            reflections.append(review)
                            trace.append(TraceStep("Reflection", reflection_summary(review)))
                            action, reviewed = apply_reflection(draft, review, rejections)
                            if action == "accept":
                                final = reviewed
                                text = "Decision recorded."
                            elif action == "retry":
                                rejections += 1
                                is_error = True
                                text = "Review rejected the decision: " + "; ".join(
                                    review["issues"]
                                )
                            else:
                                final = await force_escalation(
                                    mcp_tools, employee_id, request, review, trace
                                )
                                text = "Decision replaced by escalation after repeated rejection."
                    elif use.name in mcp_tools:
                        try:
                            text = tool_result_text(await mcp_tools[use.name].call(use.input))
                        except ToolError as exc:
                            is_error = True
                            text = tool_result_text(exc.content)
                        if use.name == "flag_for_human_review" and not is_error:
                            ticket = json.loads(text).get("ticket_id")
                            if ticket:
                                created_tickets.append(ticket)
                    else:
                        is_error = True
                        text = f"Unknown tool {use.name!r}."

                    step = TraceStep("Observation", compact(text))
                    trace.append(step)
                    if use.name != SUBMIT_DECISION_TOOL["name"]:
                        observations.append(step)
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": use.id,
                            "content": text,
                            "is_error": is_error,
                        }
                    )

                messages.append({"role": "user", "content": results})
                if final is not None:
                    break
            else:
                raise RuntimeError(f"No valid decision after {max_steps} steps.")

    return AgentResult(
        request=request,
        decision=final["decision"],
        response=final["response"],
        ticket_id=final["ticket_id"],
        trace=trace,
        reflections=reflections,
    )


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
        print(f"DECISION : {result.decision}")
        if result.ticket_id:
            print(f"TICKET   : {result.ticket_id}")
        print(f"RESPONSE : {result.response}")
        verdicts = ", ".join(r["verdict"] for r in result.reflections)
        print(f"REFLECT  : {verdicts}")
        match = "OK" if result.decision == demo["expected"] else "MISMATCH"
        print(f"CHECK    : {match}")
        print()


if __name__ == "__main__":
    load_dotenv()
    asyncio.run(main())
