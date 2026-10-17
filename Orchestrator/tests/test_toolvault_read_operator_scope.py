"""toolvault(action='read') never hands a session another operator's name.

Audit 2026-10-09 O20-mcp-server-15: the MCP tools/list enum was narrowed to the
bound operator, but the toolvault meta-tool is in the mcp group and is proxied
to /local/tools/execute, and its 'read' action resolved the x-source
"operators" enum to the whole roster -- one call gave any remote caller every
operator name on the box. The same read in an in-box session advertised
operators the dispatcher now refuses to act as (O18-toolvault-modules-06).

The resolver is the choke point: resolved for a real session operator it offers
only that operator; the "system" context (static tool arrays, the MCP stdio
catalog) still gets the live roster. Hermetic: the roster is monkeypatched and
the real search_snapshots module schema is read.
"""

import asyncio

import pytest

from Orchestrator.toolvault import resolvers
from Orchestrator.toolvault.context import ToolContext
from Orchestrator.tools.blackbox_tools import execute_tool

ROSTER = ["alice", "bob", "carol-secret"]


@pytest.fixture(autouse=True)
def roster(monkeypatch):
    monkeypatch.setattr(resolvers, "_list_operators", lambda ctx: list(ROSTER))


def _read(operator, caller=None, tool_name="search_snapshots"):
    # Exactly what /local/tools/execute runs for the MCP gateway's proxy call.
    params = {"action": "read", "tool_name": tool_name, "operator": operator}
    return asyncio.run(execute_tool("toolvault", params, operator, caller=caller))


@pytest.mark.parametrize("caller", ["mcp-gateway", None, "local"])
def test_read_for_a_real_operator_names_only_that_operator(caller):
    res = _read("alice", caller=caller)
    assert res.success, res.result
    assert res.data["schema"]["properties"]["operator"]["enum"] == ["alice"]
    leaked = [n for n in ("bob", "carol-secret") if n in res.result]
    assert not leaked, f"toolvault read leaked the roster: {leaked}"


def test_read_for_system_keeps_the_live_roster():
    res = _read("system", caller="mcp-gateway")
    assert res.success, res.result
    assert res.data["schema"]["properties"]["operator"]["enum"] == ROSTER


def test_a_model_supplied_ctx_param_cannot_reach_the_resolver():
    # The meta-tool takes its context from the dispatcher, never from arguments.
    params = {"action": "read", "tool_name": "search_snapshots", "ctx": {"operator": "system"}}
    res = asyncio.run(execute_tool("toolvault", params, "alice", caller="mcp-gateway"))
    assert res.success, res.result
    assert res.data["schema"]["properties"]["operator"]["enum"] == ["alice"]


def test_resolver_scopes_by_context():
    schema = {"name": "t", "parameters": {"type": "object", "properties": {
        "operator": {"type": "string", "x-source": "operators"}}}}

    def enum(ctx):
        return resolvers.resolve_schema(schema, ctx)["parameters"]["properties"]["operator"]["enum"]

    assert enum(None) == ROSTER
    assert enum(ToolContext()) == ROSTER
    assert enum(ToolContext(operator="system")) == ROSTER
    assert enum(ToolContext(operator="")) == ROSTER
    assert enum(ToolContext(operator="alice")) == ["alice"]
    assert enum(ToolContext(operator="Flight Recorder")) == ["Flight Recorder"]
