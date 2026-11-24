"""Scripted model responses exercise the real orchestration without paid API calls."""

import asyncio
import copy
import itertools
from datetime import date

import pytest

from equipment_mcp.models import (
    EquipmentRequest,
    IncompleteRequestsError,
    ModelTurn,
    ReviewResult,
    ToolCall,
    ToolDefinition,
)
from equipment_mcp.policy import MAX_REFLECTION_REJECTIONS
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
            ToolDefinition(name, name, {"type": "object", "properties": {}}) for name in self.names
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


def test_each_item_needs_fresh_evidence():
    original = "I need a headset and laptop."
    engine, client, _ = build(
        [
            plan("headset", "laptop", text=original),
            facts("headset"),
            ("agent", [submit("r1", "headset")]),
            review(),
            ("agent", [submit("r2", "laptop")]),
            facts("laptop"),
            ("agent", [submit("r2", "laptop")]),
            review(),
        ],
    )
    result = asyncio.run(engine.run(original, "E001"))
    assert len(result.decisions) == 2
    assert any("price not checked for laptop" in t.content for t in result.trace)
    assert len(result.reflections) == 2 and not client.script


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


def test_combined_approvals_cannot_exceed_department_budget():
    original = "I need a headset and a laptop."
    engine, client, backend = build(
        [
            plan("headset", "laptop", text=original),
            facts("headset"),
            ("agent", [submit("r1", "headset")]),
            review(),
            facts("laptop"),
            ("agent", [submit("r2", "laptop")]),
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
    assert any("remaining budget 80 after earlier approvals" in t.content for t in result.trace)
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
