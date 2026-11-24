"""Scripted model responses exercise the real orchestration without paid API calls."""

import asyncio
import copy
import itertools
import json
from datetime import date

import pytest

from equipment_mcp.equipment import flag_for_human_review
from equipment_mcp.models import (
    EquipmentRequest,
    IncompleteRequestsError,
    ModelTurn,
    ReviewResult,
    ToolCall,
)
from equipment_mcp.policy import MAX_REFLECTION_REJECTIONS
from equipment_mcp.repositories import SqliteTicketStore
from equipment_mcp.workflow import EquipmentWorkflow

IDS = itertools.count()


def use(name, **args):
    return ToolCall(f"call-{next(IDS)}", name, args)


def submit(rid, item, decision="approve", ticket=None):
    return use(
        "submit_decision",
        request_id=rid,
        item=item,
        decision=decision,
        response=f"{item}: {decision}.",
        ticket_id=ticket,
    )


def plan(*items, text):
    return ("split", [EquipmentRequest(f"r{i}", item, text) for i, item in enumerate(items, 1)])


def review(verdict="confirmed"):
    return (
        "reflection",
        ReviewResult(verdict, [] if verdict == "confirmed" else ["wrong decision"]),
    )


def facts(item):
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


class ScriptedModel:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.contexts = []

    def pop(self, phase, detail):
        expected, value = self.script.pop(0)
        assert phase == expected
        self.calls.append((phase, copy.deepcopy(detail)))
        if isinstance(value, Exception):
            raise value
        return value

    async def split_requests(self, request):
        return self.pop("split", request)

    def start_session(self, context, tools):
        self.contexts.append(context)
        model = self

        class Session:
            async def next_actions(self, results=None):
                return ModelTurn(model.pop("agent", {"context": context, "results": results}))

        return Session()

    async def review(self, context, draft, observations):
        return self.pop(
            "reflection", {"context": context, "draft": draft, "observations": observations}
        )


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

    def __init__(self, budget=1000):
        self.calls = []
        self.tickets = []
        self.budget = budget

    @property
    def definitions(self):
        return [
            {"name": name, "description": name, "input_schema": {"type": "object"}}
            for name in self.names
        ]

    async def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
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
            result = {**args, "eligible": True, "status": "eligible_new"}
        elif name == "get_item_catalog":
            result = {**args, "price": 120, "auto_approve_limit": 2000}
        elif name == "get_department_budget":
            result = {**args, "remaining": self.budget}
        elif name == "check_inventory":
            result = {**args, "in_stock": 3}
        elif name == "flag_for_human_review":
            result = {**args, "ticket_id": f"ESC-{len(self.tickets) + 1:04d}"}
            self.tickets.append(result)
        else:
            raise AssertionError(name)
        return result


def build(script, *, budget=1000):
    model, backend = ScriptedModel(script), Backend(budget)
    engine = EquipmentWorkflow(model, backend, clock=lambda: date(2026, 11, 23))
    return engine, model, backend


def test_retry_keys_are_host_owned_and_stable_when_items_are_reordered(tmp_path):
    original = "My laptop is broken. I also need a webcam."

    class PersistentBackend(Backend):
        async def call(self, name, args):
            if name == "flag_for_human_review":
                self.calls.append((name, copy.deepcopy(args)))
                return flag_for_human_review(
                    **args, tickets=SqliteTicketStore(tmp_path / "tickets.sqlite3")
                )
            return await super().call(name, args)

    def run(items, submission_id, ticket_ids):
        script = [plan(*items, text=original)]
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
        assert len(set(keys)) == 2
        assert "model-must-not-control-this" not in keys
        assert result.submission_id
        return result, keys

    ids = {"laptop": "ESC-0001", "webcam": "ESC-0002"}
    first, keys = run(["laptop", "webcam"], "submission-1", ids)
    retry, retry_keys = run(["webcam", "laptop"], "submission-1", ids)
    assert keys == list(reversed(retry_keys))
    assert {d.ticket_id for d in first.decisions} == {d.ticket_id for d in retry.decisions}
    assert first.submission_id == retry.submission_id == "submission-1"
    new, new_keys = run(["laptop", "webcam"], None, {"laptop": "ESC-0003", "webcam": "ESC-0004"})
    assert new.submission_id != first.submission_id
    assert not set(keys).intersection(new_keys)


def test_blank_submission_id_is_rejected_before_model_call():
    workflow = EquipmentWorkflow(ScriptedModel([]), Backend())
    with pytest.raises(ValueError, match="submission_id"):
        asyncio.run(workflow.run("New webcam", "E001", submission_id=" "))


def test_a09_duplicate_submission_does_not_overwrite_or_run_trailing_side_effect():
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
    assert result.decision == "approve"
    assert [(d.request_id, d.item) for d in result.decisions] == [("r1", "headset")]
    assert len(result.reflections) == 1 and not backend.tickets
    assert sum("additional call ignored" in t.content for t in result.trace) == 2
    assert len(client.calls) == 4 and not client.script


def test_multi_item_results_are_retained_and_wait_for_all_requests():
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
    assert [(d.request_id, d.item, d.decision) for d in result.decisions] == [
        ("r1", "headset", "approve"),
        ("r2", "monitor", "deny"),
    ]
    assert "headset" in result.response and "monitor" in result.response
    with pytest.raises(ValueError, match="result.decisions"):
        _ = result.decision
    with pytest.raises(ValueError, match="result.decisions"):
        _ = result.ticket_id
    assert [c.task.request_id for c in client.contexts] == ["r1", "r2"]
    assert client.contexts[1].reserved_budget == {"IT": 120}
    assert [r["request_id"] for r in result.reflections] == ["r1", "r2"]
    assert not client.script


