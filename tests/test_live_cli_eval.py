import asyncio
import json
from datetime import date
from types import SimpleNamespace

import pytest

from bench.cli_vs_mcp import command_for
from bench.live_cli_eval import Meter, agent_loop, parse_command, score
from bench.offline_support import PARAMS
from equipment_mcp import tools


def test_cli_arguments_preserve_quoted_values():
    args = {"employee_id": "E003", "request": 'A "new" webcam; please', "reason": "정책 검토"}
    assert parse_command(command_for("flag_for_human_review", args)) == (
        "flag_for_human_review",
        args,
    )


def test_cli_help_is_not_recorded_as_business_facts(capsys):
    with pytest.raises(ValueError):
        parse_command("equipment --help")
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("item", ["webcam", "4K webcam"])
def test_out_of_policy_item_accepts_the_users_original_wording(item):
    result = score(
        2,
        {"decision": "escalate", "item": item, "ticket_id": "ESC-0001"},
        {},
        [
            {
                "operation": "check_request_eligibility",
                "arguments": {"item": item},
                "result": {"status": "not_in_policy"},
            }
        ],
        ["ESC-0001"],
    )
    assert result["success"]


def test_out_of_policy_item_does_not_accept_an_unrelated_item():
    result = score(
        2,
        {"decision": "escalate", "item": "keyboard", "ticket_id": "ESC-0001"},
        {},
        [
            {
                "operation": "check_request_eligibility",
                "arguments": {"item": "keyboard"},
                "result": {"status": "not_in_policy"},
            }
        ],
        ["ESC-0001"],
    )
    assert not result["success"]


@pytest.mark.parametrize("arm", ["mcp", "cli_full", "cli_syntax"])
def test_real_approval_gate_and_reflection_are_counted(arm):
    calls = [
        ("get_employee_info", {"employee_id": "E001"}),
        ("check_request_eligibility", {"employee_id": "E001", "item": "laptop"}),
        ("get_open_requests", {"employee_id": "E001"}),
        ("get_item_catalog", {"item": "laptop"}),
        ("get_department_budget", {"department": "Finance"}),
        ("check_inventory", {"item": "laptop"}),
    ]
    draft = {"decision": "approve", "item": "laptop", "response": "Approved.", "ticket_id": None}

    def use(name, args):
        return SimpleNamespace(type="tool_use", id=f"id_{name}", name=name, input=args)

    blocks = [
        use(name, args)
        if arm == "mcp"
        else use("run_command", {"command": command_for(name, args)})
        for name, args in calls
    ]
    responses = [
        [use("submit_decision", draft)],
        blocks,
        [use("submit_decision", draft)],
        [
            SimpleNamespace(
                type="text",
                text=json.dumps(
                    {
                        "verdict": "confirmed",
                        "issues": [],
                        "revised_response": None,
                    }
                ),
            )
        ],
    ]

    class FakeClient:
        messages = None

        async def create(self, **kwargs):
            content = responses.pop(0)
            return SimpleNamespace(
                content=content,
                model="test",
                id="test",
                stop_reason="tool_use",
                usage=SimpleNamespace(
                    model_dump=lambda **kw: {
                        "input_tokens": 17,
                        "output_tokens": 3,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    }
                ),
            )

    class FakeExecutor:
        async def call(self, name, args):
            kwargs = dict(args)
            if name in ("get_employee_info", "check_request_eligibility"):
                kwargs["today"] = date(2026, 11, 23)
            return json.dumps(getattr(tools, name)(**kwargs))

        async def run(self, command):
            name, args = parse_command(command)
            return await self.call(name, args)

    client = FakeClient()
    client.messages = client
    meter = Meter(client)
    defs = [
        {
            "name": name,
            "description": name,
            "input_schema": {
                "properties": {p: {"type": "string"} for p in params},
                "required": list(params),
            },
        }
        for name, params in PARAMS.items()
    ]
    row = {}
    asyncio.run(agent_loop(meter, "test", arm, FakeExecutor(), defs, 0, row))
    assert row["drafts"][0]["gate_problems"]  # Cannot approve before gathering evidence.
    assert not row["drafts"][1]["gate_problems"]
    assert row["success"]
    assert len(row["reflections"]) == 1
    assert len(meter.calls) == 4
    assert meter.calls[-1]["phase"] == "reflection"
    assert meter.usage()["input_tokens"] == 68
    assert meter.usage()["output_tokens"] == 12
