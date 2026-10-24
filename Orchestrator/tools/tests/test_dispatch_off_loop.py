"""Audit rank 46 (O18-03): a tool that blocks must never stall the event loop.

BlackBoxToolExecutor is the one dispatcher the chat streams, the realtime and
live voice bridges and the phone bridge share, and those all run on the
server's single event loop. A sync ``def execute`` therefore runs in a worker
thread (contextvars propagate, as with asyncio.to_thread) under a timeout: the
schema's ``timeout_s`` when set, otherwise DEFAULT_SYNC_TIMEOUT_S. An ``async
def execute`` is awaited as before, never wrapped again, and is bounded only by
a ``timeout_s`` its schema declares.

Hermetic: the registry points at a tmp tools dir; sleeps are short.
"""

import asyncio
import inspect
import json
import threading
import time

import pytest

from Orchestrator import tool_outcomes
from Orchestrator.toolvault import registry, schema_spec, resolvers
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.tools import blackbox_tools
from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor


def _schema(name, **extra):
    d = {
        "name": name,
        "description": "A tool.",
        "category": "utility",
        "groups": ["chat"],
        "tier": 2,
        "parameters": {"type": "object", "properties": {
            "sleep": {"type": "number", "description": "seconds"}}},
    }
    d.update(extra)
    return d


def _write(tools_dir, name, body, **schema_extra):
    folder = tools_dir / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "schema.json").write_text(json.dumps(_schema(name, **schema_extra)))
    (folder / "executor.py").write_text(body)


@pytest.fixture
def tools_dir(tmp_path, monkeypatch):
    d = tmp_path / "tools"
    d.mkdir()
    monkeypatch.setattr(registry, "TOOLS_DIR", d)
    registry.invalidate_cache()
    yield d
    registry.invalidate_cache()


# A blocking sync executor: sleeps, then reports the thread it ran on.
_SYNC_SLEEPER = (
    "import threading, time\n"
    "from Orchestrator.toolvault.context import ToolResult\n"
    "def execute(params, ctx):\n"
    "    time.sleep(params.get('sleep', 0.5))\n"
    "    return ToolResult(True, threading.current_thread().name)\n"
)

# An async executor: awaits, then reports the thread it ran on.
_ASYNC_SLEEPER = (
    "import asyncio, threading\n"
    "from Orchestrator.toolvault.context import ToolResult\n"
    "CANCELLED = []\n"
    "async def execute(params, ctx):\n"
    "    try:\n"
    "        await asyncio.sleep(params.get('sleep', 0))\n"
    "    except asyncio.CancelledError:\n"
    "        CANCELLED.append(True)\n"
    "        raise\n"
    "    return ToolResult(True, threading.current_thread().name)\n"
)


async def _with_ticker(coro):
    """Await ``coro`` while a 20 ms ticker counts event-loop turns."""
    ticks = 0
    stop = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.02)
            ticks += 1

    t = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        stop.set()
        await t
    return result, ticks


def _execute(name, params, operator="LoopTester"):
    return BlackBoxToolExecutor(operator=operator).execute(name, params)


# ---------------------------------------------------------------------------
# Sync executors run off the loop
# ---------------------------------------------------------------------------

def test_blocking_sync_executor_does_not_stall_the_loop(tools_dir):
    """A 0.5 s blocking executor must leave a 20 ms ticker running. Run on the
    loop (the old contract) it would get ~0 ticks for the whole call."""
    _write(tools_dir, "slow_sync", _SYNC_SLEEPER)
    result, ticks = asyncio.run(_with_ticker(_execute("slow_sync", {"sleep": 0.5})))
    assert result.success is True, result.result
    assert ticks >= 15, f"event loop stalled during the tool call (ticks={ticks})"
    assert result.result != threading.main_thread().name


def test_sync_executor_sees_the_callers_context(tools_dir):
    """contextvars propagate into the worker thread: the turn's tool-outcome
    log the /chat worker opened is the one the executor sees."""
    _write(tools_dir, "ctx_probe", (
        "from Orchestrator import tool_outcomes\n"
        "from Orchestrator.toolvault.context import ToolResult\n"
        "def execute(params, ctx):\n"
        "    log = tool_outcomes._LOG.get()\n"
        "    return ToolResult(log is not None, f'log={log is not None}')\n"
    ))

    async def go():
        log = tool_outcomes.begin()
        try:
            res = await _execute("ctx_probe", {})
        finally:
            tool_outcomes.end()
        return res, log

    result, log = asyncio.run(go())
    assert result.success is True, result.result
    assert [e["tool"] for e in log] == ["ctx_probe"]


def test_sync_executor_exception_becomes_a_failed_result(tools_dir):
    _write(tools_dir, "sync_boom", (
        "def execute(params, ctx):\n"
        "    raise RuntimeError('boom in thread')\n"
    ))
    result = asyncio.run(_execute("sync_boom", {}))
    assert result.success is False
    assert "boom in thread" in result.result


