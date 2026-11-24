"""Plain-text serialization helpers."""

import json
from typing import Any

from equipment_mcp.models import TraceStep


def format_trace_step(step: TraceStep) -> str:
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
