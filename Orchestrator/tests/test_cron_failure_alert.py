"""
Tests for M5.1 — cron failure alerts + realizing delivery='notification' via
the notification bus.

Two behaviours hang off the END of _execute_job (after the retry loop has
already resolved success vs terminal failure):

  (a) Failure alert: a job that FAILS ALL retries fires exactly ONE
      notify(category='alert') carrying the operator + the error text. The
      dedup_key collapses any retry-storm duplicates into one logical alert.
  (b) delivery='notification' realized: a SUCCESSFUL run whose delivery is
      'notification' fires notify(category='cron') with the result text — the
      long-dead 'notification' delivery mode now actually delivers (it used to
      silently fall through to snapshot).
  (c) A successful delivery='snapshot' run does NOT notify (snapshot/auto-mint
      already handled it in the pipeline).
  (d) Robustness: a notify() that RAISES must NOT break the job's own status /
      stats bookkeeping (run_count/error_count still written).

notify() is patched out so these are fast and assert only the call contract.
"""

import asyncio

import pytest

from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler import executor as executor_mod
from Orchestrator.scheduler.manager import CronJobManager


@pytest.fixture()
def temp_manager(tmp_path, monkeypatch):
    db = tmp_path / "cron_jobs_alert_test.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    # Zero out the retry backoff so the failure-path tests (which exhaust all
    # retries) don't sleep the real 5s/30s schedule.
    monkeypatch.setattr(
        manager_mod,
        "RETRY_BACKOFF_SECONDS",
        [0] * (manager_mod.MAX_RETRIES + 1),
    )
    return CronJobManager()


class _NotifyRecorder:
    """Async stand-in for notify() that records every call."""

    def __init__(self):
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return None


# ---------------------------------------------------------------------------
# (a) Terminal failure -> exactly one notify(category='alert')
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_job_alerts_once(temp_manager, monkeypatch):
    """A job that fails all retries calls notify(category='alert') exactly
    once, carrying the operator and the error text."""
    job = temp_manager.create_job(
        name="failer", prompt="hi", schedule="0 15 * * *", operator="alice"
    )
    job_id = job["id"]

    async def fake_execute(j):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(executor_mod, "execute_cron_job", fake_execute)

    rec = _NotifyRecorder()
    monkeypatch.setattr(manager_mod, "notify", rec)

    await temp_manager._execute_job(job_id)

    alert_calls = [
        c for c in rec.calls if c["kwargs"].get("category") == "alert"
    ]
    assert len(alert_calls) == 1, "exactly one alert per terminal failure"
    kw = alert_calls[0]["kwargs"]
    assert kw["operator"] == "alice"
    assert "kaboom" in kw["body"]
    assert "failer" in kw["title"]
    # Idempotent per terminal failure: a stable dedup_key is provided.
    assert kw.get("dedup_key")


# ---------------------------------------------------------------------------
# (b) Successful delivery='notification' -> notify(category='cron')
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notification_delivery_pushes(temp_manager, monkeypatch):
    """A successful delivery='notification' run pushes notify(category='cron')
    with the result text."""
    job = temp_manager.create_job(
        name="notifier", prompt="hi", schedule="0 15 * * *",
        operator="bob", delivery="notification",
    )
    job_id = job["id"]

    async def fake_execute(j):
        return "the result body"

    monkeypatch.setattr(executor_mod, "execute_cron_job", fake_execute)

    rec = _NotifyRecorder()
    monkeypatch.setattr(manager_mod, "notify", rec)

    await temp_manager._execute_job(job_id)

    cron_calls = [c for c in rec.calls if c["kwargs"].get("category") == "cron"]
    assert len(cron_calls) == 1
    kw = cron_calls[0]["kwargs"]
    assert kw["operator"] == "bob"
    assert kw["title"] == "notifier"
    assert "the result body" in kw["body"]


# ---------------------------------------------------------------------------
# (c) Successful delivery='snapshot' -> no notify
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_delivery_does_not_notify(temp_manager, monkeypatch):
    """A successful delivery='snapshot' run does NOT notify (snapshot/auto-mint
    already handled the persistence in the pipeline)."""
    job = temp_manager.create_job(
        name="snapper", prompt="hi", schedule="0 15 * * *",
        operator="carol", delivery="snapshot",
    )
    job_id = job["id"]

    async def fake_execute(j):
        return "result"

    monkeypatch.setattr(executor_mod, "execute_cron_job", fake_execute)

    rec = _NotifyRecorder()
    monkeypatch.setattr(manager_mod, "notify", rec)

    await temp_manager._execute_job(job_id)

    assert rec.calls == [], "snapshot delivery must not notify"


# ---------------------------------------------------------------------------
# (d) A notify() exception must NOT break the job's status/stats writes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_exception_does_not_break_stats(temp_manager, monkeypatch):
    """A notify() that RAISES must not break the job's own bookkeeping —
    run_count/error_count are still written for the failed run."""
    job = temp_manager.create_job(
        name="boomnotify", prompt="hi", schedule="0 15 * * *", operator="dave"
    )
    job_id = job["id"]

    async def fake_execute(j):
        raise RuntimeError("exec boom")

    monkeypatch.setattr(executor_mod, "execute_cron_job", fake_execute)

    async def exploding_notify(*args, **kwargs):
        raise RuntimeError("notify boom")

    monkeypatch.setattr(manager_mod, "notify", exploding_notify)

    # Must not raise out of _execute_job.
    await temp_manager._execute_job(job_id)

    refreshed = temp_manager.get_job(job_id)
    assert refreshed["run_count"] == 1
    assert refreshed["error_count"] == 1
    assert refreshed["last_run_result"] == "error"


@pytest.mark.asyncio
async def test_notify_skipped_for_system_operator(temp_manager, monkeypatch):
    """A failed job owned by 'system' (or blank) does NOT notify — we never
    push to nobody."""
    job = temp_manager.create_job(
        name="syswork", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    job_id = job["id"]

    async def fake_execute(j):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(executor_mod, "execute_cron_job", fake_execute)

    rec = _NotifyRecorder()
    monkeypatch.setattr(manager_mod, "notify", rec)

    await temp_manager._execute_job(job_id)

    assert rec.calls == [], "system-operator job must not notify"
