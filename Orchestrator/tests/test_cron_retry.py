"""
Tests for M2.8 — bounded retry-on-failure with per-attempt history.

_run_job_body wraps the execution in a bounded retry loop (up to MAX_RETRIES
retries, with backoff). Each ATTEMPT writes its own history row. Retries
happen WITHIN the single held per-job lock (no APScheduler re-enqueue). A
succeeding attempt stops further retries. After exhausting all retries the job
increments error_count ONCE (not per attempt) and stays scheduled (active).

  (a) Fails twice then succeeds: 3 attempts, 3 history rows, ends successful,
      error_count unchanged.
  (b) Fails every attempt: MAX_RETRIES+1 history rows, error_count +1 (once),
      job still active.
  (c) A one-shot that fails all retries is NOT deleted (M2.6 rule upheld).

Backoff is monkeypatched to ~0 so the tests are fast. The executor call is
mocked so no live server is needed.
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
    # Zero out the backoff so retries are instant.
    monkeypatch.setattr(
        manager_mod,
        "RETRY_BACKOFF_SECONDS",
        [0] * (manager_mod.MAX_RETRIES + 1),
    )
    return CronJobManager()


def _make_flaky_executor(fail_times):
    """Return an async executor that raises for the first `fail_times` calls
    then succeeds, plus a counter object exposing .calls."""
    state = {"calls": 0}

    async def executor(job):
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise RuntimeError(f"transient failure #{state['calls']}")
        return "success"

    return executor, state


# ---------------------------------------------------------------------------
# (a) Fails twice then succeeds → 3 attempts, 3 rows, success, error_count same
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fails_twice_then_succeeds(temp_manager, monkeypatch):
    assert manager_mod.MAX_RETRIES >= 2, "test assumes MAX_RETRIES >= 2"
    job = temp_manager.create_job(
        name="flaky", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    job_id = job["id"]

    executor, state = _make_flaky_executor(fail_times=2)
    monkeypatch.setattr(executor_mod, "execute_cron_job", executor)

    await temp_manager._execute_job(job_id)

    # 3 attempts total (2 fails + 1 success).
    assert state["calls"] == 3

    history = temp_manager.get_job_history(job_id)
    assert len(history) == 3
    statuses = sorted(h["delivery_status"] for h in history)
    assert statuses == ["delivered", "error", "error"]

    refreshed = temp_manager.get_job(job_id)
    assert refreshed["last_run_result"] == "success"
    # A run that ultimately succeeds does not count as a job error.
    assert refreshed["error_count"] == 0
    # run_count increments once per executed run, not per attempt.
    assert refreshed["run_count"] == 1


# ---------------------------------------------------------------------------
# (b) Fails every attempt → MAX_RETRIES+1 rows, error_count +1 once, active
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fails_all_attempts(temp_manager, monkeypatch):
    job = temp_manager.create_job(
        name="always-fails", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    job_id = job["id"]

    executor, state = _make_flaky_executor(fail_times=999)  # never succeeds
    monkeypatch.setattr(executor_mod, "execute_cron_job", executor)

    await temp_manager._execute_job(job_id)

    expected_attempts = manager_mod.MAX_RETRIES + 1
    assert state["calls"] == expected_attempts

    history = temp_manager.get_job_history(job_id)
    assert len(history) == expected_attempts
    assert all(h["delivery_status"] == "error" for h in history)

    refreshed = temp_manager.get_job(job_id)
    assert refreshed["last_run_result"] == "error"
    # error_count incremented exactly ONCE for the whole failed run.
    assert refreshed["error_count"] == 1
    assert refreshed["run_count"] == 1
    # Job stays scheduled/active after a failed run.
    assert refreshed["status"] == "active"


# ---------------------------------------------------------------------------
# (c) One-shot failing all retries is NOT deleted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_one_shot_failing_all_retries_not_deleted(temp_manager, monkeypatch):
    job = temp_manager.create_job(
        name="oneshot-flaky", prompt="hi", schedule="0 15 * * *",
        operator="system", one_shot=True,
    )
    job_id = job["id"]

    executor, state = _make_flaky_executor(fail_times=999)
    monkeypatch.setattr(executor_mod, "execute_cron_job", executor)

    await temp_manager._execute_job(job_id)

    assert state["calls"] == manager_mod.MAX_RETRIES + 1
    # The failed one-shot survives.
    assert temp_manager.get_job(job_id) is not None


@pytest.mark.asyncio
async def test_one_shot_succeeding_on_retry_is_deleted(temp_manager, monkeypatch):
    """A one-shot that fails once then succeeds is still deleted (final
    outcome is success)."""
    job = temp_manager.create_job(
        name="oneshot-retry-ok", prompt="hi", schedule="0 15 * * *",
        operator="system", one_shot=True,
    )
    job_id = job["id"]

    executor, state = _make_flaky_executor(fail_times=1)
    monkeypatch.setattr(executor_mod, "execute_cron_job", executor)

    await temp_manager._execute_job(job_id)

    assert state["calls"] == 2  # one fail + one success
    assert temp_manager.get_job(job_id) is None


# ---------------------------------------------------------------------------
# A first-attempt success does NOT retry (no spurious extra attempts/rows)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_attempt_success_no_retry(temp_manager, monkeypatch):
    job = temp_manager.create_job(
        name="clean", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    job_id = job["id"]

    executor, state = _make_flaky_executor(fail_times=0)
    monkeypatch.setattr(executor_mod, "execute_cron_job", executor)

    await temp_manager._execute_job(job_id)

    assert state["calls"] == 1
    history = temp_manager.get_job_history(job_id)
    assert len(history) == 1
    assert history[0]["delivery_status"] == "delivered"
