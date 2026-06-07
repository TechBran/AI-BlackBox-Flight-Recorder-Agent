"""Tests for Batch C module executors (Task 6.2).

Batch C migrates 7 scheduling + computer-control + task executors OUT of the
monolithic ``blackbox_tools._execute_<name>`` methods INTO per-tool
``ToolVault/tools/<name>/executor.py`` modules:

    create_cron_job, edit_cron_job, search_cron_jobs,
    use_computer, list_devices, control_android_device, get_task_status

These tests run against the REAL on-disk modules (no tmp_path) — they assert the
7 executors load cleanly and a couple route correctly without touching the
network.
"""

import asyncio
import inspect

import pytest

from Orchestrator.toolvault import registry
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor


BATCH_C = [
    "create_cron_job",
    "edit_cron_job",
    "search_cron_jobs",
    "use_computer",
    "list_devices",
    "control_android_device",
    "get_task_status",
]


@pytest.fixture(autouse=True)
def fresh_registry():
    """Invalidate the executor cache around each test so on-disk edits register."""
    registry.invalidate_cache()
    yield
    registry.invalidate_cache()


# ---------------------------------------------------------------------------
# 1. Every Batch C executor loads: callable, no load_errors, valid signature.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", BATCH_C)
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


@pytest.mark.parametrize("name", BATCH_C)
def test_no_load_error_for_executor(name):
    registry.get_executor(name)
    errors = registry.load_errors()
    assert name not in errors, f"{name} has load errors: {errors.get(name)}"


def test_all_batch_c_loaded():
    """All 7 resolve to a callable."""
    assert all(registry.get_executor(n) is not None for n in BATCH_C)


# ---------------------------------------------------------------------------
# 2. Routing smokes (no network needed — short-circuit on missing params).
# ---------------------------------------------------------------------------

def test_get_task_status_requires_task_id_via_dispatch():
    """get_task_status short-circuits on a missing task_id (no network)."""
    ex = BlackBoxToolExecutor(operator="Brandon")
    result = asyncio.run(ex.execute("get_task_status", {}))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "Task ID is required" in result.result


def test_use_computer_requires_prompt():
    ex = registry.get_executor("use_computer")
    result = asyncio.run(ex({}, ToolContext(operator="system")))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "Prompt is required" in result.result


def test_control_android_device_requires_prompt():
    ex = registry.get_executor("control_android_device")
    result = asyncio.run(ex({}, ToolContext(operator="system")))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "Prompt is required" in result.result


def test_control_android_device_requires_device_id():
    ex = registry.get_executor("control_android_device")
    result = asyncio.run(ex({"prompt": "do a thing"}, ToolContext(operator="system")))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "device_id is required" in result.result


def test_edit_cron_job_requires_job_id():
    ex = registry.get_executor("edit_cron_job")
    result = asyncio.run(ex({}, ToolContext(operator="system")))
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "job_id is required" in result.result


# ---------------------------------------------------------------------------
# 3. Network/manager-heavy ones: confirm a valid 2-param async executor loads.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["create_cron_job", "search_cron_jobs", "list_devices"])
def test_executor_signature(name):
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
