"""Exercise scheduling through the workflow, using events rather than timing claims."""

import asyncio
import json

import pytest

from equipment_mcp.models import IncompleteRequestsError, ToolExecutionError
from equipment_mcp.workflow import EquipmentWorkflow
from tests.test_agent_workflow import Backend, ScriptedModel, plan, review, submit, use

ORIGINAL = "I need a headset."


def run_workflow(model, backend):
    async def run():
        async with asyncio.timeout(3):
            return await EquipmentWorkflow(model, backend).run(ORIGINAL, "E001")

    return asyncio.run(run())


@pytest.mark.parametrize("inventory_error", [False, True])
def test_reads_overlap_and_results_keep_call_order_even_on_tool_error(inventory_error):
    first = use("get_employee_info", employee_id="E001")
    second = use("check_inventory", item="headset")

    class GatedBackend(Backend):
        def __init__(self):
            super().__init__()
            self.employee_started = asyncio.Event()
            self.inventory_finished = asyncio.Event()
            self.finished = []

        async def call(self, name, args):
            if name == "get_employee_info":
                self.employee_started.set()
                await self.inventory_finished.wait()
            elif name == "check_inventory":
                await self.employee_started.wait()
                self.finished.append(name)
                self.inventory_finished.set()
                if inventory_error:
                    raise ToolExecutionError("Inventory temporarily unavailable")
                return await super().call(name, args)
            elif name == "get_department_budget":
                # A dependent lookup is selected on a later model turn.
                assert "get_employee_info" in self.finished
            value = await super().call(name, args)
            self.finished.append(name)
            return value

    model = ScriptedModel(
        [
            plan("headset", text=ORIGINAL),
            ("agent", [first, second]),
            ("agent", [use("get_department_budget", department="IT")]),
            ("agent", [submit("r1", "headset", "deny")]),
            review(),
        ]
    )
    backend = GatedBackend()
    result = run_workflow(model, backend)
    assert backend.finished[:2] == ["check_inventory", "get_employee_info"]
    returned = model.calls[2][1]["results"]
    assert [r.call_id for r in returned] == [first.call_id, second.call_id]
    assert json.loads(returned[0].content)["department"] == "IT"
    assert returned[1].is_error is inventory_error
    if inventory_error:
        assert "temporarily unavailable" in returned[1].content
    observed = [s.content for s in result.trace if s.kind == "Observation"]
    assert "get_employee_info: " in observed[0] and "check_inventory: " in observed[1]
    assert all(first.call_id not in entry and second.call_id not in entry for entry in observed)
    execution = [s.content for s in result.trace if s.kind == "Execution"]
    assert len(execution) == 1
    assert "PARALLEL 2 reads: get_employee_info, check_inventory" in execution[0]
    assert "wall time" not in execution[0] and "Duration" not in execution[0]
    # Scheduling diagnostics are for terminal output, not added to the model context.
    assert all(s.kind == "Observation" for s in model.calls[-1][1]["observations"])
    assert not model.script


def test_writes_and_decision_wait_for_reads_and_trailing_calls_do_not_run():
    class TrackedBackend(Backend):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0
            self.events = []

        async def call(self, name, args):
            if name == "flag_for_human_review":
                assert self.active == 0
            self.events.append(("start", name))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0)
                return await super().call(name, args)
            finally:
                self.active -= 1
                self.events.append(("end", name))

    def flag():
        return use("flag_for_human_review", employee_id="E001", request=ORIGINAL, reason="Review")

    model = ScriptedModel(
        [
            plan("headset", text=ORIGINAL),
            (
                "agent",
                [
                    use("get_employee_info", employee_id="E001"),
                    use("get_item_catalog", item="headset"),
                    flag(),
                    flag(),
                    use("check_inventory", item="headset"),
                    submit("r1", "headset", "escalate", "ESC-0001"),
                    use("get_department_budget", department="IT"),
                    use("get_open_requests", employee_id="E001"),
                    flag(),
                ],
            ),
            review(),
        ]
    )
    backend = TrackedBackend()
    result = run_workflow(model, backend)
    assert backend.max_active == 2
    assert len(backend.tickets) == 1
    names = [name for name, _ in backend.calls]
    assert names == [
        "get_employee_info",
        "get_item_catalog",
        "flag_for_human_review",
        "check_inventory",
    ]
    assert backend.events.index(("end", "flag_for_human_review")) < backend.events.index(
        ("start", "check_inventory")
    )
    observations = model.calls[-1][1]["observations"]
    assert any("check_inventory" in step.content for step in observations)
    assert sum("additional call ignored" in step.content for step in result.trace) == 3
    assert result.ticket_id == "ESC-0001"


def test_repeated_reads_and_unclassified_tools_are_sequential():
    class BackendWithUnknownTool(Backend):
        names = Backend.names + ["custom_operation"]

        def __init__(self):
            super().__init__()
            self.active = set()
            self.events = []

        async def call(self, name, args):
            assert name not in self.active
            if name == "custom_operation":
                assert not self.active
            self.active.add(name)
            self.events.append(("start", name))
            try:
                await asyncio.sleep(0)
                return {} if name == "custom_operation" else await super().call(name, args)
            finally:
                self.active.remove(name)
                self.events.append(("end", name))

    model = ScriptedModel(
        [
            plan("headset", text=ORIGINAL),
            (
                "agent",
                [
                    use("get_item_catalog", item="headset"),
                    use("check_inventory", item="headset"),
                    use("get_item_catalog", item="headset"),
                    use("custom_operation"),
                    use("get_employee_info", employee_id="E001"),
                ],
            ),
            ("agent", [submit("r1", "headset", "deny")]),
            review(),
        ]
    )
    backend = BackendWithUnknownTool()
    run_workflow(model, backend)
    events = backend.events
    assert events.index(("end", "get_item_catalog")) < events.index(
        ("start", "get_item_catalog"), 1
    )
    assert events.index(("end", "custom_operation")) < events.index(("start", "get_employee_info"))


def test_fatal_read_error_cancels_siblings_before_any_side_effect():
    class BrokenBackend(Backend):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.cleaned_up = False

        async def call(self, name, args):
            if name == "get_employee_info":
                self.started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cleaned_up = True
            if name == "check_inventory":
                await self.started.wait()
                raise ConnectionError("MCP connection lost")
            raise AssertionError("Must not execute after the connection fails")

    model = ScriptedModel(
        [
            plan("headset", text=ORIGINAL),
            (
                "agent",
                [
                    use("get_employee_info", employee_id="E001"),
                    use("check_inventory", item="headset"),
                    use(
                        "flag_for_human_review",
                        employee_id="E001",
                        request=ORIGINAL,
                        reason="Review",
                    ),
                ],
            ),
        ]
    )
    backend = BrokenBackend()
    with pytest.raises(IncompleteRequestsError) as caught:
        run_workflow(model, backend)
    assert backend.cleaned_up and not backend.tickets
    assert caught.value.pending_request_ids == ["r1"]
    assert any(isinstance(exc, ConnectionError) for exc in caught.value.__cause__.exceptions)
