import asyncio
import json

import pytest
from anthropic.lib.tools.mcp import async_mcp_tool

from bench.cli_vs_mcp import cli_guide, command_for
from bench.live_cli_eval import parse_command
from bench.scale_tools import CORE, COUNTS, execute, make_server, parameters
from bench.tool_count_eval import cli_executor, mcp_executor


def test_nested_catalogs_preserve_core_and_all_descriptions():
    previous = []
    for count in COUNTS:
        server = make_server(count)
        defs = [async_mcp_tool(t, None).to_dict() for t in asyncio.run(server.list_tools())]
        assert len(defs) == count
        assert defs[: len(previous)] == previous
        assert [d["name"] for d in defs[:4]] == list(CORE)
        guide = cli_guide(defs, full=True, param_map=parameters(count))
        for tool in defs:
            assert tool["description"] in guide
            for prop in tool["input_schema"]["properties"].values():
                assert prop["description"] in guide
        previous = defs


@pytest.mark.parametrize("count", COUNTS)
def test_all_mock_operations_are_executable(count):
    param_map = parameters(count)
    for name in param_map:
        if name in CORE:
            continue
        args = {"record_id": 'record "quoted"'}
        assert parse_command(command_for(name, args, param_map), param_map) == (name, args)
        result = execute(count, name, args)
        assert result["record_id"] == args["record_id"]
        assert result["category"] == name.removeprefix("get_")
        assert result["status"] == "available"
    with pytest.raises(ValueError):
        execute(count, "get_item_catalog", {"item": "laptop"})


def test_real_cli_and_mcp_scale_paths_match(tmp_path, monkeypatch):
    monkeypatch.setenv("BENCH_OFFLINE", "1")
    monkeypatch.setenv("BENCH_DATE", "2026-11-23")

    async def run():
        cli = cli_executor(20, tmp_path / "cli.jsonl")
        async with mcp_executor(20, tmp_path / "mcp.jsonl") as mcp:
            assert len(mcp.defs) == 20
            for name, args in [
                ("get_employee_info", {"employee_id": "E001"}),
                ("get_office_directory", {"record_id": "x"}),
                ("get_commuter_route", {"record_id": 'y "quoted"'}),
                (
                    "flag_for_human_review",
                    {"employee_id": "E003", "request": "webcam", "reason": "not in policy"},
                ),
            ]:
                text = await cli.call(name, args)
                assert text == await mcp.call(name, args)
                assert isinstance(json.loads(text), dict)
        assert (tmp_path / "cli.jsonl").read_bytes() == (tmp_path / "mcp.jsonl").read_bytes()

    asyncio.run(run())
