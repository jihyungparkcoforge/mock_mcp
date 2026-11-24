import pytest

from equipment_mcp.formatting import format_trace_step
from equipment_mcp.models import TraceStep
from equipment_mcp.policy import (
    MAX_REFLECTION_REJECTIONS,
    VALID_DECISIONS,
    apply_reflection,
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
