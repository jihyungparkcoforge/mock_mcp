"""Scripted model responses exercise the real orchestration without paid API calls."""

import asyncio
import copy
import itertools
import json
from collections.abc import Iterable
from datetime import date
from typing import Any, Literal, TypeAlias, overload

import pytest

from equipment_mcp.equipment import flag_for_human_review
from equipment_mcp.models import (
    DecisionContext,
    EquipmentRequest,
    IncompleteRequestsError,
    ModelTurn,
    ReviewResult,
    ToolCall,
    ToolResult,
    TraceStep,
)
from equipment_mcp.policy import MAX_REFLECTION_REJECTIONS
from equipment_mcp.ports import LLMSession
from equipment_mcp.repositories import MemoryEquipmentData, SqliteTicketStore
from equipment_mcp.workflow import EquipmentWorkflow

IDS = itertools.count()

# Each model phase has its own response shape. Exceptions simulate provider failures.
ScriptPhase: TypeAlias = Literal["split", "agent", "reflection"]
ScriptStep: TypeAlias = (
    tuple[Literal["split"], list[EquipmentRequest] | Exception]
    | tuple[Literal["agent"], list[ToolCall] | Exception]
    | tuple[Literal["reflection"], ReviewResult | Exception]
)


def use(name: str, **args: Any) -> ToolCall:
    return ToolCall(f"call-{next(IDS)}", name, args)


def submit(rid: str, item: str, decision: str = "approve", ticket: str | None = None) -> ToolCall:
    return use(
        "submit_decision",
        request_id=rid,
        item=item,
        decision=decision,
        response=f"{item}: {decision}.",
        ticket_id=ticket,
    )


def plan(*items: str, text: str) -> tuple[Literal["split"], list[EquipmentRequest]]:
    return ("split", [EquipmentRequest(f"r{i}", item, text) for i, item in enumerate(items, 1)])


def review(verdict: str = "confirmed") -> tuple[Literal["reflection"], ReviewResult]:
    return (
        "reflection",
        ReviewResult(verdict, [] if verdict == "confirmed" else ["wrong decision"]),
    )


def facts(item: str) -> tuple[Literal["agent"], list[ToolCall]]:
    return (
        "agent",
        [
            use("get_employee_info", employee_id="E001"),
            use("check_request_eligibility", employee_id="E001", item=item),
            use("get_open_requests", employee_id="E001"),
            use("get_item_catalog", item=item),
            use("get_department_budget", department="IT"),
            use("check_inventory", item=item),
        ],
    )


# Stand in for the model port with a deterministic script. The real workflow still
# chooses when to split, act, and review; no provider SDK or paid API is involved.
class ScriptedModel:
    def __init__(self, script: Iterable[ScriptStep]):
        self.script: list[ScriptStep] = list(script)
        self.calls: list[tuple[ScriptPhase, Any]] = []
        self.contexts: list[DecisionContext] = []

    @overload
    def pop(self, phase: Literal["split"], detail: Any) -> list[EquipmentRequest]: ...

    @overload
    def pop(self, phase: Literal["agent"], detail: Any) -> list[ToolCall]: ...

    @overload
    def pop(self, phase: Literal["reflection"], detail: Any) -> ReviewResult: ...

    def pop(
        self, phase: ScriptPhase, detail: Any
    ) -> list[EquipmentRequest] | list[ToolCall] | ReviewResult:
        expected, value = self.script.pop(0)
        assert phase == expected, f"Expected model phase {expected!r}, got {phase!r}"
        self.calls.append((phase, copy.deepcopy(detail)))
        if isinstance(value, Exception):
            raise value
        return value

    async def split_requests(self, request: str) -> list[EquipmentRequest]:
        return self.pop("split", request)

    def start_session(self, context: DecisionContext, tools: list[dict[str, Any]]) -> LLMSession:
        self.contexts.append(context)
        model = self

        class Session:
            async def next_actions(self, results: list[ToolResult] | None = None) -> ModelTurn:
                return ModelTurn(model.pop("agent", {"context": context, "results": results}))

        return Session()

    async def review(
        self, context: DecisionContext, draft: dict[str, Any], observations: list[TraceStep]
    ) -> ReviewResult:
        return self.pop(
            "reflection", {"context": context, "draft": draft, "observations": observations}
        )


