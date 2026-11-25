"""Exercise scheduling through the workflow, using events rather than timing claims."""

import asyncio
import json
from typing import Any

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


@pytest.fixture(params=[False, True], ids=["success", "tool-error"])
def parallel_reads(request):
    inventory_error = request.param
    first = use("get_employee_info", employee_id="E001")
    second = use("check_inventory", item="headset")

    # Events force inventory to finish first, making overlap and result ordering
    # deterministic while the real workflow schedules the calls.
    class GatedBackend(Backend):
        def __init__(self):
            super().__init__()
            self.employee_started = asyncio.Event()
            self.inventory_finished = asyncio.Event()
            self.finished = []

        async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if name == "get_employee_info":
                self.employee_started.set()
                await self.inventory_finished.wait()
            elif name == "check_inventory":
                await self.employee_started.wait()
                self.finished.append(name)
                self.inventory_finished.set()
                if inventory_error:
                    raise ToolExecutionError("Inventory temporarily unavailable")
                return await super().call(name, arguments)
            elif name == "get_department_budget":
                self.employee_finished_before_budget = "get_employee_info" in self.finished
            value = await super().call(name, arguments)
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
    return result, model, backend, first, second, inventory_error


def test_independent_reads_overlap(parallel_reads):
    _, _, backend, _, _, _ = parallel_reads
    assert backend.finished[:2] == ["check_inventory", "get_employee_info"]


def test_dependent_lookup_waits_for_employee_read(parallel_reads):
    _, _, backend, _, _, _ = parallel_reads
    assert backend.employee_finished_before_budget


def test_parallel_results_keep_model_call_order(parallel_reads):
    _, model, _, first, second, _ = parallel_reads
    returned = model.calls[2][1]["results"]
    assert [r.call_id for r in returned] == [first.call_id, second.call_id]


def test_parallel_read_preserves_successful_payload(parallel_reads):
    _, model, _, _, _, _ = parallel_reads
    returned = model.calls[2][1]["results"]
    assert json.loads(returned[0].content)["department"] == "IT"


def test_parallel_read_preserves_error_flag(parallel_reads):
    _, model, _, _, _, inventory_error = parallel_reads
    returned = model.calls[2][1]["results"]
    assert returned[1].is_error is inventory_error


@pytest.mark.parametrize("index,name", [(0, "get_employee_info"), (1, "check_inventory")])
def test_parallel_observations_keep_tool_order(parallel_reads, index, name):
    result, _, _, _, _, _ = parallel_reads
    observed = [s.content for s in result.trace if s.kind == "Observation"]
    assert f"{name}: " in observed[index]


def test_parallel_observations_omit_call_ids(parallel_reads):
    result, _, _, first, second, _ = parallel_reads
    observed = [s.content for s in result.trace if s.kind == "Observation"]
    assert [entry for entry in observed if first.call_id in entry or second.call_id in entry] == []


def test_parallel_trace_records_one_execution_group(parallel_reads):
    result, _, _, _, _, _ = parallel_reads
    execution = [s.content for s in result.trace if s.kind == "Execution"]
    assert len(execution) == 1
    assert "PARALLEL 2 reads: get_employee_info, check_inventory" in execution[0]


def test_parallel_trace_omits_timing_diagnostics(parallel_reads):
    result, _, _, _, _, _ = parallel_reads
    execution = [s.content for s in result.trace if s.kind == "Execution"]
    assert "wall time" not in execution[0]
    assert "Duration" not in execution[0]


def test_parallel_scheduling_diagnostics_stay_out_of_model_context(parallel_reads):
    _, model, _, _, _, _ = parallel_reads
    observations = model.calls[-1][1]["observations"]
    assert [s for s in observations if s.kind != "Observation"] == []


def test_parallel_reads_consume_expected_model_turns(parallel_reads):
    _, model, _, _, _, _ = parallel_reads
    assert model.script == []


@pytest.fixture
def write_barrier():
    # Record active calls around a cooperative yield to observe read/write barriers
    # without relying on elapsed time or a live MCP server.
    class TrackedBackend(Backend):
        def __init__(self):
            super().__init__()
            self.active = 0
            self.max_active = 0
            self.events = []
            self.active_at_write = []

        async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if name == "flag_for_human_review":
                self.active_at_write.append(self.active)
            self.events.append(("start", name))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0)
                return await super().call(name, arguments)
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
    return result, model, backend


