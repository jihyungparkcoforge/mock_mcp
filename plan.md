# IT Equipment Request Handler — Requirements and Verification

MCP server exposing a mock IT equipment request system, plus a ReAct agent
(Claude via Microsoft Foundry) that approves, denies, or escalates requests.

## 1. Requirements

### 1.1 Request data
| Field | Source | Example |
|---|---|---|
| `employee_id` | given with the request | `E001` |
| `request` (free text) | employee | "My laptop is 5 years old and slow" |
| `item` | extracted by the agent from the text | `laptop`, `monitor`, `webcam` |
| `reason` | extracted by the agent from the text | "slow", "screen cracked" |
| `role`, `hire_date`, `equipment` | `get_employee_info` | `standard`, `2021-02-01`, `[{item, acquired}]` |

### 1.2 Policy rules (`src/equipment_mcp/data/policies.json`)
| Role | Monitor | Laptop | Headset | Workstation |
|---|---|---|---|---|
| standard | 1 / 3y | 1 / 4y | 1 / 2y | not allowed |
| manager | 2 / 2y | 1 / 2y | 1 / 2y | not allowed |
| engineer | 2 / 3y | 1 / 3y | 1 / 2y | 1 / 4y |

(max quantity / refresh cycle)

Eligibility (`check_request_eligibility`), evaluated against `today`:
- **eligible_new**: employee holds fewer units than `max_quantity`.
- **eligible_refresh**: at `max_quantity`, and the **oldest** unit's
  `acquired + refresh_years <= today` (the due date itself counts as eligible).
- **not_eligible**: at `max_quantity` and the oldest unit is not yet due;
  `next_eligible_date` is returned.
- **not_in_policy**: the item is not listed for the employee's role.
- **employee_not_found** / **role_not_found**: missing data.

Other data:
- Catalog (`catalog.json`): laptop $1,400, monitor $300, headset $120,
  workstation $3,200; auto-approve limit $2,000.
- Department budgets (`budgets.json`): remaining = annual_budget - spent.
- Inventory (`inventory.json`): headset is out of stock (restock 2026-12-15).
- Open requests (`open_requests.json`): E002 has ORD-1042 (monitor, awaiting delivery).

### 1.3 Decision rules (`DECISION_RULES` in `policy.py`)
Approve only when **all** hold:
1. eligibility status is `eligible_new` or `eligible_refresh`
2. `employment_status` is `active`
3. no open request for the same item
4. price is within the auto-approve limit
5. department remaining budget, minus earlier approvals in this invocation, covers the price
6. item is in stock

| Condition | Decision |
|---|---|
| `not_eligible`, no damage/failure reason | **deny** (give `next_eligible_date`) |
| employee is `leaving` | **deny** |
| open request for the same item | **deny** (give its request ID) |
| `employee_not_found` | **deny** (ask to confirm the ID) |
| `not_eligible` + damage/failure reason | **escalate** |
| item not in policy or not in catalog | **escalate** |
| price above auto-approve limit | **escalate** |
| remaining budget below price | **escalate** |
| out of stock | **escalate** |
| employee `on_leave` | **escalate** |
| `role_not_found`, missing data | **escalate** |

If both a deny reason and an escalate reason apply, **deny**.
"Ambiguous" means the data alone cannot decide; the agent escalates instead of guessing.

Approval guard (code, not the model): an `approve` is accepted only when the run's
tool results prove all six conditions for the submitted item (`approval_gaps`).
`deny` and `escalate` do not require the six approval checks, so the agent can stop
early. All decisions still require valid fields, the current request ID and item,
and reflection. An escalation must reference a ticket created for that item.
An unknown employee ID produces `deny` with a request to verify the ID; it does not
create a ticket automatically.

