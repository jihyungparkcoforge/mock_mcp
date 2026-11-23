"""MCP server. Run: uv run python -m equipment_mcp.server

Serves Streamable HTTP at MCP_SERVER_URL when it is set, stdio otherwise.
"""

from __future__ import annotations

from typing import Annotated, Any

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from equipment_mcp import tools
from equipment_mcp.transport import http_settings, server_url

mcp = MCPServer("equipment-requests")


@mcp.tool()
def get_employee_info(
    employee_id: Annotated[str, Field(description='Employee ID, e.g. "E001".')],
) -> dict[str, Any]:
    """
    Retrieve an employee's role, tenure, and equipment records.

    Use this tool when:
    - The employee's role or employment information is needed.
    - Existing equipment records must be inspected.
    - Additional employee context is required to investigate a request.

    Returns:
    - Employee role and tenure.
    - Equipment currently on file.
    - {"error": "employee_not_found"} for an unknown employee ID.

    This tool is read-only and does not modify employee records.
    """
    return tools.get_employee_info(employee_id)


@mcp.tool()
def get_policy_limits(
    role: Annotated[str, Field(description='Role name: "standard", "manager", or "engineer".')],
) -> dict[str, Any]:
    """
    Retrieve equipment eligibility policies for a specified employee role.

    Use this tool when:
    - Equipment allowances for a role need to be inspected.
    - Maximum quantities or replacement intervals must be verified.
    - Policy details are needed to explain an eligibility decision.

    Returns:
    - Eligible equipment categories.
    - Maximum quantities and refresh cycles in years.
    - {"error": "role_not_found", "available_roles": [...]}
      for an unknown role.

    This tool is read-only and returns policy information,
    not an employee-specific eligibility decision.
    """
    return tools.get_policy_limits(role)


@mcp.tool()
def check_request_eligibility(
    employee_id: Annotated[str, Field(description='Employee ID, e.g. "E001".')],
    item: Annotated[
        str,
        Field(
            description=(
                'Policy item name such as "laptop" or "monitor". Map everyday wording to it '
                '("second screen" -> "monitor", "notebook" -> "laptop"); otherwise pass the '
                "item as the employee wrote it."
            )
        ),
    ],
) -> dict[str, Any]:
    """
    Evaluate an employee's equipment eligibility.

    Use this tool when:
    - An employee requests new equipment.
    - A replacement request requires policy validation.

    This tool checks:
    - Employee role
    - Current equipment
    - Replacement history
    - Policy restrictions

    Returns:
    - Eligibility status
    - Applicable policy
    - Supporting evidence
    - Missing information
    """
    return tools.check_request_eligibility(employee_id, item)


@mcp.tool()
def flag_for_human_review(
    employee_id: Annotated[str, Field(description='Employee ID, e.g. "E001".')],
    request: Annotated[str, Field(description="The employee's original request text.")],
    reason: Annotated[str, Field(description="Why this needs a human decision.")],
) -> dict[str, Any]:
    """
    Create a human review ticket for an equipment request requiring escalation.

    Use this tool when:
    - The request is ambiguous or lacks sufficient evidence.
    - Conflicting information prevents a reliable decision.
    - A policy exception requires human authorization.
    - An out-of-policy request requires escalation under company rules.

    Required information:
    - employee_id: The employee associated with the request.
    - request: The employee's original request text.
    - reason: The specific reason human review is required.

    Returns:
    - The created review ticket, including its ID and status.
    """
    return tools.flag_for_human_review(employee_id, request, reason)


if __name__ == "__main__":
    load_dotenv()
    url = server_url()
    if url:
        mcp.run(transport="streamable-http", **http_settings(url))
    else:
        mcp.run()
