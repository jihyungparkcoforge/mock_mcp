# IT Equipment Request Handler — Plan

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
| Role | Monitor (max qty / refresh) | Laptop (max qty / refresh) |
|---|---|---|
| standard | 1 / 3 years | 1 / 4 years |
| manager | 2 / 2 years | 1 / 2 years |
| engineer | 2 / 3 years | 1 / 3 years |

Eligibility (`check_request_eligibility`), evaluated against `today`:
- **eligible_new** — employee holds fewer units than `max_quantity`.
- **eligible_refresh** — at `max_quantity`, and the **oldest** unit's
  `acquired + refresh_years <= today` (the due date itself counts as eligible).
- **not_eligible** — at `max_quantity` and the oldest unit is not yet due;
  `next_eligible_date` is returned.
- **not_in_policy** — the item is not listed for the employee's role.
- **employee_not_found** — unknown ID.

### 1.3 Decision rules (agent)
| Situation | Decision |
|---|---|
| `eligible_new` / `eligible_refresh` | **approve** |
| `not_eligible`, no damage/failure reason | **deny** (state `next_eligible_date`) |
| `not_in_policy` | **escalate** — reason: item not covered by policy |
| `not_eligible` **and** the request cites damage/failure (broken, cracked, not turning on, …) | **escalate** — reason: out-of-cycle replacement due to damage |

"Ambiguous" means the policy data alone cannot decide the case correctly.
The agent must call `flag_for_human_review` for these instead of guessing.

### 1.4 Tools (MCP server)
| Tool | Returns | Side effect |
|---|---|---|
| `get_employee_info(employee_id)` | role, hire date, tenure (years, 1 decimal), equipment | none |
| `get_policy_limits(role)` | items with `max_quantity`, `refresh_years` | none |
| `check_request_eligibility(employee_id, item)` | `eligible`, `status`, `reason`, counts, `next_eligible_date` | none |
| `flag_for_human_review(employee_id, request, reason)` | ticket `ESC-NNNN`, `pending_review` | appends a line to `data/escalations.jsonl` |

Lookup failures return `{"error": ...}` dicts (not exceptions) so the agent
observes them. Exact return shapes are in the docstrings of `src/equipment_mcp/tools.py`.

### 1.5 Mock data (`src/equipment_mcp/data/employees.json`, reference date 2026-11-23)
| ID | Role | Hired | Equipment | Purpose |
|---|---|---|---|---|
| E001 | standard | 2021-02-01 | laptop 2022-01-10, monitor 2025-06-01 | approve (laptop) / deny (2nd monitor) |
| E002 | manager | 2019-05-15 | laptop 2025-09-01, monitor 2024-01-15 | escalate (cracked laptop) / eligible_new monitor |
| E003 | engineer | 2026-10-01 | laptop + 2 monitors 2026-10-01 | escalate (webcam) / at-max monitors |
| E004 | engineer | 2020-08-20 | laptop 2023-11-23 | boundary: refresh due exactly today |

### 1.6 Demo requests (`src/equipment_mcp/demo_requests.py`)
1. E001 laptop replacement (slow, ~5 years) → **approve**
2. E001 second monitor → **deny**
3. E003 4K webcam → **escalate** (not in policy)
4. E002 cracked laptop screen, laptop from 2025-09 → **escalate** (damage inside cycle)

## 2. Decisions (owner)

| # | Decision | Reason |
|---|---|---|
| D1 | Three roles: standard / manager / engineer with the limits in 1.2 | Includes the assignment's examples (standard 3y monitor, manager 2y laptop) while making role differences large enough to test. |
| D2 | Escalate when the item is not in policy | No policy basis to approve or deny automatically. |
| D3 | Escalate when a replacement is inside the refresh cycle but the reason is damage/failure | Policy says deny, but an exception reason exists that needs human judgment. |
| D4 | LLM: Claude called through Microsoft Foundry (`AnthropicFoundry` client) | _owner to fill in reason_ |
| D5 | Data in JSON files; escalations appended to `escalations.jsonl` | Separates data from code, is easy to show in screenshots, and the side effect leaves a visible file. |
| D6 | Hand-written agent loop; MCP tools converted and called with the SDK helper `anthropic.lib.tools.mcp.async_mcp_tool` (not `tool_runner`) | Every Thought/Action/Observation is recorded and printed by our own code, which is the evidence for the ReAct criterion, and reflection can be inserted inside the loop. The SDK already converts MCP tools, so we do not re-implement that. |
| D7 | Final answer via a local `submit_decision` tool with a strict schema (not on the MCP server) | The SDK guarantees well-formed JSON, so no text parsing is needed, and the decision shows up in the trace as the last Action. |
| D8 | The system prompt holds only agent policy (role, status-to-decision rules, escalation principle, reply constraints). How and when to use each tool lives in the MCP tool descriptions. | MCP already tells the model how to use its tools; repeating it in the prompt couples the agent to the server and goes stale when a tool changes. |
| D9 | Transport chosen by `MCP_SERVER_URL`: Streamable HTTP when set, stdio when empty (`equipment_mcp/transport.py`) | _owner to fill in reason_ |

## 3. Owner to confirm (chosen while scaffolding, not yet confirmed)

