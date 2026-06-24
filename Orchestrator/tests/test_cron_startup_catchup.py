"""
Tests for M3.2 — cold-restart catch-up for missed runs (exactly once) +
surface failed-to-register jobs as errored.

LOCKED missed-run policy: ALWAYS CATCH UP ONCE. If the whole process was DOWN
across a job's fire time, on the next start() exactly ONE coalesced catch-up
run fires — never N, even if several fires were missed while down.

Mechanism = SQLite as the source of truth. The persisted next_run_at is the
durable record of the next due fire. On start(), for each active job we
CAPTURE that stored value BEFORE re-registering (which overwrites it to the
next FUTURE fire). If the captured value is in the PAST, the job was due during
downtime → enqueue ONE catch-up through _execute_job (so the per-job lock
guards against colliding with the normal next fire).

Reload hardening: a job whose cron fails to register is marked status='error'
(a recognised state) instead of being silently dropped from the live scheduler
while it stays 'active' in the DB.
"""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler.manager import (
    LOCAL_TZ,
    CronJobManager,
    VALID_STATUS,
)


@pytest.fixture()
def temp_manager(tmp_path, monkeypatch):
    db = tmp_path / "cron_jobs_test.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    return CronJobManager()


def _set_next_run_at(mgr, job_id, value):
    conn = sqlite3.connect(mgr.db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE cron_jobs SET next_run_at = ? WHERE id = ?", (value, job_id)
        )
        conn.commit()
    finally:
        conn.close()


def _status(mgr, job_id):
    conn = sqlite3.connect(mgr.db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM cron_jobs WHERE id = ?", (job_id,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_error_is_a_valid_status():
    """'error' is a recognised job status (added to the allow-list)."""
    assert "error" in VALID_STATUS


def test_past_next_run_at_triggers_exactly_one_catchup(temp_manager, monkeypatch):
    """A job whose captured next_run_at is in the PAST gets ONE catch-up run."""
    mgr = temp_manager
    job = mgr.create_job(
        name="daily", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    job_id = job["id"]

    # Simulate the box having been DOWN past a fire time: stamp a PAST
    # next_run_at as the durable pre-restart record.
    past = (datetime.now(LOCAL_TZ) - timedelta(hours=2)).isoformat()
    _set_next_run_at(mgr, job_id, past)

    calls = []

    async def _fake_execute(jid):
        calls.append(jid)

    monkeypatch.setattr(mgr, "_execute_job", _fake_execute)

    async def _run():
        await mgr.start()
        # Give any scheduled catch-up date-job / task a tick to fire.
        for _ in range(20):
            await asyncio.sleep(0)
            await asyncio.sleep(0.01)
            if calls:
                break
        await mgr.shutdown()

    asyncio.run(_run())

    assert calls.count(job_id) == 1, (
        f"expected EXACTLY one catch-up _execute_job for {job_id}, got {calls!r}"
    )


def test_future_next_run_at_triggers_no_catchup(temp_manager, monkeypatch):
    """A job with a FUTURE next_run_at gets NO catch-up run."""
    mgr = temp_manager
    job = mgr.create_job(
        name="daily", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    job_id = job["id"]

    future = (datetime.now(LOCAL_TZ) + timedelta(hours=2)).isoformat()
    _set_next_run_at(mgr, job_id, future)

    calls = []

    async def _fake_execute(jid):
        calls.append(jid)

    monkeypatch.setattr(mgr, "_execute_job", _fake_execute)

    async def _run():
        await mgr.start()
        for _ in range(10):
            await asyncio.sleep(0)
            await asyncio.sleep(0.01)
        await mgr.shutdown()

    asyncio.run(_run())

    assert calls == [], (
        f"a job with a future next_run_at must NOT be caught up; got {calls!r}"
    )


def test_failed_register_marks_job_error_not_active(temp_manager):
    """A job whose cron fails to register is marked status='error'."""
    mgr = temp_manager
    good = mgr.create_job(
        name="good", prompt="hi", schedule="0 15 * * *", operator="system"
    )

    # Insert a job with a cron that registers fine at create-time validation
    # but is then corrupted in the DB so the startup re-register raises.
    bad = mgr.create_job(
        name="bad", prompt="hi", schedule="0 16 * * *", operator="system"
    )
    bad_id = bad["id"]
    conn = sqlite3.connect(mgr.db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE cron_jobs SET schedule = ? WHERE id = ?",
            ("not a valid cron", bad_id),
        )
        conn.commit()
    finally:
        conn.close()

    async def _run():
        await mgr.start()
        await mgr.shutdown()

    asyncio.run(_run())

    assert _status(mgr, bad_id) == "error", (
        "a job that failed to register on startup must be marked status='error', "
        "not left silently 'active'"
    )
    # The good job is unaffected.
    assert _status(mgr, good["id"]) == "active"


def test_catchup_burst_respects_concurrency_cap(temp_manager, monkeypatch):
    """Many past-due jobs at boot must not all execute at once — the catch-up
    semaphore caps concurrency so a post-outage burst drains a few at a time
    (M3.2 follow-up) instead of detonating against the LLM/SMS/voice path."""
    mgr = temp_manager
    cap = manager_mod.CATCHUP_CONCURRENCY
    n_jobs = cap + 3  # more past-due jobs than the cap

    for i in range(n_jobs):
        job = mgr.create_job(
            name=f"j{i}", prompt="hi", schedule="0 15 * * *", operator="system"
        )
        past = (datetime.now(LOCAL_TZ) - timedelta(hours=2)).isoformat()
        _set_next_run_at(mgr, job["id"], past)

    state = {"running": 0, "peak": 0}

    async def _run():
        gate = asyncio.Event()

        async def gated_execute(jid):
            state["running"] += 1
            state["peak"] = max(state["peak"], state["running"])
            await gate.wait()  # hold so concurrency can accumulate
            state["running"] -= 1

        monkeypatch.setattr(mgr, "_execute_job", gated_execute)

        await mgr.start()
        # Let the catch-up tasks start and fill the semaphore; the surplus
        # block waiting for a permit.
        for _ in range(50):
            await asyncio.sleep(0)
            await asyncio.sleep(0.005)
        peak_while_gated = state["peak"]
        gate.set()  # release everyone; the rest drain through the semaphore
        await asyncio.gather(*list(mgr._catchup_tasks), return_exceptions=True)
        await mgr.shutdown()
        return peak_while_gated

    peak = asyncio.run(_run())

    assert peak <= cap, f"catch-up exceeded the concurrency cap: {peak} > {cap}"
    assert peak == cap, (
        f"with {n_jobs} past-due jobs the burst should fill the cap "
        f"({cap}), but peaked at {peak}"
    )
