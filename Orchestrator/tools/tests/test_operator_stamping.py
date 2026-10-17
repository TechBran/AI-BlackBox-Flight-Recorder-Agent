"""A model-supplied ``operator`` never overrides the session operator.

Audit 2026-10-09 O18-toolvault-modules-06 (rank 33): every Google Workspace and
Gmail executor prefers ``params['operator']`` over ``ctx.operator``, and the
in-box chat / realtime-voice loops pass the model's arguments verbatim, so a
session running as operator A could read, send or delete as operator B simply
by naming B. ``BlackBoxToolExecutor.execute`` is the single dispatcher every
in-box surface goes through, so it stamps the session operator there.

Hermetic: the registry is pointed at a tmp tools dir with a throwaway module
that echoes the operator it was handed, exactly as the real executors read it.
"""

import asyncio
import json

import pytest

from Orchestrator.toolvault import registry
from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor


# Mirrors the real Google Workspace executors' resolution line
# (ToolVault/tools/create_doc/executor.py and siblings).
_ECHO_OPERATOR = (
    "from Orchestrator.toolvault.context import ToolResult\n"
    "async def execute(params, ctx):\n"
    "    operator = params.get('operator') or ctx.operator or 'system'\n"
    "    return ToolResult(True, f\"acting-as:{operator}\")\n"
)


@pytest.fixture
def echo_tool(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    folder = tools / "echo_operator"
    folder.mkdir(parents=True)
    (folder / "schema.json").write_text(json.dumps({
        "name": "echo_operator",
        "description": "Echo the operator the executor acts as.",
        "category": "communication",
        "groups": ["chat"],
        "tier": 2,
        "parameters": {
            "type": "object",
            "properties": {"operator": {"type": "string", "description": "Operator"}},
            "required": [],
        },
    }))
    (folder / "executor.py").write_text(_ECHO_OPERATOR)
    monkeypatch.setattr(registry, "TOOLS_DIR", tools)
    registry.invalidate_cache()
    yield "echo_operator"
    registry.invalidate_cache()


def _run(executor, tool, params):
    return asyncio.run(executor.execute(tool, params))


def test_model_supplied_operator_is_overridden(echo_tool):
    res = _run(BlackBoxToolExecutor(operator="alice"), echo_tool, {"operator": "SomeoneElse"})
    assert res.success
    assert res.result == "acting-as:alice"


def test_blank_or_missing_operator_resolves_to_the_session(echo_tool):
    ex = BlackBoxToolExecutor(operator="alice")
    assert _run(ex, echo_tool, {}).result == "acting-as:alice"
    assert _run(ex, echo_tool, {"operator": ""}).result == "acting-as:alice"


def test_overseer_operator_is_stamped_too(echo_tool):
    """The Flight Recorder's read-all capability rides ctx.operator through
    config.reads_all_operators, never a model argument, so it gets no
    act-as-anyone escape hatch here."""
    res = _run(BlackBoxToolExecutor(operator="Flight Recorder"), echo_tool, {"operator": "alice"})
    assert res.result == "acting-as:Flight Recorder"


def test_system_executor_keeps_honouring_an_explicit_operator(echo_tool):
    """'system' (and an unset operator) is the trusted internal scope that has
    no session operator to protect; behaviour there is unchanged."""
    assert _run(BlackBoxToolExecutor(), echo_tool, {"operator": "bob"}).result == "acting-as:bob"
    assert _run(BlackBoxToolExecutor(operator=""), echo_tool, {"operator": "bob"}).result == "acting-as:bob"


def test_callers_arguments_are_not_mutated(echo_tool):
    args = {"operator": "SomeoneElse"}
    _run(BlackBoxToolExecutor(operator="alice"), echo_tool, args)
    assert args == {"operator": "SomeoneElse"}
