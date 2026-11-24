# mock_mcp

Mock equipment-request tools over MCP, with a Claude agent for approval, denial, and
human review.

## CLI

Choose one mode explicitly:

Set `FOUNDRY_MODEL` to your deployment name in `.env` or the environment. There is
no fallback model; a missing or blank value stops execution before opening clients.

```sh
# Run all 11 predefined requests from demo_requests.py
uv run python -m equipment_mcp.agent --demo

# Enter an employee ID and one free-text request (which may contain multiple items)
uv run python -m equipment_mcp.agent --interactive
```

Both modes print the trace, decisions and reflection results. The 11 demos cover
one approval, four denials and six escalations; see the case table in `plan.md`.
Demo mode compares results with the expected decisions and ends with totals,
decision accuracy and mismatched cases.
This score checks decision labels, not response wording or reasoning quality.
Empty interactive input is prompted again. Without a mode,
the CLI shows usage and exits before opening clients or making model calls.
Terminal traces use cyan for Thought, yellow for Action, green for Observation and
magenta for Reflection. Blue Execution entries show parallel tool groups without
timings. Tool results appear as Observations. Redirected output stays plain text;
set `NO_COLOR=1` to disable colors.

When `MCP_SERVER_URL` is set, start `uv run python -m equipment_mcp.server` in a
separate terminal first. For an automatically launched stdio server, use
`MCP_SERVER_URL="" uv run python -m equipment_mcp.agent --interactive` (or `--demo`).

## Agent requests

