"""
Tests for M1.2 — next_run_at must be truthful, never a frozen cache.

next_run_at is a *derived projection* of (cron schedule x box-local clock),
not durable state. Two truths it must obey:

  (a) A paused job has no next run, so its persisted next_run_at must be
      cleared on pause.
  (b) On every startup re-register the persisted next_run_at must be
      recomputed from the live scheduler, so a value frozen by a previous
      process (e.g. the infamous March-frozen job) is overwritten with the
      real next fire time.
"""

import asyncio
import sqlite3

import pytest

from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler.manager import CronJobManager


@pytest.fixture()
def temp_manager(tmp_path, monkeypatch):
    db = tmp_path / "cron_jobs_test.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    return CronJobManager()


def _raw_next_run_at(mgr, job_id):
    """Read next_run_at straight from the row, bypassing dict normalisation."""
    conn = sqlite3.connect(mgr.db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT next_run_at FROM cron_jobs WHERE id = ?", (job_id,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_pause_clears_next_run_at(temp_manager):
    """Pausing a job clears its next_run_at (a paused job has no next run)."""
    job = temp_manager.create_job(
        name="daily", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    # Sanity: a freshly-created active job has a next_run_at.
    assert _raw_next_run_at(temp_manager, job["id"])

    temp_manager.pause_job(job["id"])

    assert not _raw_next_run_at(temp_manager, job["id"]), (
        "paused job still has a next_run_at; it should be cleared"
    )


def test_startup_recomputes_frozen_next_run_at(temp_manager):
    """A stale next_run_at frozen by a prior process is recomputed on start()."""
    job = temp_manager.create_job(
        name="daily", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    job_id = job["id"]

    # Simulate a value frozen by a previous process (the March-frozen bug).
    stale = "2026-03-02T15:00:00-05:00"
    conn = sqlite3.connect(temp_manager.db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE cron_jobs SET next_run_at = ? WHERE id = ?", (stale, job_id)
        )
        conn.commit()
    finally:
        conn.close()
    assert _raw_next_run_at(temp_manager, job_id) == stale

    async def _run():
        await temp_manager.start()
        try:
            live = temp_manager.scheduler.get_job(job_id).next_run_time
        finally:
            await temp_manager.shutdown()
        return live

    live = asyncio.run(_run())

    persisted = _raw_next_run_at(temp_manager, job_id)
    assert persisted != stale, "stale next_run_at was not overwritten on startup"
    assert persisted == live.isoformat(), (
        f"persisted next_run_at {persisted!r} != live scheduler "
        f"next_run_time {live.isoformat()!r}"
    )
