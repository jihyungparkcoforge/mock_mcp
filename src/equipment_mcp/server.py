"""MCP server exposing the equipment request tools over stdio.

Run:  uv run python -m equipment_mcp.server

Each tool is a thin wrapper around a function in tools.py. Only the
parameters the agent should control are exposed (no today / log_path).
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from equipment_mcp import tools

mcp = MCPServer("equipment-requests")


@mcp.tool()
def get_employee_info(
    employee_id: Annotated[str, Field(description='Employee ID, e.g. "E001".')],
) -> dict[str, Any]:
    """Look up an employee's role, tenure (years), and equipment currently on file.

    Returns {"error": "employee_not_found"} if the ID does not exist.
    """
    return tools.get_employee_info(employee_id)


@mcp.tool()
def get_policy_limits(
    role: Annotated[str, Field(description='Role name: "standard", "manager", or "engineer".')],
) -> dict[str, Any]:
    """Return which items a role is eligible for, with max quantity and refresh cycle (years).

    Returns {"error": "role_not_found", "available_roles": [...]} for an unknown role.
    """
    return tools.get_policy_limits(role)


@mcp.tool()
def check_request_eligibility(
    employee_id: Annotated[str, Field(description='Employee ID, e.g. "E001".')],
    item: Annotated[str, Field(description='Policy item name, e.g. "laptop" or "monitor".')],
) -> dict[str, Any]:
    """Check whether requesting `item` is within the employee's role policy.

    `status` is one of: eligible_new, eligible_refresh, not_eligible, not_in_policy,
    employee_not_found, role_not_found. `eligible` is None when policy cannot decide.
    """
    return tools.check_request_eligibility(employee_id, item)


@mcp.tool()
def flag_for_human_review(
    employee_id: Annotated[str, Field(description='Employee ID, e.g. "E001".')],
    request: Annotated[str, Field(description="The employee's original request text.")],
    reason: Annotated[str, Field(description="Why this needs a human decision.")],
) -> dict[str, Any]:
    """Escalate an ambiguous or out-of-policy request to a human reviewer.

    Side effect: records a review ticket. Returns the ticket (ticket_id, status, ...).
    """
    return tools.flag_for_human_review(employee_id, request, reason)


if __name__ == "__main__":
    mcp.run()  # stdio transport