# Stand in for the MCP executor with recorded calls and in-memory tickets. This
# isolates orchestration policy from network transport and persistent storage.
class Backend:
    names = [
        "get_employee_info",
        "get_open_requests",
        "check_request_eligibility",
        "get_item_catalog",
        "get_department_budget",
        "check_inventory",
        "flag_for_human_review",
    ]

    def __init__(self, budget: float = 1000):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tickets: list[dict[str, Any]] = []
        self.budget = budget

    @property
    def definitions(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "description": name, "input_schema": {"type": "object"}}
            for name in self.names
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "get_employee_info":
            result = {"employee_id": "E001", "department": "IT", "employment_status": "active"}
        elif name == "get_open_requests":
            result = {
                "employee_id": "E001",
                "open_requests": [
                    {"request_id": "ORD-1", "item": "monitor", "status": "awaiting_delivery"}
                ],
            }
        elif name == "check_request_eligibility":
            result = {**arguments, "eligible": True, "status": "eligible_new"}
        elif name == "get_item_catalog":
            result = {**arguments, "price": 120, "auto_approve_limit": 2000}
        elif name == "get_department_budget":
            result = {**arguments, "remaining": self.budget}
        elif name == "check_inventory":
            result = {**arguments, "in_stock": 3}
        elif name == "flag_for_human_review":
            result = {**arguments, "ticket_id": f"ESC-{len(self.tickets) + 1:04d}"}
            self.tickets.append(result)
        else:
            raise AssertionError(name)
        return result


def build(
    script: Iterable[ScriptStep], *, budget: float = 1000
) -> tuple[EquipmentWorkflow, ScriptedModel, Backend]:
    model, backend = ScriptedModel(script), Backend(budget)
    engine = EquipmentWorkflow(model, backend, clock=lambda: date(2026, 11, 23))
    return engine, model, backend


@pytest.fixture
def submission_retries(tmp_path):
    original = "My laptop is broken. I also need a webcam."

    # Keep SQLite real to verify retry persistence; replace only the MCP transport.
    class PersistentBackend(Backend):
        async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if name == "flag_for_human_review":
                self.calls.append((name, copy.deepcopy(arguments)))
                return flag_for_human_review(
                    **arguments,
                    data=MemoryEquipmentData({"employees": {"E001": {}}}),
                    tickets=SqliteTicketStore(tmp_path / "tickets.sqlite3"),
                )
            return await super().call(name, arguments)

    def run(items, submission_id, ticket_ids):
        script: list[ScriptStep] = [plan(*items, text=original)]
        for i, item in enumerate(items, 1):
            script.extend(
                [
                    (
                        "agent",
                        [
                            use(
                                "flag_for_human_review",
                                employee_id="E001",
                                request="rewritten",
                                reason="Needs review",
                                idempotency_key="model-must-not-control-this",
                            )
                        ],
                    ),
                    ("agent", [submit(f"r{i}", item, "escalate", ticket_ids[item])]),
                    review(),
                ]
            )
        backend = PersistentBackend()
        workflow = EquipmentWorkflow(ScriptedModel(script), backend)
        result = asyncio.run(workflow.run(original, "E001", submission_id=submission_id))
        keys = [args["idempotency_key"] for _, args in backend.calls]
        return result, keys

    ids = {"laptop": "ESC-0001", "webcam": "ESC-0002"}
    first, keys = run(["laptop", "webcam"], "submission-1", ids)
    retry, retry_keys = run(["webcam", "laptop"], "submission-1", ids)
    new, new_keys = run(["laptop", "webcam"], None, {"laptop": "ESC-0003", "webcam": "ESC-0004"})
    return [(first, keys), (retry, retry_keys), (new, new_keys)]


@pytest.mark.parametrize("attempt", [0, 1, 2], ids=["first", "retry", "new-submission"])
def test_retry_keys_are_unique_per_item(submission_retries, attempt):
    _, keys = submission_retries[attempt]
    assert len(set(keys)) == 2


