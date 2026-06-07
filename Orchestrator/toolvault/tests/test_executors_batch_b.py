"""Tests for Batch B module executors (Task 6.2).

Batch B migrates 9 memory + communication + contacts tool executors OUT of the
monolithic ``blackbox_tools._execute_<name>`` methods INTO per-tool
``ToolVault/tools/<name>/executor.py`` modules.

Special case: ``search_snapshots`` has NO ``_execute_search_snapshots`` — its
body is ``_execute_search_memory`` (canonical ``search_snapshots`` -> executor
name ``search_memory`` per ``_EXECUTOR_NAMES``). So the executor lives in
``ToolVault/tools/search_snapshots/executor.py`` and ``get_executor`` resolves
BOTH the canonical name and the ``search_memory`` alias to the SAME callable.

These tests run against the REAL on-disk modules (no tmp_path) — they assert the
9 executors load cleanly, the alias resolves to the same object, and a couple
route correctly with externals mocked.
"""

import asyncio
import inspect

import pytest

from Orchestrator.toolvault import registry
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor


BATCH_B = [
    "search_snapshots",
    "get_snapshot",
    "list_recent_snapshots",
    "get_current_time",
    "send_sms",
    "make_phone_call",
    "make_voice_call",
    "search_contacts",
    "save_contact",
]


@pytest.fixture(autouse=True)
def fresh_registry():
    """Invalidate the executor cache around each test so on-disk edits register."""
    registry.invalidate_cache()
    yield
    registry.invalidate_cache()


# ---------------------------------------------------------------------------
# 1. Every Batch B executor loads: callable, no load_errors, valid signature.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", BATCH_B)
def test_executor_is_callable(name):
    ex = registry.get_executor(name)
    assert ex is not None, f"get_executor({name!r}) returned None"
    assert callable(ex)
    assert inspect.iscoroutinefunction(ex), f"{name} executor is not async"
    positional = [
        p
        for p in inspect.signature(ex).parameters.values()
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    assert len(positional) == 2, f"{name} executor must take (params, ctx)"


@pytest.mark.parametrize("name", BATCH_B)
def test_no_load_error_for_executor(name):
    registry.get_executor(name)
    errors = registry.load_errors()
    assert name not in errors, f"{name} has load errors: {errors.get(name)}"


def test_all_batch_b_loaded():
    """All 9 resolve to a callable."""
    assert all(registry.get_executor(n) is not None for n in BATCH_B)


# ---------------------------------------------------------------------------
# 2. Alias: search_memory resolves to the SAME object as search_snapshots.
# ---------------------------------------------------------------------------

def test_search_memory_alias_same_object():
    alias = registry.get_executor("search_memory")
    canonical = registry.get_executor("search_snapshots")
    assert alias is not None
    assert canonical is not None
    assert alias is canonical, "search_memory alias must resolve to search_snapshots executor"


# ---------------------------------------------------------------------------
# 3. Routing smokes (no network needed / mocked externals).
# ---------------------------------------------------------------------------

def test_get_current_time_via_dispatch():
    """get_current_time needs no network; route through the dispatch façade."""
    ex = BlackBoxToolExecutor(operator="Brandon")
    result = asyncio.run(ex.execute("get_current_time", {}))
    assert isinstance(result, ToolResult)
    assert result.success is True
    assert "Current date and time" in result.result
    assert "iso" in (result.data or {})


def test_search_contacts_executor_smoke(monkeypatch):
    """search_contacts delegates to contacts.search_contacts; mock it.

    The executor imports it at module level as ``_search_contacts`` (mirroring
    blackbox_tools). The module is loaded via importlib so isn't in sys.modules
    under a normal name — patch the symbol via the function's __globals__.
    """
    ex = registry.get_executor("search_contacts")
    assert ex is not None
    monkeypatch.setitem(
        ex.__globals__, "_search_contacts",
        lambda query, operator: [{"name": "Alice", "phone": "+15551234567"}],
    )
    result = asyncio.run(ex({"query": "alice"}, ToolContext(operator="Brandon")))
    assert isinstance(result, ToolResult)
    assert result.success is True
    assert "Alice" in result.result
    assert result.data == {"contacts": [{"name": "Alice", "phone": "+15551234567"}]}


def test_search_contacts_requires_query():
    ex = registry.get_executor("search_contacts")
    result = asyncio.run(ex({}, ToolContext(operator="system")))
    assert result.success is False
    assert "query is required" in result.result.lower()


# ---------------------------------------------------------------------------
# 4. Network-heavy ones: just confirm a valid 2-param async executor loads.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["send_sms", "make_phone_call", "make_voice_call"])
def test_network_executor_signature(name):
    ex = registry.get_executor(name)
    assert ex is not None
    assert inspect.iscoroutinefunction(ex)
    positional = [
        p
        for p in inspect.signature(ex).parameters.values()
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    assert len(positional) == 2
