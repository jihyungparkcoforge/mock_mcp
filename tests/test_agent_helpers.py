import pytest

from equipment_mcp.formatting import format_trace_step
from equipment_mcp.models import TraceStep
from equipment_mcp.policy import (
    MAX_REFLECTION_REJECTIONS,
    VALID_DECISIONS,
    apply_reflection,
    approval_gaps,
    record_fact,
    validate_decision,
)
from equipment_mcp.prompts import SUBMIT_DECISION_TOOL


class TestFormatTraceStep:
    @pytest.mark.parametrize("kind", ["Thought", "Action", "Observation", "Reflection"])
    def test_prefixes_kind(self, kind):
        assert format_trace_step(TraceStep(kind, "hello")) == f"{kind}: hello"


class TestSubmitDecisionTool:
    def test_schema_restricts_decision_values(self):
        props = SUBMIT_DECISION_TOOL["input_schema"]["properties"]
        assert props["decision"]["enum"] == list(VALID_DECISIONS)

    def test_schema_is_strict(self):
        schema = SUBMIT_DECISION_TOOL["input_schema"]
        assert SUBMIT_DECISION_TOOL["strict"] is True
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == {
            "request_id",
            "decision",
            "item",
            "response",
            "ticket_id",
        }


class TestValidateDecision:
    def test_approve_without_ticket_is_valid(self):
        d = {"decision": "approve", "response": "Approved.", "ticket_id": None}
        assert validate_decision(d, created_tickets=[]) == []

    def test_deny_without_ticket_is_valid(self):
        d = {"decision": "deny", "response": "Next eligible 2028-06-01.", "ticket_id": None}
        assert validate_decision(d, created_tickets=[]) == []

    def test_escalate_with_created_ticket_is_valid(self):
        d = {"decision": "escalate", "response": "Sent for review.", "ticket_id": "ESC-0001"}
        assert validate_decision(d, created_tickets=["ESC-0001"]) == []

    def test_escalate_without_ticket_is_a_problem(self):
        d = {"decision": "escalate", "response": "Sent for review.", "ticket_id": None}
        assert len(validate_decision(d, created_tickets=[])) == 1

    def test_escalate_with_unknown_ticket_is_a_problem(self):
        d = {"decision": "escalate", "response": "Sent for review.", "ticket_id": "ESC-9999"}
        assert len(validate_decision(d, created_tickets=["ESC-0001"])) == 1

    @pytest.mark.parametrize("decision", ["approve", "deny"])
    def test_ticket_on_non_escalation_is_a_problem(self, decision):
        d = {"decision": decision, "response": "Done.", "ticket_id": "ESC-0001"}
        assert len(validate_decision(d, created_tickets=["ESC-0001"])) == 1

    def test_blank_response_is_a_problem(self):
        d = {"decision": "approve", "response": "   ", "ticket_id": None}
        assert len(validate_decision(d, created_tickets=[])) == 1

    def test_problems_are_readable_strings(self):
        d = {"decision": "escalate", "response": "", "ticket_id": None}
        problems = validate_decision(d, created_tickets=[])
        assert len(problems) == 2
        assert all(isinstance(p, str) and p.strip() for p in problems)


class TestApplyReflection:
    draft = {"decision": "approve", "response": "Approved.", "ticket_id": None}

    def test_confirmed_keeps_draft(self):
        review = {"verdict": "confirmed", "issues": [], "revised_response": None}
        assert apply_reflection(self.draft, review, rejections=0) == ("accept", self.draft)

    def test_revised_replaces_response_only(self):
        review = {"verdict": "revised", "issues": ["x"], "revised_response": "Fixed."}
        action, final = apply_reflection(self.draft, review, rejections=0)
        assert action == "accept"
        assert final == {"decision": "approve", "response": "Fixed.", "ticket_id": None}

    def test_rejected_asks_agent_to_retry(self):
        review = {"verdict": "rejected", "issues": ["wrong"], "revised_response": None}
        assert apply_reflection(self.draft, review, rejections=0)[0] == "retry"

    def test_rejected_past_limit_escalates(self):
        review = {"verdict": "rejected", "issues": ["wrong"], "revised_response": None}
        action, _ = apply_reflection(self.draft, review, rejections=MAX_REFLECTION_REJECTIONS)
        assert action == "escalate"

    def test_escalation_is_never_rejected(self):
        draft = {"decision": "escalate", "response": "Sent.", "ticket_id": "ESC-0001"}
        review = {"verdict": "rejected", "issues": ["?"], "revised_response": None}
        assert apply_reflection(draft, review, rejections=0) == ("accept", draft)

    def test_draft_is_not_mutated(self):
        draft = dict(self.draft)
        review = {"verdict": "revised", "issues": ["x"], "revised_response": "Fixed."}
        apply_reflection(draft, review, rejections=0)
        assert draft["response"] == "Approved."