@pytest.mark.parametrize("attempt", [0, 1, 2], ids=["first", "retry", "new-submission"])
def test_retry_keys_are_host_owned(submission_retries, attempt):
    _, keys = submission_retries[attempt]
    assert "model-must-not-control-this" not in keys


@pytest.mark.parametrize("attempt", [0, 1, 2], ids=["first", "retry", "new-submission"])
def test_submission_always_has_an_id(submission_retries, attempt):
    result, _ = submission_retries[attempt]
    assert result.submission_id


def test_retry_keys_survive_item_reordering(submission_retries):
    (_, keys), (_, retry_keys), _ = submission_retries
    assert keys == list(reversed(retry_keys))


def test_retry_returns_original_tickets(submission_retries):
    (first, _), (retry, _), _ = submission_retries
    assert {d.ticket_id for d in first.decisions} == {d.ticket_id for d in retry.decisions}


def test_retry_keeps_submission_id(submission_retries):
    (first, _), (retry, _), _ = submission_retries
    assert first.submission_id == retry.submission_id == "submission-1"


def test_new_submission_gets_new_id(submission_retries):
    (first, _), _, (new, _) = submission_retries
    assert new.submission_id != first.submission_id


def test_new_submission_does_not_reuse_retry_keys(submission_retries):
    (_, keys), _, (_, new_keys) = submission_retries
    assert set(keys).isdisjoint(new_keys)


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"request": ""}, "Request, employee ID and a positive step limit are required"),
        ({"request": "  "}, "Request, employee ID and a positive step limit are required"),
        ({"employee_id": ""}, "Request, employee ID and a positive step limit are required"),
        ({"employee_id": "  "}, "Request, employee ID and a positive step limit are required"),
        ({"max_steps": 0}, "Request, employee ID and a positive step limit are required"),
        ({"max_steps": -1}, "Request, employee ID and a positive step limit are required"),
        ({"submission_id": ""}, "submission_id must be a nonempty string"),
        ({"submission_id": " "}, "submission_id must be a nonempty string"),
        ({"submission_id": 123}, "submission_id must be a nonempty string"),
    ],
    ids=[
        "empty-request",
        "blank-request",
        "empty-employee",
        "blank-employee",
        "zero-steps",
        "negative-steps",
        "empty-submission",
        "blank-submission",
        "non-string-submission",
    ],
)
def test_invalid_submission_is_rejected_before_model_call(changes, message):
    client = ScriptedModel([])
    workflow = EquipmentWorkflow(client, Backend())
    arguments = {"request": "New webcam", "employee_id": "E001", **changes}
    with pytest.raises(ValueError, match=message):
        asyncio.run(workflow.run(**arguments))
    assert client.calls == []


@pytest.fixture
def duplicate_submission():
    original = "My monitor order is pending. Separately, I need a headset for calls."
    engine, client, backend = build(
        [
            plan("headset", text=original),
            facts("headset"),
            (
                "agent",
                [
                    submit("r1", "headset"),
                    submit("r1", "monitor", "deny"),
                    use(
                        "flag_for_human_review",
                        employee_id="E001",
                        request=original,
                        reason="extra",
                    ),
                ],
            ),
            review(),
        ],
    )
    result = asyncio.run(engine.run(original, " e001 "))
    return result, client, backend


def test_duplicate_submission_keeps_first_decision(duplicate_submission):
    result, _, _ = duplicate_submission
    assert result.decision == "approve"
    assert [(d.request_id, d.item) for d in result.decisions] == [("r1", "headset")]


def test_duplicate_submission_is_reviewed_once(duplicate_submission):
    result, _, _ = duplicate_submission
    assert len(result.reflections) == 1


def test_duplicate_submission_skips_trailing_ticket(duplicate_submission):
    _, _, backend = duplicate_submission
    assert backend.tickets == []


def test_duplicate_submission_traces_ignored_calls(duplicate_submission):
    result, _, _ = duplicate_submission
    assert sum("additional call ignored" in t.content for t in result.trace) == 2


def test_duplicate_submission_consumes_expected_model_turns(duplicate_submission):
    _, client, _ = duplicate_submission
    assert len(client.calls) == 4
    assert client.script == []