### 1.4 Tools (MCP server)
| Tool | Returns | Side effect |
|---|---|---|
| `get_employee_info(employee_id)` | role, department, employment_status, hire date, tenure, equipment | none |
| `get_policy_limits(role)` | items with `max_quantity`, `refresh_years` | none |
| `check_request_eligibility(employee_id, item)` | `eligible`, `status`, `reason`, counts, `next_eligible_date` | none |
| `get_item_catalog(item)` | price, currency, auto_approve_limit | none |
| `get_department_budget(department)` | annual_budget, spent, remaining | none |
| `check_inventory(item)` | in_stock, restock_date | none |
| `get_open_requests(employee_id)` | open requests (id, item, status, created) | none |
| `flag_for_human_review(employee_id, request, reason)` | ticket `ESC-NNNN`, `pending_review` | appends a line to `data/escalations.jsonl` |

The four required tools are employee lookup, policy lookup, eligibility, and human
review. Catalog, budget, inventory, and open-request lookups extend the decision
checks. Lookup tools return facts, and the agent combines them. Unknown lookup keys
return error dictionaries or eligibility status records. Invalid JSON, broken
connections, and malformed data are technical errors, not business decisions.

### 1.5 Mock data (`employees.json`, reference date 2026-11-23)
| ID | Role | Dept | Status | Equipment | Scenario |
|---|---|---|---|---|---|
| E001 | standard | Finance | active | laptop 2022-01-10, monitor 2025-06-01 | approve laptop / deny 2nd monitor |
| E002 | manager | Sales | active | laptop 2025-09-01, monitor 2024-01-15 | escalate cracked laptop / deny monitor (open ORD-1042) |
| E003 | engineer | Engineering | active | laptop + 2 monitors 2026-10-01 | escalate webcam (not in policy) |
| E004 | engineer | Engineering | active | laptop 2023-11-23 | escalate headset (no stock) / workstation (over limit) |
| E005 | standard | Operations | active | laptop 2024-05-01 | escalate monitor (budget 150 < 300) |
| E006 | engineer | Engineering | on_leave | laptop 2022-06-01 | escalate laptop (on leave) |
| E007 | standard | Sales | leaving | laptop 2021-09-01 | deny laptop (leaving) |

### 1.6 Demo requests (`src/equipment_mcp/demo_requests.py`)
| # | Request | Expected | Deciding condition |
|---|---|---|---|
| 1 | E001 laptop replacement | approve | all six checks pass |
| 2 | E001 second monitor | deny | not_eligible |
| 3 | E003 4K webcam | escalate | not in policy |
| 4 | E002 cracked laptop | escalate | damage inside refresh cycle |
| 5 | E002 another monitor | deny | open request ORD-1042 |
| 6 | E007 new laptop | deny | employee leaving |
| 7 | E005 first monitor | escalate | budget below price |
| 8 | E004 headset | escalate | out of stock |
| 9 | E004 workstation | escalate | price above limit |
| 10 | E006 laptop refresh | escalate | employee on leave |

## 2. Request handling and boundaries

- One structured model call extracts new requests, one per item category. Existing
  orders and background mentions are excluded. Excerpts must occur verbatim in the
  original message; duplicate categories or IDs are rejected.
- Requests receive IDs (`r1`, `r2`, ...) and run sequentially. Each has separate
  observations, review retries, ticket state, and final decision. Repeated mentions
  of one category are consolidated; multiple quantities are not supported.
- `submit_decision` is a local model tool, not an MCP server tool. Its schema and
  runtime validation bind it to the current item and request ID. Later calls in the
  same model response are ignored once a decision is accepted.
- Earlier approvals reserve budget for later items in the same invocation. This
  mock records decisions; it does not place orders or persist budget spending.
- Ticket creation preserves the original message and prefixes the reason with the
  current request ID and item. Repeated calls for that item reuse its ticket.
- Employee IDs are trimmed and uppercased; role and item keys are trimmed and
  lowercased. Free-text category mapping and damage interpretation belong to the
  model. Eligibility itself uses only recorded facts and the evaluation date.
- `flag_for_human_review` rejects blank requests or reasons without writing a ticket.
  It does not require the employee ID to exist. JSONL ticket IDs are sequential;
  concurrent writers and cross-invocation retry idempotency are not implemented.
- Dates and repositories are injectable. They are not exposed as MCP arguments.
  Tenure is `round(days / 365.25, 1)`; leap-day anniversaries use February 28 when needed.
