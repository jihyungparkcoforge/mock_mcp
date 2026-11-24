"""Text serialization and optional terminal trace colors."""

import json
from typing import Any

from equipment_mcp.models import TraceStep

TRACE_COLORS = {
    "Requests": "34",  # Blue
    "Execution": "34",  # Blue
    "Thought": "36",  # Cyan
    "Action": "33",  # Yellow
    "Observation": "32",  # Green
    "Reflection": "35",  # Magenta
}


def format_trace_step(step: TraceStep, *, color: bool = False) -> str:
    code = TRACE_COLORS.get(step.kind) if color else None
    if code:
        return f"\033[1;{code}m{step.kind}:\033[0;{code}m {step.content}\033[0m"
    return step.kind + ": " + step.content


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


def reflection_summary(review: dict[str, Any]) -> str:
    summary = review["verdict"]
    if review["issues"]:
        summary += " - " + "; ".join(review["issues"])
    return summary
