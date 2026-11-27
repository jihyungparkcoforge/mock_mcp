"""Test application wiring and resource ownership without external connections."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import ANY, create_autospec

import anthropic
import mcp
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.types import ListToolsResult, Tool

from equipment_mcp import app
from equipment_mcp.foundry import FoundryAdapter
from equipment_mcp.models import AgentResult, ItemDecision
from equipment_mcp.prompts import MAX_TOKENS


@pytest.mark.parametrize("model", [None, "", "   "], ids=["missing", "empty", "blank"])
def test_settings_reject_missing_model(monkeypatch, model):
    if model is None:
        monkeypatch.delenv("FOUNDRY_MODEL", raising=False)
    else:
        monkeypatch.setenv("FOUNDRY_MODEL", model)
    with pytest.raises(ValueError, match="Set FOUNDRY_MODEL"):
        app.Settings.from_environment()


def test_settings_trim_model_name(monkeypatch):
    monkeypatch.setenv("FOUNDRY_MODEL", "  test-model  ")
    assert app.Settings.from_environment().model == "test-model"


def test_settings_use_default_token_limit(monkeypatch):
    monkeypatch.setenv("FOUNDRY_MODEL", "test-model")
    assert app.Settings.from_environment().max_tokens == MAX_TOKENS


@pytest.fixture
def sdk_clients(monkeypatch):
    """Mock SDK construction and I/O; keep real discovery, adapters, and context managers."""
    monkeypatch.setenv("FOUNDRY_MODEL", "environment-model")
    monkeypatch.delenv("MCP_SERVER_URL", raising=False)
    llm_factory = create_autospec(anthropic.AsyncAnthropicFoundry)
    mcp_factory = create_autospec(mcp.Client)
    llm_client = llm_factory.return_value
    mcp_client = mcp_factory.return_value
    session = create_autospec(ClientSession, instance=True, spec_set=True)
    session.list_tools.return_value = ListToolsResult(
        tools=[Tool(name="lookup", description="Lookup", input_schema={"type": "object"})]
    )
    mcp_client.session = session
    events = []

    # Record actual context entry/exit so partial startup and failure cleanup are visible.
    async def enter_llm():
        events.append("llm-enter")
        return llm_client

    async def enter_mcp():
        events.append("mcp-enter")
        return mcp_client

    async def exit_llm(exc_type, exc, traceback):
        events.append("llm-exit")
        return False

    async def exit_mcp(exc_type, exc, traceback):
        events.append("mcp-exit")
        return False

    llm_client.__aenter__.side_effect = enter_llm
    llm_client.__aexit__.side_effect = exit_llm
    mcp_client.__aenter__.side_effect = enter_mcp
    mcp_client.__aexit__.side_effect = exit_mcp
    monkeypatch.setattr(anthropic, "AsyncAnthropicFoundry", llm_factory)
    monkeypatch.setattr(mcp, "Client", mcp_factory)
    return SimpleNamespace(
        llm_factory=llm_factory,
        mcp_factory=mcp_factory,
        llm=llm_client,
        mcp=mcp_client,
        session=session,
        events=events,
    )


async def open_and_close_workflow(settings: app.Settings | None = None):
    """Return the composed workflow for inspection after its resources have closed."""
    async with app.open_workflow(settings) as workflow:
        return workflow


@pytest.mark.parametrize(
    "settings,expected",
    [
        (None, ("environment-model", MAX_TOKENS)),
        (app.Settings("explicit-model", max_tokens=123), ("explicit-model", 123)),
    ],
    ids=["environment-settings", "explicit-settings"],
)
def test_open_workflow_configures_foundry_adapter(sdk_clients, settings, expected):
    workflow = asyncio.run(open_and_close_workflow(settings))
    assert isinstance(workflow.llm, FoundryAdapter)
    assert (workflow.llm.deployment_name, workflow.llm.max_tokens) == expected


def test_explicit_settings_work_without_environment_model(sdk_clients, monkeypatch):
    monkeypatch.delenv("FOUNDRY_MODEL")
    workflow = asyncio.run(open_and_close_workflow(app.Settings("explicit-model")))
    assert isinstance(workflow.llm, FoundryAdapter)
    assert workflow.llm.deployment_name == "explicit-model"


def test_open_workflow_uses_entered_llm_client(sdk_clients):
    workflow = asyncio.run(open_and_close_workflow())
    assert isinstance(workflow.llm, FoundryAdapter)
    assert workflow.llm.client is sdk_clients.llm


def test_open_workflow_exposes_discovered_mcp_tools(sdk_clients):
    workflow = asyncio.run(open_and_close_workflow())
    assert [tool["name"] for tool in workflow.tools.definitions] == ["lookup"]


def test_open_workflow_selects_http_transport(sdk_clients, monkeypatch):
    url = "http://127.0.0.1:8000/mcp"
    monkeypatch.setenv("MCP_SERVER_URL", url)
    asyncio.run(open_and_close_workflow())
    sdk_clients.mcp_factory.assert_called_once_with(url, mode="legacy")


@pytest.mark.parametrize("url", [None, ""], ids=["missing-url", "empty-url"])
def test_open_workflow_defaults_to_stdio(sdk_clients, monkeypatch, url):
    if url is not None:
        monkeypatch.setenv("MCP_SERVER_URL", url)
    asyncio.run(open_and_close_workflow())
    sdk_clients.mcp_factory.assert_called_once_with(
        StdioServerParameters(
            command=sys.executable, args=["-m", "equipment_mcp.server", "--stdio"]
        ),
        mode="legacy",
    )


def test_invalid_settings_fail_before_client_construction(sdk_clients, monkeypatch):
    monkeypatch.delenv("FOUNDRY_MODEL")
    with pytest.raises(ValueError, match="Set FOUNDRY_MODEL"):
        asyncio.run(open_and_close_workflow())
    sdk_clients.llm_factory.assert_not_called()
    sdk_clients.mcp_factory.assert_not_called()


def test_open_workflow_keeps_clients_open_until_context_exit(sdk_clients):
    async def run():
        async with app.open_workflow():
            sdk_clients.events.append("workflow-body")

    asyncio.run(run())
    assert sdk_clients.events == [
        "llm-enter",
        "mcp-enter",
        "workflow-body",
        "mcp-exit",
        "llm-exit",
    ]


def test_open_workflow_closes_clients_when_body_fails(sdk_clients):
    async def run():
        async with app.open_workflow():
            raise RuntimeError("workflow failed")

    with pytest.raises(RuntimeError, match="workflow failed"):
        asyncio.run(run())
    assert sdk_clients.events == ["llm-enter", "mcp-enter", "mcp-exit", "llm-exit"]


@pytest.mark.parametrize("stage", ["constructor", "enter"])
def test_mcp_startup_failure_closes_already_open_llm(sdk_clients, stage):
    failing_call = sdk_clients.mcp_factory if stage == "constructor" else sdk_clients.mcp.__aenter__
    failing_call.side_effect = ConnectionError("MCP unavailable")
    with pytest.raises(ConnectionError, match="MCP unavailable"):
        asyncio.run(open_and_close_workflow())
    assert sdk_clients.events == ["llm-enter", "llm-exit"]


def test_llm_startup_failure_does_not_construct_mcp(sdk_clients):
    sdk_clients.llm.__aenter__.side_effect = ConnectionError("LLM unavailable")
    with pytest.raises(ConnectionError, match="LLM unavailable"):
        asyncio.run(open_and_close_workflow())
    sdk_clients.mcp_factory.assert_not_called()


def test_discovery_failure_closes_both_clients(sdk_clients):
    sdk_clients.session.list_tools.side_effect = ConnectionError("discovery failed")
    with pytest.raises(ConnectionError, match="discovery failed"):
        asyncio.run(open_and_close_workflow())
    assert sdk_clients.events == ["llm-enter", "mcp-enter", "mcp-exit", "llm-exit"]


@pytest.fixture
def workflow_run(monkeypatch):
    """Mock decision execution only; run_agent still opens and closes the real composition."""
    result = AgentResult(
        request="I need a headset.",
        decisions=[ItemDecision("r1", "headset", "approve", "Approved.")],
    )
    run = create_autospec(app.EquipmentWorkflow.run, return_value=result)
    monkeypatch.setattr(app.EquipmentWorkflow, "run", run)
    return run


def test_run_agent_forwards_request_with_defaults(sdk_clients, workflow_run):
    asyncio.run(app.run_agent(" I need a headset. ", " e001 "))
    workflow_run.assert_awaited_once_with(
        ANY, " I need a headset. ", " e001 ", 14, submission_id=None
    )


def test_run_agent_forwards_explicit_limit_and_submission_id(sdk_clients, workflow_run):
    asyncio.run(app.run_agent("I need a headset.", "E001", 3, submission_id="retry-1"))
    workflow_run.assert_awaited_once_with(
        ANY, "I need a headset.", "E001", 3, submission_id="retry-1"
    )


def test_run_agent_returns_workflow_result(sdk_clients, workflow_run):
    result = asyncio.run(app.run_agent("I need a headset.", "E001"))
    assert result is workflow_run.return_value


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError], ids=["error", "cancel"])
def test_run_agent_closes_clients_when_execution_stops(sdk_clients, workflow_run, failure):
    workflow_run.side_effect = failure("execution stopped")
    with pytest.raises(failure, match="execution stopped"):
        asyncio.run(app.run_agent("I need a headset.", "E001"))
    assert sdk_clients.events == ["llm-enter", "mcp-enter", "mcp-exit", "llm-exit"]