@pytest.fixture
def multi_item_result():
    original = "I need a headset and another monitor."
    engine, client, _ = build(
        [
            plan("headset", "monitor", text=original),
            facts("headset"),
            ("agent", [submit("r1", "headset")]),
            review(),
            facts("monitor"),
            ("agent", [submit("r2", "monitor", "deny")]),
            review(),
        ],
    )
    result = asyncio.run(engine.run(original, "E001"))
    return result, client


def test_multi_item_result_retains_each_decision(multi_item_result):
    result, _ = multi_item_result
    assert [(d.request_id, d.item, d.decision) for d in result.decisions] == [
        ("r1", "headset", "approve"),
        ("r2", "monitor", "deny"),
    ]


def test_multi_item_response_mentions_each_item(multi_item_result):
    result, _ = multi_item_result
    assert "headset" in result.response
    assert "monitor" in result.response


def test_multi_item_result_rejects_single_decision_access(multi_item_result):
    result, _ = multi_item_result
    with pytest.raises(ValueError, match="result.decisions"):
        _ = result.decision


def test_multi_item_result_rejects_single_ticket_access(multi_item_result):
    result, _ = multi_item_result
    with pytest.raises(ValueError, match="result.decisions"):
        _ = result.ticket_id


def test_multi_item_sessions_follow_request_order(multi_item_result):
    _, client = multi_item_result
    assert [c.task.request_id for c in client.contexts] == ["r1", "r2"]


def test_multi_item_session_receives_reserved_budget(multi_item_result):
    _, client = multi_item_result
    assert client.contexts[1].reserved_budget == {"IT": 120}


def test_later_reservation_does_not_mutate_earlier_context(multi_item_result):
    _, client = multi_item_result
    assert client.contexts[0].reserved_budget == {}


def test_multi_item_result_reviews_every_request(multi_item_result):
    result, _ = multi_item_result
    assert [r["request_id"] for r in result.reflections] == ["r1", "r2"]


def test_multi_item_workflow_waits_for_all_model_turns(multi_item_result):
    _, client = multi_item_result
    assert client.script == []


@pytest.fixture
def mismatched_calls():
    original = "I need a headset."
    engine, client, backend = build(
        [
            plan("headset", text=original),
            (
                "agent",
                [
                    use("get_employee_info", employee_id="E002"),
                    use("get_item_catalog", item="monitor"),
                    submit("r9", "headset", "deny"),
                    submit("r1", "monitor", "deny"),
                ],
            ),
            facts("headset"),
            ("agent", [submit("r1", "headset")]),
            review(),
        ],
    )
    result = asyncio.run(engine.run(original, "E001"))
    return result, client, backend


def test_mismatched_calls_allow_later_valid_decision(mismatched_calls):
    result, _, _ = mismatched_calls
    assert result.decision == "approve"


def test_mismatched_decisions_are_not_reviewed(mismatched_calls):
    result, _, _ = mismatched_calls
    assert len(result.reflections) == 1


def test_mismatched_tool_arguments_are_not_executed(mismatched_calls):
    _, _, backend = mismatched_calls
    assert not any(
        a.get("employee_id") == "E002" or a.get("item") == "monitor" for _, a in backend.calls
    )


def test_mismatched_calls_are_traced(mismatched_calls):
    result, _, _ = mismatched_calls
    assert len([t for t in result.trace if "does not match" in t.content]) == 4


def test_mismatched_calls_do_not_skip_later_turns(mismatched_calls):
    _, client, _ = mismatched_calls
    assert client.script == []


@pytest.mark.parametrize("field", ["employee_id", "item"])
@pytest.mark.parametrize("value", [None, 123], ids=["null", "number"])
def test_invalid_tool_argument_type_is_returned_to_model(field, value):
    original = "I need a headset."
    arguments = {"employee_id": "E001", "item": "headset", field: value}
    invalid = use("check_request_eligibility", **arguments)
    valid = use("check_request_eligibility", employee_id="E001", item="headset")
    # Script malformed model output followed by a correction; use the real workflow.
    engine, client, backend = build(
        [
            plan("headset", text=original),
            ("agent", [invalid]),
            ("agent", [valid]),
            ("agent", [submit("r1", "headset")]),
            review(),
        ]
    )
    asyncio.run(engine.run(original, "E001"))
    assert client.calls[2][1]["results"] == [
        ToolResult(invalid.call_id, f"Tool {field} must be a string", True)
    ]
    assert backend.calls == [(valid.name, valid.arguments)]