def test_write_barrier_allows_two_parallel_reads(write_barrier):
    _, _, backend = write_barrier
    assert backend.max_active == 2


def test_ticket_write_waits_for_all_reads(write_barrier):
    _, _, backend = write_barrier
    assert backend.active_at_write == [0]


def test_duplicate_write_creates_one_ticket(write_barrier):
    _, _, backend = write_barrier
    assert len(backend.tickets) == 1


def test_decision_skips_trailing_business_calls(write_barrier):
    _, _, backend = write_barrier
    assert [name for name, _ in backend.calls] == [
        "get_employee_info",
        "get_item_catalog",
        "flag_for_human_review",
        "check_inventory",
    ]


def test_later_read_waits_for_ticket_write(write_barrier):
    _, _, backend = write_barrier
    assert backend.events.index(("end", "flag_for_human_review")) < backend.events.index(
        ("start", "check_inventory")
    )


def test_review_receives_observation_from_read_after_write(write_barrier):
    _, model, _ = write_barrier
    observations = model.calls[-1][1]["observations"]
    assert any("check_inventory" in step.content for step in observations)


def test_decision_traces_each_ignored_trailing_call(write_barrier):
    result, _, _ = write_barrier
    assert sum("additional call ignored" in step.content for step in result.trace) == 3


def test_decision_uses_ticket_created_before_submission(write_barrier):
    result, _, _ = write_barrier
    assert result.ticket_id == "ESC-0001"


@pytest.fixture
def sequential_tools():
    # Add an unclassified tool and record overlap so conservative scheduling can
    # be checked independently of any real tool implementation.
    class BackendWithUnknownTool(Backend):
        names = Backend.names + ["custom_operation"]

        def __init__(self):
            super().__init__()
            self.active = set()
            self.events = []
            self.overlapping_repeats = []
            self.active_at_custom_call = []

        async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            if name in self.active:
                self.overlapping_repeats.append(name)
            if name == "custom_operation":
                self.active_at_custom_call.append(set(self.active))
            self.active.add(name)
            self.events.append(("start", name))
            try:
                await asyncio.sleep(0)
                return {} if name == "custom_operation" else await super().call(name, arguments)
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
    return backend


def test_repeated_reads_do_not_overlap(sequential_tools):
    backend = sequential_tools
    assert backend.overlapping_repeats == []


def test_repeated_read_starts_after_previous_call_ends(sequential_tools):
    backend = sequential_tools
    events = backend.events
    assert events.index(("end", "get_item_catalog")) < events.index(
        ("start", "get_item_catalog"), 1
    )


def test_unclassified_tool_waits_for_all_reads(sequential_tools):
    backend = sequential_tools
    assert backend.active_at_custom_call == [set()]


def test_read_after_unclassified_tool_waits_for_completion(sequential_tools):
    backend = sequential_tools
    events = backend.events
    assert events.index(("end", "custom_operation")) < events.index(("start", "get_employee_info"))


@pytest.fixture
def fatal_read():
    # Hold one read open until cancellation, then fail its sibling. This verifies
    # cleanup and side-effect suppression without a broken network connection.
    class BrokenBackend(Backend):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.cleaned_up = False

        async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
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
    return caught.value, backend


def test_fatal_read_error_cleans_up_sibling(fatal_read):
    _, backend = fatal_read
    assert backend.cleaned_up


def test_fatal_read_error_prevents_ticket_creation(fatal_read):
    _, backend = fatal_read
    assert backend.tickets == []


def test_fatal_read_error_reports_pending_request(fatal_read):
    error, _ = fatal_read
    assert error.pending_request_ids == ["r1"]


def test_fatal_read_error_preserves_connection_failure(fatal_read):
    error, _ = fatal_read
    assert any(isinstance(exc, ConnectionError) for exc in error.__cause__.exceptions)


@pytest.mark.parametrize("parallel_reads", [True], indirect=True, ids=["tool-error"])
def test_parallel_read_preserves_error_message(parallel_reads):
    _, model, _, _, _, _ = parallel_reads
    returned = model.calls[2][1]["results"]
    assert "temporarily unavailable" in returned[1].content
