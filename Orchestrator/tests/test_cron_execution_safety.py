"""
Tests for M2.6 — per-job execution lock, delete-one-shot-only-on-success,
and removal of the dead executor-stub fallback branch.

These cover _execute_job's concurrency and one-shot-cleanup safety:

  (a) Per-job lock: two concurrent _execute_job(job_id) calls run the body
      ONCE; the second observes the held lock, writes a "skipped: already
      running" history note, and returns without double-executing.
  (b) Delete-one-shot-only-on-success: a one-shot whose run RAISES survives
      (it is NOT silently destroyed), while a successful one-shot IS deleted.
  (c) The dead `except ImportError` stub branch (delivery_status='stub') is
      gone — a successful run records delivery_status='delivered', never
      'stub'.

The actual job execution (executor.execute_cron_job, the /chat HTTP call) is
mocked so the tests are fast, deterministic, and can simulate success vs
failure without a live server.
"""

import asyncio

import pytest

from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler import executor as executor_mod
from Orchestrator.scheduler.manager import CronJobManager


@pytest.fixture()
def temp_manager(tmp_path, monkeypatch):
    db = tmp_path / "cron_jobs_test.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    # Build the manager against the patched DB path. The APScheduler instance is
    # created but never started, so add_job/get_job calls are harmless no-ops.
    return CronJobManager()


# ---------------------------------------------------------------------------
# (a) Per-job execution lock — concurrent calls run the body once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_execute_runs_body_once(temp_manager, monkeypatch):
    """Two concurrent _execute_job(job_id) calls: the body runs ONCE; the
    second observes the held lock, writes a skipped note, and does not
    double-execute."""
    job = temp_manager.create_job(
        name="locker", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    job_id = job["id"]

    started = asyncio.Event()
    release = asyncio.Event()
    call_count = 0

    async def fake_execute(j):
        nonlocal call_count
        call_count += 1
        started.set()          # signal the body is now inside the lock
        await release.wait()    # hold the lock until the test releases it
        return "ok"

    monkeypatch.setattr(executor_mod, "execute_cron_job", fake_execute)

    # First call: acquires the lock and blocks inside fake_execute.
    first = asyncio.create_task(temp_manager._execute_job(job_id))
    await started.wait()

    # Second call while the first still holds the lock: must SKIP, not block.
    # A correct lock returns immediately; the timeout guards against a missing
    # lock deadlocking the suite (it would block forever on release.wait()).
    await asyncio.wait_for(temp_manager._execute_job(job_id), timeout=5)

    # Let the first call complete.
    release.set()
    await first

    # The body executed exactly once.
    assert call_count == 1

    # History has two rows: one real run (delivered) + one skipped note.
    history = temp_manager.get_job_history(job_id)
    statuses = [h["delivery_status"] for h in history]
    assert statuses.count("skipped") == 1
    assert statuses.count("delivered") == 1
    # run_count incremented only for the real run, not the skip.
    refreshed = temp_manager.get_job(job_id)
    assert refreshed["run_count"] == 1


@pytest.mark.asyncio
async def test_skipped_run_records_history_note(temp_manager, monkeypatch):
    """The skipped run writes a history row whose error/result mentions it was
    skipped because the job was already running."""
    job = temp_manager.create_job(
        name="locker2", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    job_id = job["id"]

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_execute(j):
        started.set()
        await release.wait()
        return "ok"

    monkeypatch.setattr(executor_mod, "execute_cron_job", fake_execute)

    first = asyncio.create_task(temp_manager._execute_job(job_id))
    await started.wait()
    await asyncio.wait_for(temp_manager._execute_job(job_id), timeout=5)
    release.set()
    await first

    history = temp_manager.get_job_history(job_id)
    skipped = [h for h in history if h["delivery_status"] == "skipped"]
    assert len(skipped) == 1
    note = (skipped[0]["error"] or "") + (skipped[0]["result"] or "")
    assert "already running" in note.lower()


# ---------------------------------------------------------------------------
# (b) One-shot delete only on success
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_one_shot_is_not_deleted(temp_manager, monkeypatch):
    """A one-shot whose run RAISES must survive (not be silently destroyed)."""
    job = temp_manager.create_job(
        name="oneshot-fail", prompt="hi", schedule="0 15 * * *",
        operator="system", one_shot=True,
    )
    job_id = job["id"]

    async def fake_execute(j):
        raise RuntimeError("boom")

    monkeypatch.setattr(executor_mod, "execute_cron_job", fake_execute)

    await temp_manager._execute_job(job_id)

    # The failed one-shot is still in the store.
    assert temp_manager.get_job(job_id) is not None
    # Its run was recorded as an error.
    history = temp_manager.get_job_history(job_id)
    assert any(h["delivery_status"] == "error" for h in history)


@pytest.mark.asyncio
async def test_successful_one_shot_is_deleted(temp_manager, monkeypatch):
    """A one-shot whose run SUCCEEDS is deleted after execution."""
    job = temp_manager.create_job(
        name="oneshot-ok", prompt="hi", schedule="0 15 * * *",
        operator="system", one_shot=True,
    )
    job_id = job["id"]

    async def fake_execute(j):
        return "done"

    monkeypatch.setattr(executor_mod, "execute_cron_job", fake_execute)

    await temp_manager._execute_job(job_id)

    # The successful one-shot is gone.
    assert temp_manager.get_job(job_id) is None


# ---------------------------------------------------------------------------
# (c) Dead stub branch removed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_stub_delivery_status(temp_manager, monkeypatch):
    """A successful run records delivery_status='delivered', never 'stub' —
    the dead `except ImportError` stub branch has been removed."""
    job = temp_manager.create_job(
        name="nostub", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    job_id = job["id"]

    async def fake_execute(j):
        return "real result"

    monkeypatch.setattr(executor_mod, "execute_cron_job", fake_execute)

    await temp_manager._execute_job(job_id)

    history = temp_manager.get_job_history(job_id)
    statuses = [h["delivery_status"] for h in history]
    assert "stub" not in statuses
    assert statuses == ["delivered"]
