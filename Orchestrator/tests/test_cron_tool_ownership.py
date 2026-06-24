"""
Tests for M2.5 — operator-ownership scoping on mutating cron tools.

Before mutating, the tool resolves manager.get_job(job_id) and allows the call
only when the job is owned by ctx.operator OR ctx.operator == "system".
Otherwise it returns ToolResult(False, "Job not found") — a GENERIC message
that does not leak the existence of another operator's job.

Applies to edit_cron_job (here) and the cron_job_history tool (M2.7).
NO delete tool exists — delete stays UI-only.
"""

import asyncio

import pytest

from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler.manager import CronJobManager
from Orchestrator.toolvault.context import ToolContext
from ToolVault.tools.edit_cron_job.executor import execute as edit_execute


@pytest.fixture()
def temp_manager(tmp_path, monkeypatch):
    db = tmp_path / "cron_jobs_ownership.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    mgr = CronJobManager()
    monkeypatch.setattr(manager_mod, "_manager_instance", mgr, raising=False)
    return mgr


def _run(coro):
    return asyncio.run(coro)


def test_operator_a_cannot_edit_operator_b_job(temp_manager):
    """Cross-operator edit returns a generic 'Job not found' — no existence
    leak and no mutation."""
    job_b = temp_manager.create_job(
        name="b job", prompt="hi", schedule="0 7 * * *", operator="B"
    )
    result = _run(
        edit_execute(
            {"job_id": job_b["id"], "name": "hacked"},
            ToolContext(operator="A"),
        )
    )
    assert not result.success
    assert result.result == "Job not found"
    # The job must be untouched.
    assert temp_manager.get_job(job_b["id"])["name"] == "b job"


def test_owner_may_edit_own_job(temp_manager):
    job_a = temp_manager.create_job(
        name="a job", prompt="hi", schedule="0 7 * * *", operator="A"
    )
    result = _run(
        edit_execute(
            {"job_id": job_a["id"], "name": "renamed"},
            ToolContext(operator="A"),
        )
    )
    assert result.success, result.result
    assert temp_manager.get_job(job_a["id"])["name"] == "renamed"


def test_system_may_edit_any_job(temp_manager):
    job_b = temp_manager.create_job(
        name="b job", prompt="hi", schedule="0 7 * * *", operator="B"
    )
    result = _run(
        edit_execute(
            {"job_id": job_b["id"], "name": "sys-renamed"},
            ToolContext(operator="system"),
        )
    )
    assert result.success, result.result
    assert temp_manager.get_job(job_b["id"])["name"] == "sys-renamed"


def test_missing_job_returns_generic_not_found(temp_manager):
    result = _run(
        edit_execute(
            {"job_id": "cron_doesnotexist", "name": "x"},
            ToolContext(operator="A"),
        )
    )
    assert not result.success
    assert result.result == "Job not found"
