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
def get_policy_limits(role: str) -> dict[str, Any]:
    """Return which items a role is eligible for, with max quantity and refresh cycle (years)."""
    pass  # TODO


@mcp.tool()
def check_request_eligibility(employee_id: str, item: str) -> dict[str, Any]:
    """Check whether requesting `item` (e.g. "laptop") is within the employee's role policy."""
    pass  # TODO


@mcp.tool()
def flag_for_human_review(employee_id: str, request: str, reason: str) -> dict[str, Any]:
    """Escalate an ambiguous or out-of-policy request to a human reviewer (creates a ticket)."""
    pass  # TODO


if __name__ == "__main__":
    mcp.run()  # stdio transport