- **C1** Lookup errors are returned as `{"error": ...}` dicts rather than raised,
  so the agent sees them as Observations. (`flag_for_human_review` also rejects
  blank `request`/`reason` with an error dict and writes nothing.)
- **C2** IDs are normalized (`" e001 "` → `E001`); roles/items lower-cased and trimmed.
  Mapping free text ("second monitor", "notebook") to an item name is the agent's job, not the tool's.
- **C3** Damage detection (D3) is done by the agent from the request text; the
  eligibility tool stays purely data-driven.
- **C4** Unknown employee IDs: the agent replies asking the employee to verify
  the ID (no approve/deny). Not one of the chosen escalation triggers.
- **C5** `flag_for_human_review` does not validate that the employee exists.
- **C6** Tenure = `round(days / 365.25, 1)`. `today`/`now`/`log_path` are
  function parameters for deterministic tests and are **not** exposed as MCP tool arguments.
- **C7** `validate_decision` checks the submitted decision against the run:
  `escalate` needs a ticket that `flag_for_human_review` actually returned in this run;
  `approve`/`deny` must not carry a ticket; `response` must not be blank. Problems are
  sent back to the model as an error tool_result so it can correct itself (no crash, no
  silent acceptance). Superseded: parsing a ```json text block (`parse_final_decision`).
- **C8** Reflection runs inside the loop, right after `submit_decision` passes
  `validate_decision`, and reviews the draft against the Observations.
  `confirmed` sends it as is; `revised` replaces only the reply text; `rejected`
  sends the issues back to the agent to re-investigate and resubmit. After
  `MAX_REFLECTION_REJECTIONS` (2) rejections the request is escalated automatically.
  An `escalate` decision is never rejected, because its ticket is already recorded.
- **C9** Model `claude-opus-5-5` (Foundry deployment name via `FOUNDRY_MODEL`).
  Opus 5.5 returns 400 on forced `tool_choice` (`any`/`tool`), so use `auto`.
  Its between-tool text arrives as `thinking` blocks that are empty by default —
  to show **Thought** lines in the trace, either request `thinking.display: "summarized"`
  or instruct the model to write a short text "Thought" before each tool call. Pick one.
- **C10** MCP Python SDK **2.x** (`mcp>=2.3`): `FastMCP` is now
  `mcp.server.mcpserver.MCPServer`; tool schemas use `input_schema` (snake_case).
  Tutorials written for 1.x will not match.
- **C11** `check_request_eligibility` returns `status: "role_not_found"` (eligible None)
  when the employee's role has no policy entry. Treated as a data problem, kept
  separate from `not_in_policy`.
- **C12** `strict: true` on `submit_decision` must be confirmed to work on the Foundry
  deployment before relying on it.
## 4. Layout

```
pyproject.toml                 uv project (mcp, anthropic; dev: pytest)
src/equipment_mcp/
  tools.py                     plain functions (business logic)        <- TODO
  server.py                    MCPServer wrappers, stdio               <- TODO
  agent.py                     ReAct loop, reflection, Foundry client  <- TODO
  demo_requests.py             4 demo requests + expected decisions
  data/employees.json, policies.json
scripts/smoke_client.py        one-tool end-to-end client check        <- TODO
tests/test_tools.py            unit tests per tool (normal + edge)
tests/test_server.py           tool registration + calls via MCPServer in-process
tests/test_agent_helpers.py    pure agent helpers (no LLM)
```

## 5. Build order (TDD)

Run tests with `uv run pytest -q`. Every test starts red.

1. [ ] **Hello server** — throwaway one-tool server (e.g. `ping`) + client; screenshot. *(deliverable 2)*
2. [ ] `load_*`, `normalize_*`, `get_employee_info` → `TestGetEmployeeInfo` green.
3. [ ] Wrap `get_employee_info` in `server.py`; implement `scripts/smoke_client.py`;
       run `uv run python scripts/smoke_client.py E001`; screenshot. *(deliverable 2)*
4. [ ] `get_policy_limits` → green → wrap → smoke.
5. [ ] `check_request_eligibility` → green → wrap → smoke.
6. [ ] `flag_for_human_review` → green → wrap → smoke. `tests/test_server.py` all green.
7. [ ] Agent helpers (`format_trace_step`, `validate_decision`) → `test_agent_helpers.py` green.
8. [ ] `make_llm_client` + `run_agent` with a single hard-coded tool call; confirm a response.
9. [ ] Full ReAct loop (model picks tools until it emits the final json block).
10. [ ] `reflect` — capture at least one `confirmed` and ideally one `revised`. *(deliverable 8)*
11. [ ] Run the 4 demo requests; save full traces + decisions. *(deliverables 6, 7)*
12. [ ] CI: GitHub Actions running `uv sync` + `uv run pytest -q` (no API key needed). *(deliverable 9)*

## 6. Run

```bash
uv sync
uv run pytest -q
uv run python -m equipment_mcp.server            # stdio, or HTTP if MCP_SERVER_URL is set
uv run python scripts/smoke_client.py E001
export ANTHROPIC_FOUNDRY_API_KEY=... ANTHROPIC_FOUNDRY_RESOURCE=...
uv run python -m equipment_mcp.agent
```