@pytest.fixture
def missing_evidence_review():
    original = "I need a headset and laptop."
    engine, client, _ = build(
        [
            plan("headset", "laptop", text=original),
            facts("headset"),
            ("agent", [submit("r1", "headset")]),
            review(),
            ("agent", [submit("r2", "laptop")]),
            review("rejected"),
            facts("laptop"),
            ("agent", [submit("r2", "laptop")]),
            review(),
        ],
    )
    result = asyncio.run(engine.run(original, "E001"))
    return result, client


def test_missing_evidence_retry_retains_both_decisions(missing_evidence_review):
    result, _ = missing_evidence_review
    assert len(result.decisions) == 2


def test_missing_evidence_rejection_is_traced(missing_evidence_review):
    result, _ = missing_evidence_review
    assert any("Review rejected the decision" in t.content for t in result.trace)


def test_missing_evidence_is_reviewed_again_after_retry(missing_evidence_review):
    result, client = missing_evidence_review
    assert len(result.reflections) == 3
    assert client.script == []


@pytest.fixture
def scoped_tickets():
    original = 'My laptop is broken.\nI also need a webcam for "Brian\'s calls".'

    def flag():
        return use(
            "flag_for_human_review",
            employee_id="E001",
            request="Rewritten text",
            reason="Needs review",
        )

    engine, client, backend = build(
        [
            plan("laptop", "webcam", text=original),
            ("agent", [flag(), flag()]),
            ("agent", [submit("r1", "laptop", "escalate", "ESC-0001")]),
            review(),
            ("agent", [submit("r2", "webcam", "escalate", "ESC-0001")]),
            ("agent", [flag()]),
            ("agent", [submit("r2", "webcam", "escalate", "ESC-0002")]),
            review(),
        ],
    )
    result = asyncio.run(engine.run(original, "E001"))
    return result, client, backend, original


def test_each_item_uses_its_own_ticket(scoped_tickets):
    result, _, _, _ = scoped_tickets
    assert [d.ticket_id for d in result.decisions] == ["ESC-0001", "ESC-0002"]


def test_repeated_ticket_call_is_idempotent(scoped_tickets):
    _, _, backend, _ = scoped_tickets
    assert len(backend.tickets) == 2


def test_tickets_preserve_original_request(scoped_tickets):
    _, _, backend, original = scoped_tickets
    assert [t["request"] for t in backend.tickets] == [original, original]


def test_first_ticket_reason_identifies_item(scoped_tickets):
    _, _, backend, _ = scoped_tickets
    assert backend.tickets[0]["reason"].startswith("[r1: laptop]")


def test_second_ticket_reason_identifies_item(scoped_tickets):
    _, _, backend, _ = scoped_tickets
    assert backend.tickets[1]["reason"].startswith("[r2: webcam]")


def test_ticket_from_another_item_is_rejected(scoped_tickets):
    result, _, _, _ = scoped_tickets
    assert any("ticket_id is missing" in t.content for t in result.trace)


def test_scoped_ticket_workflow_consumes_all_turns(scoped_tickets):
    _, client, _, _ = scoped_tickets
    assert client.script == []


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"employee_id": "E002"}, "Tool employee_id does not match the current employee"),
        ({"item": "monitor"}, "Tool item does not match the current request"),
    ],
    ids=["another-employee", "another-item"],
)
def test_cached_ticket_does_not_bypass_scope_validation(changes, message):
    original = "I need a headset."
    arguments = {"employee_id": "E001", "request": original, "reason": "Needs review"}
    mismatched = use("flag_for_human_review", **(arguments | changes))
    engine, client, _ = build(
        [
            plan("headset", text=original),
            ("agent", [use("flag_for_human_review", **arguments), mismatched]),
            ("agent", [submit("r1", "headset", "escalate", "ESC-0001")]),
            review(),
        ]
    )
    asyncio.run(engine.run(original, "E001"))
    assert client.calls[2][1]["results"][1] == ToolResult(mismatched.call_id, message, True)


