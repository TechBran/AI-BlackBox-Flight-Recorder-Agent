"""Tests for the dispatch façade (Task 5.1).

``BlackBoxToolExecutor.execute`` is now MODULE-FIRST: it asks the ToolVault
registry for a per-tool ``executor.py`` (``registry.get_executor``) and runs it
with a :class:`ToolContext`; only if no module executor exists does it fall back
to the legacy ``_execute_<name>`` method.

Because NO ``executor.py`` files ship today, every real call still falls through
to legacy → behavior is unchanged. These tests exercise both rails hermetically
by pointing ``registry.TOOLS_DIR`` at a ``tmp_path`` and writing throwaway
modules, restoring the cache afterward.
"""

import asyncio
import json

import pytest

from Orchestrator.toolvault import registry
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor


# ---------------------------------------------------------------------------
# Helpers — mirror the conventions in toolvault/tests/test_registry.py.
# ---------------------------------------------------------------------------

def _valid_schema(name):
    return {
        "name": name,
        "description": "A tool.",
        "category": "communication",
        "groups": ["chat", "mcp"],
        "tier": 2,
        "parameters": {
            "type": "object",
            "properties": {"x": {"type": "integer", "description": "A value"}},
            "required": [],
        },
    }


def _write_module(tools_dir, name, executor_body, schema=None):
    """Write tools_dir/<name>/{schema.json,executor.py}."""
    folder = tools_dir / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "schema.json").write_text(
        json.dumps(schema if schema is not None else _valid_schema(name))
    )
    (folder / "executor.py").write_text(executor_body)
    return folder


@pytest.fixture
def tools_dir(tmp_path, monkeypatch):
    """Point the registry at an empty tmp tools dir and reset its cache.

    monkeypatch restores ``registry.TOOLS_DIR`` after the test; we also
    invalidate the cache on the way in AND out so no tmp module leaks into a
    later test (or into the real on-disk modules).
    """
    d = tmp_path / "tools"
    d.mkdir()
    monkeypatch.setattr(registry, "TOOLS_DIR", d)
    registry.invalidate_cache()
    yield d
    registry.invalidate_cache()


# A module executor that echoes the operator + a param, proving it ran WITH a
# ToolContext carrying the executor's operator.
_ECHO_EXECUTOR = (
    "from Orchestrator.toolvault.context import ToolResult\n"
    "async def execute(params, ctx):\n"
    "    return ToolResult(True, f\"mod:{ctx.operator}:{params.get('x')}\")\n"
)

# A module executor that raises — to prove the façade catches it.
_BOOM_EXECUTOR = (
    "async def execute(params, ctx):\n"
    "    raise RuntimeError('boom in module')\n"
)


# ---------------------------------------------------------------------------
# 1. Module-first: a module executor runs ahead of any legacy method.
# ---------------------------------------------------------------------------

def test_module_first_runs_module_executor(tools_dir):
    _write_module(tools_dir, "mod_tool", _ECHO_EXECUTOR)

    ex = BlackBoxToolExecutor(operator="Brandon")
    result = asyncio.run(ex.execute("mod_tool", {"x": 1}))

    assert isinstance(result, ToolResult)
    assert result.success is True
    # Proves the module executor ran with a ToolContext carrying our operator.
    assert "mod:Brandon:1" in result.result


# ---------------------------------------------------------------------------
# 2. Alias to module: alias resolves to a canonical module executor.
# ---------------------------------------------------------------------------

def test_alias_routes_to_module(tools_dir):
    # Folder is the canonical name; the registry resolves the alias to it.
    _write_module(tools_dir, "search_snapshots", _ECHO_EXECUTOR)

    ex = BlackBoxToolExecutor(operator="Sarah")
    # "search_memory" is an alias for "search_snapshots".
    result = asyncio.run(ex.execute("search_memory", {"x": 7}))

    assert result.success is True
    assert "mod:Sarah:7" in result.result


# ---------------------------------------------------------------------------
# 3. Legacy fallback: no module executor → legacy _execute_<name> runs.
# ---------------------------------------------------------------------------

def test_legacy_fallback_when_no_module(tools_dir):
    # tools_dir is empty → get_executor("faketool") returns None.
    class _Sub(BlackBoxToolExecutor):
        async def _execute_faketool(self, params):
            return ToolResult(True, f"legacy:{self.operator}:{params.get('x')}")

    ex = _Sub(operator="Brandon")
    result = asyncio.run(ex.execute("faketool", {"x": 42}))

    assert result.success is True
    assert "legacy:Brandon:42" in result.result


def test_legacy_fallback_path_is_reached(tools_dir):
    """The legacy ``_execute_<name>`` rail is reached when no module exists.

    Migration-proof by construction: rather than naming a REAL tool (every real
    non-mcp tool is becoming an ``executor.py`` module, so any name we picked
    would eventually be shadowed by a module executor and stop exercising the
    legacy rail), we use a SYNTHETIC subclass that defines its own
    ``_execute_faketool`` and point the registry at an empty tmp tools dir so no
    module executor can shadow it. This proves the façade falls through to the
    subclass's legacy method — the same intent as the old real-tool test, but it
    can never break as more tools migrate.
    """
    class _Sub(BlackBoxToolExecutor):
        async def _execute_faketool(self, params):
            return ToolResult(False, f"legacy ran for {self.operator}")

    ex = _Sub(operator="system")
    result = asyncio.run(ex.execute("faketool", {}))

    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "legacy ran for system" in result.result


# ---------------------------------------------------------------------------
# 4. Unknown tool: neither module nor legacy.
# ---------------------------------------------------------------------------

def test_unknown_tool_returns_failure(tools_dir):
    ex = BlackBoxToolExecutor(operator="system")
    result = asyncio.run(ex.execute("no_such_tool_anywhere", {}))

    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "Unknown tool" in result.result


# ---------------------------------------------------------------------------
# 5. Exception in a module executor is caught (no crash).
# ---------------------------------------------------------------------------

def test_module_executor_exception_is_caught(tools_dir):
    _write_module(tools_dir, "boom_tool", _BOOM_EXECUTOR)

    ex = BlackBoxToolExecutor(operator="system")
    result = asyncio.run(ex.execute("boom_tool", {}))

    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "Error executing" in result.result
    assert "boom in module" in result.result
