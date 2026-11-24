# mock_mcp

Mock equipment-request tools over MCP, with a Claude agent for approval, denial, and
human review.

## Agent requests

```python
from equipment_mcp.agent import run_agent

result = await run_agent("I need a laptop and a headset.", "E001")
for decision in result.decisions:
    print(decision.request_id, decision.item, decision.decision, decision.response)
```

The agent first makes one structured model call to identify new equipment requests.
Background mentions such as an existing monitor order are excluded by the extraction
instructions. The split is still a model judgment; verbatim excerpts and duplicate
categories are validated locally. Repeated mentions of one category are consolidated;
this does not add support for ordering multiple units of the same item.

Each item gets a stable ID within the invocation (`r1`, `r2`, ...), its own tool
observations, review retries, ticket, and final decision. Items are handled sequentially
in request order. `submit_decision` is constrained to the current ID and item. After a
decision is accepted, later calls in that model response are ignored, including ticket
creation. A subsequent item's result cannot overwrite an earlier one.

Earlier approvals reserve department budget for later items in the same invocation.
This is an in-memory check; the mock does not place orders or persist budget spending.
Escalations retain the original employee message verbatim and identify the current
request in the ticket reason. Repeated escalation calls reuse that item's ticket.

Use `result.decisions` for all results. The existing `result.decision` and
`result.ticket_id` accessors remain available for a single item and raise `ValueError`
for multiple items. `result.response` combines the per-item replies without another
model call. `max_steps` applies to each item independently.

If a later item cannot finish, `IncompleteRequestsError.result` retains completed
decisions and its `pending_request_ids` lists unfinished items. Do not blindly rerun
the whole invocation: tickets are persistent, and cross-invocation retry idempotency
is not implemented.

## Responsibility boundaries

| Module | Responsibility |
| --- | --- |
| `models.py` | Application values: requests, decisions, tool calls and results |
| `policy.py` | Local validation, approval evidence and review outcome rules |
| `ports.py` | Interfaces for model sessions, tools, data reads and ticket storage |
| `workflow.py` | Per-item orchestration, retries, finalization and shared budget reservations |
| `foundry.py`, `prompts.py` | Foundry request/response translation, conversation history and prompts |
| `mcp_executor.py` | MCP discovery and tool-result translation |
| `equipment.py` | Equipment lookups, eligibility and ticket business rules |
| `repositories.py` | JSON/file storage and interchangeable in-memory implementations |
| `app.py` | Configuration, client construction and resource cleanup |
| `server.py` | MCP schemas and registration through `create_server(data, tickets)` |

`EquipmentWorkflow` receives a `DecisionModel` and `ToolExecutor`; it never creates a
Foundry client or MCP connection. These interfaces exchange application dataclasses,
not SDK objects. The Foundry adapter owns its conversation history, including opaque
provider blocks. `app.open_workflow()` opens and closes external resources around the
workflow. Constructors only store dependencies; work starts with explicit method calls.

Tests can therefore construct the normal workflow with scripted dependencies:

```python
from equipment_mcp.workflow import EquipmentWorkflow

# Objects implementing the interfaces in ports.py; see test_agent_workflow.py.
workflow = EquipmentWorkflow(scripted_model, in_memory_tools, clock=fixed_date)
result = await workflow.run("I need a headset.", "E001")
```

Business data access also works independently of either SDK or a running agent:

```python
from equipment_mcp.equipment import get_item_catalog
from equipment_mcp.repositories import MemoryEquipmentData

data = MemoryEquipmentData(
    {
        "catalog": {
            "items": {"headset": {"price": 120}},
            "currency": "USD",
            "auto_approve_limit": 2000,
        },
    }
)
catalog = get_item_catalog("headset", data=data)
```

Business operations are standalone functions. Reads receive `data`; ticket creation
receives `tickets`. Date-sensitive operations accept explicit `today` or `now` values.
There is no service object to construct.

The existing `agent.run_agent` and public `tools.py` functions remain compatibility
entry points. New tests should inject dependencies directly instead of patching those
modules, bypassing constructors with `__new__`, or constructing SDK response objects
to test business rules. Adapter tests are the place for SDK response fixtures.

## Local checks

```sh
uv run pytest
uv run ruff check src tests
```

Benchmark-only tests live under the ignored `bench/tests/` directory and are excluded
from the default test suite. When the local benchmark files are available, run them
separately with `uv run python -m pytest bench/tests -q`.

Workflow tests use scripted model responses, including the two-submission regression,
multiple items, isolated tickets and evidence, combined budget limits, and partial
failure. Separate adapter tests check Foundry message translation and a real local
MCP stdio round trip. A boundary test blocks SDK imports while running local policy
and data access. They make no paid model calls. Historical benchmark results under `bench/`
describe the earlier single-decision agent; the benchmark harness has not been migrated
to this workflow or rerun against it.