def test_wrong_request_item_and_employee_are_rejected_before_review_or_execution():
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
    assert result.decision == "approve" and len(result.reflections) == 1
    assert not any(
        a.get("employee_id") == "E002" or a.get("item") == "monitor" for _, a in backend.calls
    )
    assert len([t for t in result.trace if "does not match" in t.content]) == 4
    assert not client.script


def test_review_can_reject_missing_evidence_for_a_new_item():
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
    assert len(result.decisions) == 2
    assert any("Review rejected the decision" in t.content for t in result.trace)
    assert len(result.reflections) == 3 and not client.script


def test_ticket_is_scoped_idempotent_and_preserves_original_request():
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
    assert [d.ticket_id for d in result.decisions] == ["ESC-0001", "ESC-0002"]
    assert len(backend.tickets) == 2
    assert all(t["request"] == original for t in backend.tickets)
    assert backend.tickets[0]["reason"].startswith("[r1: laptop]")
    assert backend.tickets[1]["reason"].startswith("[r2: webcam]")
    assert any("ticket_id is missing" in t.content for t in result.trace)
    assert not client.script


def test_review_rejects_combined_budget_and_receives_earlier_reservations():
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
    assert [d.decision for d in result.decisions] == ["approve", "escalate"]
    assert client.contexts[1].reserved_budget == {"IT": 120}
    assert any("Review rejected the decision" in t.content for t in result.trace)
    assert len(backend.tickets) == 1 and not client.script


def test_review_rejections_are_per_request_and_forced_tickets_are_separate():
    original = "I need a headset and a laptop."
    script = [plan("headset", "laptop", text=original)]
    for rid, item in [("r1", "headset"), ("r2", "laptop")]:
        for _ in range(MAX_REFLECTION_REJECTIONS + 1):
            script += [("agent", [submit(rid, item, "deny")]), review("rejected")]
    engine, client, backend = build(script)
    result = asyncio.run(engine.run(original, "E001"))
    assert len(result.reflections) == 6
    assert [d.decision for d in result.decisions] == ["escalate", "escalate"]
    assert [d.ticket_id for d in result.decisions] == ["ESC-0001", "ESC-0002"]
    assert len(backend.tickets) == 2 and not client.script


@pytest.mark.parametrize("max_steps", [1, 3])
def test_step_limit_stops_without_an_extra_call_or_automatic_ticket(max_steps):
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
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == f"No valid decision for r1 after {max_steps} steps"
    assert caught.value.pending_request_ids == ["r1"]
    assert caught.value.result.decisions == []
    assert caught.value.result.reflections == []
    assert [phase for phase, _ in client.calls] == ["split"] + ["agent"] * max_steps
    assert len(client.script) == 2  # The next action and review must remain unconsumed.
    assert not backend.calls and not backend.tickets


def test_step_limit_is_per_item_and_accepts_decision_on_last_turn():
    original = "I need a headset and a laptop."
    script = [plan("headset", "laptop", text=original)]
    for rid, item in [("r1", "headset"), ("r2", "laptop")]:
        script += [facts(item), ("agent", [submit(rid, item)]), review()]
    engine, client, _ = build(script)
    result = asyncio.run(engine.run(original, "E001", max_steps=2))
    assert [(d.request_id, d.decision) for d in result.decisions] == [
        ("r1", "approve"),
        ("r2", "approve"),
    ]
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


def test_later_failure_preserves_completed_decisions():
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
    assert caught.value.pending_request_ids == ["r2"]
    assert [(d.item, d.decision) for d in caught.value.result.decisions] == [("headset", "approve")]
    assert str(caught.value.__cause__) == "connection lost"


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [{"item": "headset", "text": "invented request"}],
        [{"item": "headset", "text": "headset"}, {"item": " Headset ", "text": "headset"}],
    ],
)
def test_invalid_or_empty_extraction_cannot_start_business_calls(entries):
    engine, client, backend = build(
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


def test_confirmed_approval_without_facts_does_not_crash_budget_accounting():
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
    assert [d.decision for d in result.decisions] == ["approve", "deny"]
    assert client.contexts[1].reserved_budget == {}
    assert client.contexts[1].unpriced_approvals == ("headset",)
    assert any("Approval cost not reserved" in t.content for t in result.trace)
    assert not client.script


def test_concurrent_submissions_on_one_workflow_keep_item_state_isolated():
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

        class ConcurrentBackend(Backend):
            async def call(self, name, args):
                if name == "flag_for_human_review":
                    await both_submissions.wait()
                return await super().call(name, args)

        backend = ConcurrentBackend()
        workflow = EquipmentWorkflow(LLM(), backend)
        async with asyncio.timeout(3):
            results = await asyncio.gather(
                workflow.run("laptop", "E001", submission_id="laptop-request"),
                workflow.run("webcam", "E001", submission_id="webcam-request"),
            )
        assert len(backend.tickets) == 2
        assert len({ticket["idempotency_key"] for ticket in backend.tickets}) == 2
        for result, item in zip(results, ("laptop", "webcam"), strict=True):
            assert result.submission_id == f"{item}-request"
            assert len(result.decisions) == 1 and result.decisions[0].item == item
            assert result.reflections[0]["item"] == item
            assert all(
                step.content.startswith(f"[r1: {item}]")
                for step in result.trace
                if step.kind != "Requests"
            )
            ticket = next(t for t in backend.tickets if t["ticket_id"] == result.ticket_id)
            assert ticket["request"] == item

    asyncio.run(run())