@pytest.fixture
def combined_budget_review():
    original = "I need a headset and a laptop."
    engine, client, backend = build(
        [
            plan("headset", "laptop", text=original),
            facts("headset"),
            ("agent", [submit("r1", "headset")]),
            review(),
            facts("laptop"),
            ("agent", [submit("r2", "laptop")]),
            review("rejected"),
            (
                "agent",
                [
                    use(
                        "flag_for_human_review",
                        employee_id="E001",
                        request=original,
                        reason="Insufficient combined budget",
                    )
                ],
            ),
            ("agent", [submit("r2", "laptop", "escalate", "ESC-0001")]),
            review(),
        ],
        budget=200,
    )
    result = asyncio.run(engine.run(original, "E001"))
    return result, client, backend


def test_combined_budget_rejection_escalates_later_item(combined_budget_review):
    result, _, _ = combined_budget_review
    assert [d.decision for d in result.decisions] == ["approve", "escalate"]


def test_combined_budget_review_receives_earlier_reservation(combined_budget_review):
    _, client, _ = combined_budget_review
    assert client.contexts[1].reserved_budget == {"IT": 120}


def test_combined_budget_rejection_is_traced(combined_budget_review):
    result, _, _ = combined_budget_review
    assert any("Review rejected the decision" in t.content for t in result.trace)


def test_combined_budget_rejection_creates_one_ticket(combined_budget_review):
    _, _, backend = combined_budget_review
    assert len(backend.tickets) == 1


def test_combined_budget_retry_consumes_all_turns(combined_budget_review):
    _, client, _ = combined_budget_review
    assert client.script == []


@pytest.fixture
def exhausted_reviews():
    original = "I need a headset and a laptop."
    script: list[ScriptStep] = [plan("headset", "laptop", text=original)]
    for rid, item in [("r1", "headset"), ("r2", "laptop")]:
        for _ in range(MAX_REFLECTION_REJECTIONS + 1):
            script += [("agent", [submit(rid, item, "deny")]), review("rejected")]
    engine, client, backend = build(script)
    result = asyncio.run(engine.run(original, "E001"))
    return result, client, backend


def test_review_rejection_limit_is_per_request(exhausted_reviews):
    result, _, _ = exhausted_reviews
    assert len(result.reflections) == 6


def test_review_rejection_limit_forces_each_escalation(exhausted_reviews):
    result, _, _ = exhausted_reviews
    assert [d.decision for d in result.decisions] == ["escalate", "escalate"]


def test_forced_escalations_have_separate_tickets(exhausted_reviews):
    result, _, backend = exhausted_reviews
    assert [d.ticket_id for d in result.decisions] == ["ESC-0001", "ESC-0002"]
    assert len(backend.tickets) == 2


def test_review_rejection_limit_consumes_expected_turns(exhausted_reviews):
    _, client, _ = exhausted_reviews
    assert client.script == []


@pytest.fixture(params=[1, 3], ids=["one-step", "three-steps"])
def exhausted_steps(request):
    max_steps = request.param
    original = "I need a headset."
    engine, client, backend = build(
        [
            plan("headset", text=original),
            *[("agent", []) for _ in range(max_steps)],
            ("agent", [submit("r1", "headset")]),
            review(),
        ]
    )
    with pytest.raises(IncompleteRequestsError) as caught:
        asyncio.run(engine.run(original, "E001", max_steps=max_steps))
    return caught.value, client, backend, max_steps


def test_step_limit_preserves_runtime_error(exhausted_steps):
    error, _, _, max_steps = exhausted_steps
    assert isinstance(error.__cause__, RuntimeError)
    assert str(error.__cause__) == f"No valid decision for r1 after {max_steps} steps"


def test_step_limit_reports_pending_request(exhausted_steps):
    error, _, _, _ = exhausted_steps
    assert error.pending_request_ids == ["r1"]