- `AgentResult.decisions` contains all item results. Single-item compatibility
  accessors reject multi-item results. A later processing failure preserves completed
  results in `IncompleteRequestsError.result` and identifies pending request IDs.

Limitations: an empty or invalid extraction raises an error before the item loop.
A request whose item cannot be identified is not guaranteed an automatic human-review
fallback. A model or transport error also must not be presented as an approval,
denial, or successful escalation. The step limit applies independently to each item.

## 3. ReAct and reflection

### 3.1 ReAct

`EquipmentWorkflow` repeats this sequence for each item:

1. Ask the model for the next action using the current context and prior results.
2. Record returned thought summaries or explanatory text as `Thought` entries.
3. Execute selected tools and record `Action` entries.
4. Return tool results or errors as `Observation` entries for the next model turn.
5. Validate and review a submitted decision; retry when rejected, or finalize it.

The model chooses which tools to call. It can request several in one response.
This is the reasoning/action/observation loop required by the rubric; it is not a
fixed sequence of lookups followed by a single answer. Thought entries contain
provider-returned summaries/text, not the model's private full reasoning.

`foundry.py` translates application values to Foundry messages and preserves opaque
provider conversation blocks. `mcp_executor.py` discovers and calls MCP tools.
`workflow.py` knows neither SDK. `app.py` owns client and connection lifetimes.

### 3.2 Reflection

After a valid draft passes the local guards, a separate model call compares the
proposed decision and reply with the tool observations and decision rules:

| Verdict | Application behavior |
|---|---|
| `confirmed` | Accept the draft unchanged. |
| `revised` | Keep the decision and ticket; replace the reply text. |
| `rejected` | Feed issues back to the item loop for investigation and resubmission. |

Two rejection retries are allowed (`MAX_REFLECTION_REJECTIONS = 2`). A third rejection
forces human review. An already-ticketed escalation is retained even if the reviewer
returns `rejected`; its wording can still be revised. The application checks ticket
existence and scope independently of the reviewer.

The rubric accepts either a demonstrated confirmation or an improvement. It does
not require reflection to change a decision or increase aggregate accuracy.

Recorded real-model examples from the earlier Haiku MCP benchmark:

- **A08, 20 tools:** headset approval passed all six checks. Reflection returned
  `confirmed`; the final reply matched the draft.
- **D04, 12 tools:** laptop replacement was requested one day before eligibility.
  The draft denied it and gave 2026-11-24 as the eligible date. Reflection confirmed it.
- **A09, 12 tools:** the headset approval was correct, but its reply asserted that
  no new monitor request had been made. Reflection identified this as unsupported
  by the observations and revised the reply to report only the existing pending
  order and that the headset approval did not change it. The final reply used that
  revision, preserving the headset approval.

Source: `bench/results/haiku_full/runs.jsonl` (local, intentionally Git-ignored).
These are recorded runs of the earlier agent, not fresh runs of the refactored
workflow. Current tests independently cover confirmation, reply revision, rejection
retries, and forced escalation without a paid model call.

## 4. Code layout

```text
src/equipment_mcp/
  agent.py          compatible CLI/import entry point; loads .env for CLI use
  app.py            settings, client/session lifetime, workflow assembly, demo main
  workflow.py       per-item ReAct loop, evidence, retries, budget reservations
  models.py         application dataclasses and errors
  ports.py          model, tool, data and ticket interfaces
  policy.py         decision rules, validation, approval guard, review outcomes
  prompts.py        Foundry prompts and structured-output schemas
  foundry.py        model adapter and provider conversation history
  mcp_executor.py   MCP discovery, calls and result translation
  transport.py      stdio or Streamable HTTP connection selection
  server.py         MCP schemas and create_server(data, tickets)
  equipment.py      standalone business functions with explicit dependencies
  repositories.py   JSON and in-memory data/ticket implementations
  tools.py          default JSON wiring and compatible public function API
  formatting.py     trace and result formatting
  demo_requests.py  10 requests and expected decisions
  data/             employees, policies, catalog, budgets, inventory, open requests
scripts/smoke_client.py       MCP connection and employee lookup check
tests/                       tool, policy, workflow, adapter and boundary tests
.github/workflows/ci.yml     dependency installation, Ruff and pytest
```