def facts_for_approval(**overrides):
    """Facts as recorded after every approval check passed for E001's laptop."""
    facts = {}
    observed = {
        "get_employee_info": {
            "employee_id": "E001",
            "department": "Finance",
            "employment_status": "active",
        },
        "check_request_eligibility": {
            "employee_id": "E001",
            "item": "laptop",
            "eligible": True,
            "status": "eligible_refresh",
        },
        "get_open_requests": {"employee_id": "E001", "open_requests": []},
        "get_item_catalog": {"item": "laptop", "price": 1400, "auto_approve_limit": 2000},
        "get_department_budget": {"department": "Finance", "remaining": 11800},
        "check_inventory": {"item": "laptop", "in_stock": 6, "restock_date": None},
    }
    observed.update(overrides)
    for tool, result in observed.items():
        if result is not None:
            record_fact(facts, tool, result)
    return facts


class TestApprovalGaps:
    def test_all_checks_passed(self):
        assert approval_gaps("laptop", facts_for_approval()) == []

    def test_item_name_is_normalized(self):
        assert approval_gaps(" Laptop ", facts_for_approval()) == []

    @pytest.mark.parametrize(
        "missing",
        [
            "get_employee_info",
            "check_request_eligibility",
            "get_open_requests",
            "get_item_catalog",
            "get_department_budget",
            "check_inventory",
        ],
    )
    def test_each_missing_check_blocks_approval(self, missing):
        gaps = approval_gaps("laptop", facts_for_approval(**{missing: None}))
        assert len(gaps) == 1

    def test_not_eligible(self):
        facts = facts_for_approval(
            check_request_eligibility={
                "employee_id": "E001",
                "item": "laptop",
                "eligible": False,
                "status": "not_eligible",
            }
        )
        assert len(approval_gaps("laptop", facts)) == 1

    def test_inactive_employee(self):
        facts = facts_for_approval(
            get_employee_info={
                "employee_id": "E001",
                "department": "Finance",
                "employment_status": "on_leave",
            }
        )
        assert len(approval_gaps("laptop", facts)) == 1

    def test_open_request_for_same_item(self):
        open_req = {"request_id": "ORD-1", "item": "laptop", "status": "awaiting_delivery"}
        facts = facts_for_approval(
            get_open_requests={"employee_id": "E001", "open_requests": [open_req]}
        )
        assert len(approval_gaps("laptop", facts)) == 1

    def test_open_request_for_other_item_is_fine(self):
        open_req = {"request_id": "ORD-1", "item": "monitor", "status": "awaiting_delivery"}
        facts = facts_for_approval(
            get_open_requests={"employee_id": "E001", "open_requests": [open_req]}
        )
        assert approval_gaps("laptop", facts) == []

    def test_price_over_auto_approve_limit(self):
        facts = facts_for_approval(
            get_item_catalog={"item": "laptop", "price": 2500, "auto_approve_limit": 2000}
        )
        assert len(approval_gaps("laptop", facts)) == 1

    def test_budget_below_price(self):
        facts = facts_for_approval(
            get_department_budget={"department": "Finance", "remaining": 100}
        )
        assert len(approval_gaps("laptop", facts)) == 1

    def test_budget_of_other_department_does_not_count(self):
        facts = facts_for_approval(
            get_department_budget={"department": "Sales", "remaining": 99999}
        )
        assert len(approval_gaps("laptop", facts)) == 1

    def test_out_of_stock(self):
        facts = facts_for_approval(
            check_inventory={"item": "laptop", "in_stock": 0, "restock_date": "2026-12-15"}
        )
        assert len(approval_gaps("laptop", facts)) == 1

    def test_error_results_are_not_recorded(self):
        facts = facts_for_approval(get_item_catalog={"error": "item_not_in_catalog"})
        assert len(approval_gaps("laptop", facts)) == 1

    def test_gaps_are_readable(self):
        gaps = approval_gaps("laptop", {})
        assert gaps and all(isinstance(g, str) and g.strip() for g in gaps)
