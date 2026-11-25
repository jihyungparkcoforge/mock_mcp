from datetime import date

import pytest

from equipment_mcp.models import DecisionContext, EquipmentRequest
from equipment_mcp.policy import validate_item_decision, validate_tool_scope


@pytest.fixture
def context():
    return DecisionContext(
        task=EquipmentRequest("r1", "headset", "I need a headset."),
        original="I need a headset.",
        employee_id="E001",
        today=date(2026, 11, 23),
        reserved_budget={},
    )


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({}, id="no-identifiers"),
        pytest.param({"department": "IT"}, id="unrelated-argument"),
        pytest.param({"employee_id": " e001 "}, id="normalized-employee"),
        pytest.param({"item": " Headset "}, id="normalized-item"),
        pytest.param({"employee_id": "E001", "item": "headset"}, id="both-identifiers"),
    ],
)
def test_tool_scope_accepts_current_target(context, arguments):
    assert validate_tool_scope(arguments, context) is None


@pytest.mark.parametrize(
    "arguments,message",
    [
        ({"employee_id": "E002"}, "Tool employee_id does not match the current employee"),
        ({"item": "monitor"}, "Tool item does not match the current request"),
    ],
    ids=["another-employee", "another-item"],
)
def test_tool_scope_rejects_another_target(context, arguments, message):
    with pytest.raises(ValueError, match=message):
        validate_tool_scope(arguments, context)


def test_tool_scope_normalization_does_not_rewrite_arguments(context):
    arguments = {"employee_id": " e001 ", "item": " Headset "}
    validate_tool_scope(arguments, context)
    assert arguments == {"employee_id": " e001 ", "item": " Headset "}


@pytest.fixture
def decision():
    return {
        "request_id": "r1",
        "item": "headset",
        "decision": "approve",
        "response": "Approved.",
        "ticket_id": None,
    }


def test_item_decision_accepts_current_request(context, decision):
    assert validate_item_decision(decision, context.task, []) == []


@pytest.mark.parametrize(
    "field,value",
    [("request_id", "r2"), ("request_id", None), ("item", "monitor"), ("item", " Headset ")],
    ids=["another-request", "missing-request", "another-item", "item-must-match-exactly"],
)
def test_item_decision_requires_exact_scope(context, decision, field, value):
    decision[field] = value
    assert validate_item_decision(decision, context.task, []) == [
        f"{field} does not match the current request"
    ]


@pytest.mark.parametrize("outcome", ["approve", "deny"])
def test_existing_review_ticket_requires_escalation(context, decision, outcome):
    decision["decision"] = outcome
    assert validate_item_decision(decision, context.task, ["ESC-0001"]) == [
        "this request already has a review ticket; submit escalate"
    ]


def test_item_decision_accepts_its_created_ticket(context, decision):
    decision.update(decision="escalate", ticket_id="ESC-0001")
    assert validate_item_decision(decision, context.task, ["ESC-0001"]) == []


def test_item_decision_keeps_base_and_scope_problems_in_order(context, decision):
    decision.update(response="", request_id="r2", item="monitor")
    assert validate_item_decision(decision, context.task, ["ESC-0001"]) == [
        "response is empty/blank",
        "request_id does not match the current request",
        "item does not match the current request",
        "this request already has a review ticket; submit escalate",
    ]