def test_step_limit_does_not_invent_decisions(exhausted_steps):
    error, _, _, _ = exhausted_steps
    assert error.result.decisions == []


def test_step_limit_does_not_invent_reflections(exhausted_steps):
    error, _, _, _ = exhausted_steps
    assert error.result.reflections == []


def test_step_limit_makes_no_extra_model_call(exhausted_steps):
    _, client, _, max_steps = exhausted_steps
    assert [phase for phase, _ in client.calls] == ["split"] + ["agent"] * max_steps
    assert len(client.script) == 2


def test_step_limit_makes_no_business_calls(exhausted_steps):
    _, _, backend, _ = exhausted_steps
    assert backend.calls == []


def test_step_limit_does_not_create_automatic_ticket(exhausted_steps):
    _, _, backend, _ = exhausted_steps
    assert backend.tickets == []


@pytest.fixture
def last_turn_decisions():
    original = "I need a headset and a laptop."
    script: list[ScriptStep] = [plan("headset", "laptop", text=original)]
    for rid, item in [("r1", "headset"), ("r2", "laptop")]:
        script += [facts(item), ("agent", [submit(rid, item)]), review()]
    engine, client, _ = build(script)
    result = asyncio.run(engine.run(original, "E001", max_steps=2))
    return result, client


def test_step_limit_accepts_each_decision_on_last_turn(last_turn_decisions):
    result, _ = last_turn_decisions
    assert [(d.request_id, d.decision) for d in result.decisions] == [
        ("r1", "approve"),
        ("r2", "approve"),
    ]


def test_step_limit_restarts_for_each_item(last_turn_decisions):
    _, client = last_turn_decisions
    assert [phase for phase, _ in client.calls] == [
        "split",
        "agent",
        "agent",
        "reflection",
        "agent",
        "agent",
        "reflection",
    ]
    assert not client.script


@pytest.fixture
def later_item_failure():
    original = "I need a headset and a laptop."
    engine, client, _ = build(
        [
            plan("headset", "laptop", text=original),
            facts("headset"),
            ("agent", [submit("r1", "headset")]),
            review(),
            ("agent", RuntimeError("connection lost")),
        ],
    )
    with pytest.raises(IncompleteRequestsError) as caught:
        asyncio.run(engine.run(original, "E001"))
    return caught.value


def test_later_failure_reports_only_unfinished_item(later_item_failure):
    assert later_item_failure.pending_request_ids == ["r2"]


def test_later_failure_preserves_completed_decisions(later_item_failure):
    assert [(d.item, d.decision) for d in later_item_failure.result.decisions] == [
        ("headset", "approve"),
    ]


def test_later_failure_preserves_original_cause(later_item_failure):
    assert str(later_item_failure.__cause__) == "connection lost"


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [{"item": "headset", "text": "invented request"}],
        [{"item": "headset", "text": "headset"}, {"item": " Headset ", "text": "headset"}],
    ],
)
def test_invalid_or_empty_extraction_cannot_start_business_calls(entries):
    engine, _, backend = build(
        [
            (
                "split",
                [
                    EquipmentRequest(f"r{i}", e["item"].strip().lower(), e["text"])
                    for i, e in enumerate(entries, 1)
                ],
            )
        ]
    )
    with pytest.raises(ValueError):
        asyncio.run(engine.run("I need a headset.", "E001"))
    assert not backend.calls


def test_confirmed_approval_is_not_overridden_by_business_rule_checker():
    original = "I need a headset."
    engine, client, _ = build(
        [
            plan("headset", text=original),
            facts("headset"),
            ("agent", [submit("r1", "headset")]),
            review(),
        ],
        budget=0,
    )
    result = asyncio.run(engine.run(original, "E001"))
    # Even an incorrect model/reviewer agreement is now accepted by the workflow.
    assert result.decision == "approve"
    assert len(result.reflections) == 1 and not client.script


@pytest.fixture
def unpriced_approval():
    original = "I need a headset and a laptop."
    engine, client, _ = build(
        [
            plan("headset", "laptop", text=original),
            ("agent", [submit("r1", "headset")]),
            review(),
            ("agent", [submit("r2", "laptop", "deny")]),
            review(),
        ],
    )
    result = asyncio.run(engine.run(original, "E001"))
    return result, client