```python
from equipment_mcp.app import run_agent

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

Earlier approvals with known department and price reserve budget for later items in
the same invocation. If either is missing, the approval is retained and subsequent
model calls receive a warning that reserved totals are incomplete.
This is in-memory accounting; the mock does not place orders or persist budget spending.
Escalations retain the original employee message verbatim and identify the current
request in the ticket reason. Repeated escalation calls reuse that item's ticket.

Use `result.decisions` for all results. The existing `result.decision` and
`result.ticket_id` accessors remain available for a single item and raise `ValueError`
for multiple items. `result.response` combines the per-item replies without another
model call. `max_steps` defaults to 14 iterations per item. Each iteration includes
one next-action model call and its requested tool executions; extraction and final
review calls are additional. Exhausting the limit raises an error rather than
automatically escalating.

If a later item cannot finish, `IncompleteRequestsError.result` retains completed
decisions and its `pending_request_ids` lists unfinished items.

## Prompt caching

Parallel read-only tool execution and prompt caching are enabled by default.
`foundry.py` enables 5-minute automatic caching on extraction, each ReAct turn,
and final review. Each system prompt also has an explicit cache breakpoint.
The last common MCP tool has another breakpoint before the per-item
`submit_decision` definition, so changing its item/request ID does not invalidate
the shared tool prefix. The growing conversation is handled by automatic caching.

Full prompts and tool definitions are still sent each time. Cache hits reuse an
identical prefix; they do not reduce context length or cache the final decision.
Hits depend on provider minimum-length and expiry rules. New loop-reflection
instructions after new tool results are new input; earlier copies can be reused
as part of the unchanged history. There is no extra model call for caching.

## Concurrent tickets and retries

The default ticket store is SQLite (`data/tickets.sqlite3`). Set `EQUIPMENT_TICKET_DB`
on the server to choose another file. All server processes sharing tickets must use
the same local database file. Employee and policy lookups still read JSON.

SQLite allocates the integer primary key inside a transaction; the display format
remains `ESC-0001`. A `UNIQUE` constraint on the optional idempotency key prevents
duplicate tickets for retries. Each operation opens its own connection, waits up to
10 seconds for a write lock, and returns only after commit. A lock timeout is an
error, not a successful ticket. The server factory runs synchronous business functions
in worker threads so database waits do not block the HTTP event loop.

For a retryable agent request, generate a submission ID in the caller **before** the
first call and preserve it across retries:

```python
result = await run_agent(
    "My laptop screen is cracked.", "E002", submission_id="request-from-ui-123"
)
# On retry, pass the same message, employee and submission_id.
```

The workflow derives a per-item key from submission ID, employee ID and normalized
item category, and overwrites any model-supplied key. Reordering extracted items
does not change their keys. An omitted submission ID creates a new UUID, available
as `result.submission_id` (also in a partial-failure result). New submissions should
use new IDs even when their text is identical.

Direct MCP clients can supply `idempotency_key` to `flag_for_human_review`. With no
key, each call creates a new ticket. With the same key, the first ticket is returned,
including its original reason and timestamp. Reusing that key for a different
employee or original message raises an error. This deduplicates ticket creation;
it does not cache model decisions or make a whole multi-item workflow transactional.
Budget reservations remain local to one invocation.

Old `escalations.jsonl` files are preserved and are not automatically imported.
SQLite IDs are unique within the new database, not across those archived logs.
Historical JSONL ticket writing lives only in the ignored `bench/compat/` package.
The application uses SQLite; benchmark JSONL storage has no concurrency guarantees.

## Responsibility boundaries

| Module | Responsibility |
| --- | --- |
| `models.py` | Application values: requests, decisions, tool calls and results |
| `policy.py` | Request/ticket validation, fact recording and review outcome rules |
| `ports.py` | Interfaces for model sessions, tools, data reads and ticket storage |
| `workflow.py` | Per-item orchestration, retries, finalization and shared budget reservations |
| `foundry.py`, `prompts.py` | Foundry request/response translation, conversation history and prompts |
| `mcp_executor.py` | SDK tool wrappers and JSON facts for the workflow |
| `equipment.py` | Equipment lookups, eligibility and ticket business rules |
| `repositories.py` | JSON data reads, SQLite tickets and in-memory implementations |
| `storage.py` | Default JSON paths and SQLite repository construction |
| `app.py` | Configuration, client construction and resource cleanup |
| `server.py` | MCP schemas and registration through `create_server(data, tickets)` |

`EquipmentWorkflow` receives an `LLMAdapter` and `ToolExecutor`; it never creates a
Foundry client or MCP connection. These interfaces exchange application dataclasses
and plain dictionaries, not SDK objects. The Foundry adapter owns its conversation history, including opaque
provider blocks. `app.open_workflow()` opens and closes external resources around the
workflow. Constructors only store dependencies; work starts with explicit method calls.

`run()` splits a submission and calls `run_item()` for each item. The item loop only
asks the LLM for actions, executes them and checks for a final decision. Tool dispatch
and parallel batches live in `_execute_calls()` / `_execute_parallel()`; draft
validation and reflection live in `_submit_decision()`. `_RunState` holds shared
submission results and budget accounting, while `_ItemState` holds one item's facts,
tickets and review progress. Both are created per invocation, never stored on the
workflow instance. This also keeps concurrent submissions on one workflow isolated.

`app.py` uses MCP SDK `Client` to manage the transport, session initialization and
cleanup. `mode="legacy"` preserves the existing initialize handshake. The adapter
loads tool pages with SDK `list_tools`, then wraps them with Anthropic SDK
`async_mcp_tool`. SDK `to_dict()` definitions go directly to Foundry; there is no
intermediate `ToolDefinition` class or second schema conversion. The SDK wrapper
also calls MCP and converts results/errors. The remaining adapter decodes JSON
objects for budget accounting and maps tool errors into workflow observations.
The application keeps its own loop to control per-item finalization and reflection.
Within one model turn, consecutive calls to the seven known read-only tools run
concurrently with `asyncio.TaskGroup`, at most one call per tool in each batch.
Repeated calls to the same tool, ticket creation, decision submission and unclassified
tools separate batches and retain their order. Results and observations are returned
in call order. Traces show tool names; call IDs are retained internally for matching
LLM tool calls to their results. Recoverable tool errors become individual
results; unexpected failures cancel and await the other reads before unwinding.
The model must wait for earlier results when it needs them to choose arguments;
the scheduler does not infer dependencies or fill in parameters. No extra model call
is added, and separate requested items still run sequentially.
From the second turn, `LOOP_REFLECTION_PROMPT` is appended after tool results to
request an evidence check and next step within the existing model call. The separate
final draft review remains. Business-rule decisions rely on the agent and this LLM
review; there is no deterministic approval checker. Request scope, response fields
and ticket links are still validated in code.

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

`app.run_agent` is the application API; `agent.py` only starts the CLI. `storage.py`
creates the default JSON and SQLite repositories. `server.create_server(data, tickets)`
registers MCP tools that call `equipment.py` directly with those repositories. Historical benchmark exports and helpers live in
the ignored `bench/compat/` package, which the application does not import.
Tests should inject dependencies directly instead of bypassing constructors with
`__new__` or constructing SDK response objects to test business rules. Adapter tests
are the place for SDK response fixtures.

## Local checks

```sh
uv run pytest
uv run ruff check src tests
```

Benchmark-only tests live under the ignored `bench/tests/` directory and are excluded
from the default test suite. When the local benchmark files are available, run them
separately with `uv run python -m pytest bench/tests -q`.

Workflow tests use scripted model responses, including the two-submission regression,
multiple items, isolated tickets and evidence, budget context for model review, and
partial failure. They also verify that model-confirmed approvals are no longer
overridden by a deterministic business-rule check. Separate adapter tests check Foundry message translation and a real local
MCP stdio round trip with concurrent calls. Scheduling tests verify overlap, result
ordering, write boundaries and cancellation. A single-session HTTP test uses a
server-side barrier to prove that three reads overlap. Boundary tests block SDK
imports while running local policy
and data access, and import every application module with the benchmark package blocked. Ticket tests exercise concurrent threads and processes sharing SQLite,
plus 20 concurrent MCP clients over in-process Streamable HTTP (ASGI, JSON responses,
stateless mode). These protocol tests do not open network sockets. They make no paid
model calls. Historical benchmark results describe the implementation recorded
with each run and are not a substitute for current submission evidence.