def test_get_executor_hands_every_caller_an_awaitable(tools_dir):
    """Direct callers (control_device, tests) await whatever get_executor
    returns, so a sync module comes back wrapped: awaitable, off the loop."""
    _write(tools_dir, "slow_sync", _SYNC_SLEEPER)
    ex = registry.get_executor("slow_sync")
    assert ex is not None
    assert inspect.iscoroutinefunction(ex)
    assert getattr(ex, "runs_in_thread", False) is True
    assert registry.get_executor("slow_sync") is ex  # cached like any executor
    result, ticks = asyncio.run(_with_ticker(ex({"sleep": 0.3}, ToolContext())))
    assert result.success is True
    assert ticks >= 8


def test_a_sync_generator_is_still_refused(tools_dir):
    _write(tools_dir, "gen_tool", (
        "def execute(params, ctx):\n"
        "    yield 1\n"
    ))
    assert registry.get_executor("gen_tool") is None
    assert "gen_tool" in registry.load_errors()


# ---------------------------------------------------------------------------
# Async executors are not wrapped again
# ---------------------------------------------------------------------------

def test_async_executor_still_runs_on_the_loop_thread(tools_dir):
    _write(tools_dir, "async_tool", _ASYNC_SLEEPER)
    ex = registry.get_executor("async_tool")
    assert getattr(ex, "runs_in_thread", False) is False
    result = asyncio.run(_execute("async_tool", {"sleep": 0}))
    assert result.success is True
    assert result.result == threading.current_thread().name


def test_async_executor_gets_no_default_timeout(tools_dir, monkeypatch):
    """The default ceiling is for threaded executors; a long async tool
    (control_phone polls for minutes) keeps its own pacing."""
    monkeypatch.setattr(blackbox_tools, "DEFAULT_SYNC_TIMEOUT_S", 0.05)
    _write(tools_dir, "async_tool", _ASYNC_SLEEPER)
    result = asyncio.run(_execute("async_tool", {"sleep": 0.3}))
    assert result.success is True, result.result


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------

def test_schema_timeout_bounds_a_hung_sync_executor(tools_dir):
    _write(tools_dir, "hung_sync", _SYNC_SLEEPER, timeout_s=0.2)
    t0 = time.monotonic()
    result = asyncio.run(_execute("hung_sync", {"sleep": 1.5}))
    elapsed = time.monotonic() - t0
    assert result.success is False
    assert "did not finish within 0.2" in result.result
    # The outcome is unknown, so the model is told not to assume either way.
    assert "may still" in result.result
    assert elapsed < 1.0, f"the caller waited {elapsed:.2f}s for an abandoned tool"


def test_default_timeout_applies_to_a_sync_executor_without_one(tools_dir, monkeypatch):
    monkeypatch.setattr(blackbox_tools, "DEFAULT_SYNC_TIMEOUT_S", 0.2)
    _write(tools_dir, "hung_sync", _SYNC_SLEEPER)
    t0 = time.monotonic()
    result = asyncio.run(_execute("hung_sync", {"sleep": 1.5}))
    assert result.success is False
    assert "did not finish within 0.2" in result.result
    assert time.monotonic() - t0 < 1.0


def test_schema_timeout_cancels_an_async_executor(tools_dir):
    _write(tools_dir, "hung_async", _ASYNC_SLEEPER, timeout_s=0.1)
    result = asyncio.run(_execute("hung_async", {"sleep": 5}))
    assert result.success is False
    assert "did not finish within 0.1" in result.result
    module_globals = registry.get_executor("hung_async").__globals__
    assert module_globals["CANCELLED"] == [True]


def test_timeout_is_recorded_as_a_failed_outcome(tools_dir):
    _write(tools_dir, "hung_sync", _SYNC_SLEEPER, timeout_s=0.1)

    async def go():
        log = tool_outcomes.begin()
        try:
            await _execute("hung_sync", {"sleep": 1.0})
        finally:
            tool_outcomes.end()
        return log

    log = asyncio.run(go())
    assert len(log) == 1 and log[0]["success"] is False


def test_an_executor_raising_timeouterror_is_not_reported_as_our_timeout(tools_dir):
    """Only the dispatcher's own ceiling produces the 'did not finish' text; a
    provider timeout raised by the tool is reported as the tool's error."""
    _write(tools_dir, "raises_timeout", (
        "def execute(params, ctx):\n"
        "    raise TimeoutError('upstream read timed out')\n"
    ), timeout_s=30)
    result = asyncio.run(_execute("raises_timeout", {}))
    assert result.success is False
    assert "upstream read timed out" in result.result
    assert "did not finish within" not in result.result


# ---------------------------------------------------------------------------
# schema.json timeout_s
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [1, 2.5, 600])
def test_schema_accepts_a_positive_timeout(value):
    errs = schema_spec.validate_module_dict(
        _schema("t", timeout_s=value), "t", resolvers.KNOWN_SOURCES)
    assert errs == []


@pytest.mark.parametrize("value", [0, -1, True, "30", None, 86400])
def test_schema_rejects_a_bad_timeout(value):
    errs = schema_spec.validate_module_dict(
        _schema("t", timeout_s=value), "t", resolvers.KNOWN_SOURCES)
    assert any("timeout_s" in e for e in errs), errs