`EquipmentWorkflow(model, tools)` only stores dependencies. Business operations such
as `get_item_catalog(item, data=data)` can run without Foundry, MCP, credentials, or
constructor bypasses. The server binds dependencies without exposing them to the model.

## 5. Verification and submission evidence

### 5.1 Build history and minimal connection

| Commit | Evidence in the source |
|---|---|
| `e7335a0` | Initial test/code scaffolding; smoke client is a TODO. |
| `12f9082` | Employee lookup implemented; the other three server tools remain stubs. Smoke client is still a TODO. |
| `a37bf01` | Remaining core tools and the working employee-lookup smoke client are implemented. |
| `1a68293` | Foundry agent integration follows the tool/client work. |

This demonstrates incremental implementation and a completed smoke client before
agent integration. Commit contents alone do not prove the date of a successful
connection run, or a successful client run before the other tools were implemented.
For rubric evidence, show the relevant commit/diff and successful client output.
Label a replay of an earlier revision as a reproduction, not a historical run log.
The current local stdio integration test verifies discovery and a real tool call.

### 5.2 Approval, denial and escalation evidence

The previous real-model benchmark completed 90 MCP runs: 30 scenarios at each of
8, 12 and 20 tools, using `claude-haiku-5-5`.

| Expected decision | Correct final decisions | Notes |
|---|---:|---|
| approve | 29/30 | A09 with 8 tools suffered the former duplicate-submission overwrite bug. |
| deny | 30/30 | All recorded final decision labels matched expectations. |
| escalate | 30/30 | Correct decision labels; separate exact request-text checks failed in three runs. |

Decision-label correctness was 89/90; full-case success was 86/90. These metrics
are distinct. The duplicate-submission bug was fixed by per-item scope and stopping
execution after acceptance; a scripted regression verifies that behavior. The old
benchmark has not been rerun against the new workflow, so neither a new accuracy
percentage nor measured improvement is claimed.

Source: local `bench/results/haiku_full/summary.json` and `runs.jsonl`. Benchmark
fixtures may override data per scenario; their results are not the ten default-data
demo requests listed in section 1.6. Benchmark artifacts remain outside the submission
repository unless included separately by the owner.

### 5.3 Current automated checks

Latest local verification: **128 submission tests passed** and `ruff check .` passed
in a temporary copy containing only the remaining Git-tracked files, without `bench/`.
The separate local benchmark suite has **14 passing tests**.

- Tool tests cover ordinary lookups, unknown keys, normalization, refresh boundaries,
  empty ticket fields, sequential IDs, and persisted ticket contents.
- Workflow tests use scripted model responses to exercise approval evidence, denial
  finalization, multiple items, duplicate submissions, isolated tickets and facts,
  budget reservations, rejected drafts and partial failures. They test application
  behavior, not a real model's decision accuracy.
- Adapter tests check Foundry message translation and real local MCP stdio discovery
  and calls. Core-boundary tests block external SDK imports.
- CI installs with `uv sync --locked`, runs `ruff check .`, then `pytest -v`.
  A passing remote run for the current working tree has not been verified.
- Benchmark tests `test_live_cli_eval.py` and `test_tool_count_eval.py` have been moved
  from `tests/` to ignored `bench/tests/`. Commit their removal from the tracked test
  directory so CI no longer depends on the local benchmark package. Run them locally
  with `uv run python -m pytest bench/tests -q` when benchmark files are available.

## 6. Run

```bash
uv sync
uv run pytest -q
uv run ruff check .
uv run python -m equipment_mcp.server
uv run python scripts/smoke_client.py E001
uv run python -m equipment_mcp.agent
```

Configure Foundry credentials through the environment or local `.env` before running
an agent demo. `FOUNDRY_MODEL` selects the deployment; the code default is
`claude-opus-5-5`, and the recorded benchmark used `claude-haiku-5-5`.
`MCP_SERVER_URL` selects Streamable HTTP when set; otherwise the client starts a local
stdio server. The child is explicitly passed `--stdio`. The CLI entry point is
`agent.py`, which loads `.env` and calls `app.main()`; importing `app.py` starts nothing.
