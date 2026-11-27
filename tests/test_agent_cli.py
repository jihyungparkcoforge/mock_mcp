"""Exercise CLI entry points without opening model or MCP connections."""

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from equipment_mcp import agent
from equipment_mcp.models import AgentResult, ItemDecision


@pytest.fixture
def cli_handlers(monkeypatch):
    """Mock async mode handlers and .env loading; keep argument parsing and asyncio real."""
    handlers = SimpleNamespace(
        demo=create_autospec(agent.run_demos),
        interactive=create_autospec(agent.run_interactive),
        load_dotenv=create_autospec(agent.load_dotenv),
    )
    monkeypatch.setattr(agent, "run_demos", handlers.demo)
    monkeypatch.setattr(agent, "run_interactive", handlers.interactive)
    monkeypatch.setattr(agent, "load_dotenv", handlers.load_dotenv)
    return handlers


@pytest.mark.parametrize("mode", ["demo", "interactive"])
def test_main_awaits_selected_mode(cli_handlers, monkeypatch, mode):
    monkeypatch.setattr(sys, "argv", ["equipment-agent", f"--{mode}"])
    agent.main()
    getattr(cli_handlers, mode).assert_awaited_once_with()


@pytest.mark.parametrize("mode,unused", [("demo", "interactive"), ("interactive", "demo")])
def test_main_does_not_start_other_mode(cli_handlers, monkeypatch, mode, unused):
    monkeypatch.setattr(sys, "argv", ["equipment-agent", f"--{mode}"])
    agent.main()
    getattr(cli_handlers, unused).assert_not_called()


@pytest.mark.parametrize("mode", ["demo", "interactive"])
def test_main_loads_environment_before_running_mode(cli_handlers, monkeypatch, mode):
    events = []
    cli_handlers.load_dotenv.side_effect = lambda: events.append("load-env")

    async def selected_handler():
        events.append("run-mode")

    getattr(cli_handlers, mode).side_effect = selected_handler
    monkeypatch.setattr(sys, "argv", ["equipment-agent", f"--{mode}"])
    agent.main()
    assert events == ["load-env", "run-mode"]


@pytest.mark.parametrize(
    "arguments",
    [[], ["--demo", "--interactive"], ["--unknown"]],
    ids=["missing-mode", "conflicting-modes", "unknown-option"],
)
def test_main_rejects_invalid_options(cli_handlers, monkeypatch, arguments):
    monkeypatch.setattr(sys, "argv", ["equipment-agent", *arguments])
    with pytest.raises(SystemExit) as caught:
        agent.main()
    assert caught.value.code == 2


@pytest.mark.parametrize("mode", ["demo", "interactive"])
@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt], ids=["eof", "ctrl-c"])
def test_main_reports_cancellation(cli_handlers, monkeypatch, capsys, mode, interruption):
    getattr(cli_handlers, mode).side_effect = interruption
    monkeypatch.setattr(sys, "argv", ["equipment-agent", f"--{mode}"])
    agent.main()
    assert capsys.readouterr().out.strip() == "Cancelled."


@pytest.mark.parametrize("mode", ["demo", "interactive"])
def test_main_propagates_unexpected_failures(cli_handlers, monkeypatch, mode):
    getattr(cli_handlers, mode).side_effect = RuntimeError("connection failed")
    monkeypatch.setattr(sys, "argv", ["equipment-agent", f"--{mode}"])
    with pytest.raises(RuntimeError, match="connection failed"):
        agent.main()


@pytest.fixture(scope="module")
def module_help():
    """Run the actual module entry point; --help exits before loading .env or clients."""
    return subprocess.run(
        [sys.executable, "-m", "equipment_mcp.agent", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_module_help_exits_successfully(module_help):
    assert module_help.returncode == 0, module_help.stderr


@pytest.mark.parametrize("option", ["--demo", "--interactive"])
def test_module_help_describes_available_modes(module_help, option):
    assert option in module_help.stdout


def test_help_does_not_load_environment(cli_handlers, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["equipment-agent", "--help"])
    with pytest.raises(SystemExit):
        agent.main()
    cli_handlers.load_dotenv.assert_not_called()


@pytest.fixture
def interactive_agent(monkeypatch):
    """Mock only the application call and .env read; keep CLI input and output real."""
    result = AgentResult(
        request="I need a headset.",
        decisions=[ItemDecision("r1", "headset", "approve", "Headset approved.")],
    )
    run_agent = create_autospec(agent.run_agent, return_value=result)
    monkeypatch.setattr(agent, "run_agent", run_agent)
    monkeypatch.setattr(agent, "load_dotenv", create_autospec(agent.load_dotenv))
    monkeypatch.setattr(sys, "argv", ["equipment-agent", "--interactive"])
    return run_agent


@pytest.mark.parametrize(
    "answers",
    [
        [" E001 ", " I need a headset. "],
        ["", " E001 ", "   ", " I need a headset. "],
    ],
    ids=["trim-input", "retry-blank-input"],
)
def test_interactive_main_passes_cleaned_input(interactive_agent, monkeypatch, answers):
    # Supply terminal input without waiting for a person during the test.
    responses = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt: next(responses))
    agent.main()
    interactive_agent.assert_awaited_once_with("I need a headset.", "E001")


def test_interactive_main_prints_application_response(interactive_agent, monkeypatch, capsys):
    responses = iter(["E001", "I need a headset."])
    monkeypatch.setattr("builtins.input", lambda prompt: next(responses))
    agent.main()
    assert "RESPONSE : Headset approved." in capsys.readouterr().out
