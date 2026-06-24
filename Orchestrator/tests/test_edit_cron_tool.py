"""
Tests for M2.4 — edit_cron_job tool combines pause/resume with field updates.

Today the tool early-returns after handling `pause`, silently dropping any
schedule/prompt change made in the same call ("resume it and move it to 8am"
loses the new time). After M2.4 a single edit_cron_job call must both flip the
status (pause/resume) AND apply the field updates, by translating `pause` into
an `updates["status"]` value and falling through to the single update_job path.
"""

import asyncio

import pytest

from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler.manager import CronJobManager
from Orchestrator.toolvault.context import ToolContext
from ToolVault.tools.edit_cron_job.executor import execute as edit_execute


@pytest.fixture()
def temp_manager(tmp_path, monkeypatch):
    db = tmp_path / "cron_jobs_edit_tool.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    mgr = CronJobManager()
    # The tool resolves the singleton via get_scheduler_manager(); point it at
    # this isolated instance so the executor mutates our temp DB.
    monkeypatch.setattr(manager_mod, "_manager_instance", mgr, raising=False)
    return mgr


def _run(coro):
    return asyncio.run(coro)


def test_resume_and_change_schedule_in_one_call(temp_manager):
    """pause=False AND schedule=... must both resume the job and apply the new
    schedule — the field update is no longer dropped on the resume path."""
    job = temp_manager.create_job(
        name="morning", prompt="hi", schedule="0 7 * * *", operator="system"
    )
    # Pause it first so resume is meaningful.
    temp_manager.pause_job(job["id"])
    assert temp_manager.get_job(job["id"])["status"] == "paused"

    result = _run(
        edit_execute(
            {"job_id": job["id"], "pause": False, "schedule": "0 8 * * *"},
            ToolContext(operator="system"),
        )
    )
    assert result.success, result.result

    refreshed = temp_manager.get_job(job["id"])
    assert refreshed["status"] == "active"  # resumed
    assert refreshed["schedule"] == "0 8 * * *"  # field update applied too


def test_pause_true_and_prompt_change_in_one_call(temp_manager):
    """The symmetric case: pause=True must both pause AND apply the prompt
    update in the same call."""
    job = temp_manager.create_job(
        name="job", prompt="old prompt", schedule="0 7 * * *", operator="system"
    )
    result = _run(
        edit_execute(
            {"job_id": job["id"], "pause": True, "prompt": "new prompt"},
            ToolContext(operator="system"),
        )
    )
    assert result.success, result.result

    refreshed = temp_manager.get_job(job["id"])
    assert refreshed["status"] == "paused"
    assert refreshed["prompt"] == "new prompt"


def test_plain_field_update_still_works(temp_manager):
    """A field-only edit (no pause key) must continue to work."""
    job = temp_manager.create_job(
        name="job", prompt="hi", schedule="0 7 * * *", operator="system"
    )
    result = _run(
        edit_execute(
            {"job_id": job["id"], "name": "renamed"},
            ToolContext(operator="system"),
        )
    )
    assert result.success, result.result
    assert temp_manager.get_job(job["id"])["name"] == "renamed"


def test_missing_job_id_errors(temp_manager):
    result = _run(edit_execute({}, ToolContext(operator="system")))
    assert not result.success
    assert "job_id is required" in result.result