def test_unpriced_approval_keeps_confirmed_decisions(unpriced_approval):
    result, _ = unpriced_approval
    assert [d.decision for d in result.decisions] == ["approve", "deny"]


def test_unpriced_approval_does_not_reserve_unknown_cost(unpriced_approval):
    _, client = unpriced_approval
    assert client.contexts[1].reserved_budget == {}


def test_unpriced_approval_is_passed_to_next_session(unpriced_approval):
    _, client = unpriced_approval
    assert client.contexts[1].unpriced_approvals == ("headset",)


def test_unpriced_approval_is_traced(unpriced_approval):
    result, _ = unpriced_approval
    assert any("Approval cost not reserved" in t.content for t in result.trace)


def test_unpriced_approval_consumes_all_turns(unpriced_approval):
    _, client = unpriced_approval
    assert client.script == []


@pytest.fixture
def concurrent_submissions():
    # This model double derives responses from each session's context so concurrent
    # runs can interleave naturally without sharing a scripted response queue.
    class LLM:
        async def split_requests(self, request):
            return [EquipmentRequest("r1", request, request)]

        def start_session(self, context, tools):
            class Session:
                async def next_actions(self, results=None):
                    if results is None:
                        return ModelTurn(
                            [
                                use(
                                    "flag_for_human_review",
                                    employee_id=context.employee_id,
                                    request=context.original,
                                    reason="Needs review",
                                )
                            ]
                        )
                    ticket = json.loads(results[0].content)
                    return ModelTurn(
                        [submit("r1", context.task.item, "escalate", ticket["ticket_id"])]
                    )

            return Session()

        async def review(self, context, draft, observations):
            return ReviewResult("confirmed", [])

    async def run():
        both_submissions = asyncio.Barrier(2)

        # Hold both writes at the same barrier to expose accidental shared item state.
        class ConcurrentBackend(Backend):
            async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                if name == "flag_for_human_review":
                    await both_submissions.wait()
                return await super().call(name, arguments)

        backend = ConcurrentBackend()
        workflow = EquipmentWorkflow(LLM(), backend)
        async with asyncio.timeout(3):
            results = await asyncio.gather(
                workflow.run("laptop", "E001", submission_id="laptop-request"),
                workflow.run("webcam", "E001", submission_id="webcam-request"),
            )
        return results, backend

    return asyncio.run(run())


def test_concurrent_submissions_create_separate_tickets(concurrent_submissions):
    _, backend = concurrent_submissions
    assert len(backend.tickets) == 2


def test_concurrent_submissions_use_distinct_retry_keys(concurrent_submissions):
    _, backend = concurrent_submissions
    assert len({ticket["idempotency_key"] for ticket in backend.tickets}) == 2


@pytest.fixture(params=["laptop", "webcam"])
def concurrent_item(concurrent_submissions, request):
    results, backend = concurrent_submissions
    index = ["laptop", "webcam"].index(request.param)
    return results[index], request.param, backend


def test_concurrent_submission_keeps_submission_id(concurrent_item):
    result, item, _ = concurrent_item
    assert result.submission_id == f"{item}-request"


def test_concurrent_submission_keeps_its_own_decision(concurrent_item):
    result, item, _ = concurrent_item
    assert [decision.item for decision in result.decisions] == [item]


def test_concurrent_submission_keeps_its_own_reflection(concurrent_item):
    result, item, _ = concurrent_item
    assert result.reflections[0]["item"] == item


def test_concurrent_submission_keeps_its_own_trace(concurrent_item):
    result, item, _ = concurrent_item
    mismatched = [
        step
        for step in result.trace
        if step.kind != "Requests" and not step.content.startswith(f"[r1: {item}]")
    ]
    assert mismatched == []


def test_concurrent_submission_keeps_its_own_ticket_request(concurrent_item):
    result, item, backend = concurrent_item
    ticket = next(t for t in backend.tickets if t["ticket_id"] == result.ticket_id)
    assert ticket["request"] == item
