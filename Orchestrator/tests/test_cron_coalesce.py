"""
Tests for M3.1 — coalesce piled-up fires into a single run + single instance.

LOCKED missed-run policy: if multiple fires of a job pile up WHILE THE
SCHEDULER IS ALIVE (a brief alive-but-busy window), only ONE run should
execute, never N. APScheduler expresses this with:

  * coalesce=True       — collapse multiple pending fires of a job into one
  * max_instances=1     — never run two instances of the same job concurrently

These defaults are applied via the scheduler's job_defaults AND on every
add_job in _register_job_with_scheduler, so they hold for both the live
scheduler and each registered job.

The wide misfire_grace_time (MISFIRE_GRACE_SECONDS) is a separate knob: it
keeps a fire that was MISSED during a brief alive-but-busy window eligible to
run. The COLD-restart catch-up is M3.2's job, not misfire_grace.
"""

import asyncio

from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler.manager import (
    CronJobManager,
    MISFIRE_GRACE_SECONDS,
)


def _temp_manager(tmp_path, monkeypatch):
    db = tmp_path / "cron_jobs_test.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    return CronJobManager()


def test_scheduler_job_defaults_coalesce_single_instance(tmp_path, monkeypatch):
    """The scheduler's job_defaults enforce coalesce + max_instances=1."""
    mgr = _temp_manager(tmp_path, monkeypatch)
    defaults = mgr.scheduler._job_defaults
    assert defaults.get("coalesce") is True, (
        "scheduler job_defaults must set coalesce=True so piled-up fires "
        "collapse into a single run"
    )
    assert defaults.get("max_instances") == 1, (
        "scheduler job_defaults must set max_instances=1 so a job never "
        "runs two instances concurrently"
    )


def test_registered_job_is_coalesced_single_instance(tmp_path, monkeypatch):
    """A registered job carries coalesce=True and max_instances=1."""
    mgr = _temp_manager(tmp_path, monkeypatch)
    job = mgr.create_job(
        name="daily", prompt="hi", schedule="0 15 * * *", operator="system"
    )

    async def _run():
        await mgr.start()
        try:
            return mgr.scheduler.get_job(job["id"])
        finally:
            await mgr.shutdown()

    scheduled = asyncio.run(_run())
    assert scheduled is not None
    assert scheduled.coalesce is True, (
        "registered job must have coalesce=True"
    )
    assert scheduled.max_instances == 1, (
        "registered job must have max_instances=1"
    )


def test_misfire_grace_is_a_wide_module_constant(tmp_path, monkeypatch):
    """misfire_grace_time is a wide module constant, not a 30s literal.

    A fire missed during a brief alive-but-busy window must still count, so
    the grace window is wide (hours, not seconds). It is exposed as a module
    constant so the value has one source of truth.
    """
    assert MISFIRE_GRACE_SECONDS >= 3600, (
        "MISFIRE_GRACE_SECONDS should be a wide window (>= 1 hour) so a fire "
        "missed during a brief alive-but-busy window still counts"
    )

    mgr = _temp_manager(tmp_path, monkeypatch)
    job = mgr.create_job(
        name="daily", prompt="hi", schedule="0 15 * * *", operator="system"
    )

    async def _run():
        await mgr.start()
        try:
            return mgr.scheduler.get_job(job["id"])
        finally:
            await mgr.shutdown()

    scheduled = asyncio.run(_run())
    assert scheduled.misfire_grace_time == MISFIRE_GRACE_SECONDS, (
        "registered job's misfire_grace_time must come from "
        "MISFIRE_GRACE_SECONDS"
    )
