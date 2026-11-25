from __future__ import annotations

import asyncio
import sys
from typing import Annotated, Any

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from equipment_mcp import equipment
from equipment_mcp.ports import EquipmentData, TicketStore
from equipment_mcp.storage import default_data, default_tickets
from equipment_mcp.transport import http_settings, server_url


def create_server(data: EquipmentData, tickets: TicketStore) -> MCPServer:
    """Expose business operations with repositories bound outside the MCP inputs."""
    server = MCPServer("equipment-requests")
    # File reads and SQLite lock waits run in threads so HTTP requests can overlap.

    @server.tool()
    async def get_employee_info(
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
        return await asyncio.to_thread(equipment.get_employee_info, employee_id, data=data)

    @server.tool()
    async def get_policy_limits(
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
        return await asyncio.to_thread(equipment.get_policy_limits, role, data=data)

    @server.tool()
    async def check_request_eligibility(
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
        return await asyncio.to_thread(
            equipment.check_request_eligibility, employee_id, item, data=data
        )

    @server.tool()
    async def flag_for_human_review(
        employee_id: Annotated[str, Field(description='Employee ID, e.g. "E001".')],
        request: Annotated[str, Field(description="The employee's original request text.")],
        reason: Annotated[str, Field(description="Why this needs a human decision.")],
        idempotency_key: Annotated[
            str | None,
            Field(
                description="Caller-supplied retry key. The agent application fills this; omit it."
            ),
        ] = None,
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
        - {"error": "employee_not_found"} for an unknown or blank employee ID;
          no ticket is created.
        """
        return await asyncio.to_thread(
            equipment.flag_for_human_review,
            employee_id,
            request,
            reason,
            data=data,
            tickets=tickets,
            idempotency_key=idempotency_key,
        )

    @server.tool()
    async def get_item_catalog(
        item: Annotated[str, Field(description='Policy item name, e.g. "laptop" or "monitor".')],
    ) -> dict[str, Any]:
        """
        Look up an item's price and the auto-approval price limit.

        Use this tool when:
        - A request may be approved and its cost must be known.

        Returns:
        - price, currency, and auto_approve_limit (items above the limit need a manager).
        - {"error": "item_not_in_catalog"} if the item is not sold internally.
        """
        return await asyncio.to_thread(equipment.get_item_catalog, item, data=data)

    @server.tool()
    async def get_department_budget(
        department: Annotated[str, Field(description="Department name from get_employee_info.")],
    ) -> dict[str, Any]:
        """
        Look up a department's annual equipment budget and how much is left.

        Use this tool when:
        - A request may be approved and must be paid from the department budget.

        Returns:
        - annual_budget, spent, and remaining.
        - {"error": "department_not_found", "available_departments": [...]} if unknown.
        """
        return await asyncio.to_thread(equipment.get_department_budget, department, data=data)

    @server.tool()
    async def check_inventory(
        item: Annotated[str, Field(description='Policy item name, e.g. "laptop" or "monitor".')],
    ) -> dict[str, Any]:
        """
        Check how many units of an item are in stock.

        Use this tool when:
        - A request may be approved and the item must be available to hand out.

        Returns:
        - in_stock count and restock_date (null when no restock is scheduled).
        - {"error": "item_not_in_inventory"} if the item is not stocked.
        """
        return await asyncio.to_thread(equipment.check_inventory, item, data=data)

    @server.tool()
    async def get_open_requests(
        employee_id: Annotated[str, Field(description='Employee ID, e.g. "E001".')],
    ) -> dict[str, Any]:
        """
        List the employee's equipment requests that are already in progress.

        Use this tool when:
        - Checking whether the same item was already requested and not yet delivered.

        Returns:
        - open_requests with request_id, item, status, and created date.
        - {"error": "employee_not_found"} for an unknown employee ID.
        """
        return await asyncio.to_thread(equipment.get_open_requests, employee_id, data=data)

    return server


if __name__ == "__main__":
    load_dotenv()
    mcp = create_server(default_data(), default_tickets())
    url = None if "--stdio" in sys.argv else server_url()
    if url:
        mcp.run(transport="streamable-http", **http_settings(url))
    else:
        mcp.run()
