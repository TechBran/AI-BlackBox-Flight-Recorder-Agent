"""
A restart in the middle of a cron run never re-runs it (audit
O15-scheduler-admin-03) and the scheduler awaits runs on its own loop (-04).

next_run_at used to be written only when a run finished, so during the run it
still held the fire time that had just passed. A restart then (systemctl
restart, a crash, the in-app updater) found a past next_run_at and the
cold-restart catch-up fired the job again, even when the first run had
already sent its SMS or placed its call.

Now the run's start is recorded before the executor is called, in ONE update:
run_started_at, the NEXT future fire as next_run_at, and (as soon as /chat
returns it) the task id. The completion update clears the marker. On start(),
a job that still carries the marker was interrupted: it gets an
'outcome_unknown' history row and an alert, and is not re-run.
"""

import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest

from Orchestrator.scheduler import executor as executor_mod
from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler.manager import LOCAL_TZ, CronJobManager

PHONE = "+15550000000"


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "cron_reconcile.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", path)
    monkeypatch.setattr(manager_mod, "RETRY_BACKOFF_SECONDS", [0] * (manager_mod.MAX_RETRIES + 1))
    return path


class _Notify:
    def __init__(self):
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)


def _row(path, job_id):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM cron_jobs WHERE id = ?", (job_id,)).fetchone())
    finally:
        conn.close()


def _sms_job(mgr, **over):
    params = dict(name="briefing", prompt="news", schedule="0 7 * * *",
                  operator="alice", delivery="sms", delivery_target=PHONE)
    params.update(over)
    return mgr.create_job(**params)


@pytest.mark.asyncio
async def test_run_start_is_persisted_before_the_executor_runs(db_path, monkeypatch):
    mgr = CronJobManager()
    job = _sms_job(mgr)
    seen = {}

    async def _executor(j):
        seen["at_start"] = _row(db_path, j["id"])
        executor_mod.note_run_task_id("task-42")
        seen["after_post"] = _row(db_path, j["id"])
        return "sent"

    monkeypatch.setattr(executor_mod, "execute_cron_job", _executor)
    monkeypatch.setattr(manager_mod, "notify", _Notify())
    before = datetime.now(LOCAL_TZ)

    await mgr._execute_job(job["id"])

    start = seen["at_start"]
    assert start["run_started_at"], "the run's start must be on disk before it runs"
    assert datetime.fromisoformat(start["next_run_at"]) > before, (
        "next_run_at must already be the NEXT fire, so a crash now is not a missed fire")
    assert seen["after_post"]["run_task_id"] == "task-42"

    done = _row(db_path, job["id"])
    assert done["run_started_at"] is None and done["run_task_id"] is None
    assert done["last_run_result"] == "success"
    history = mgr.get_job_history(job["id"])
    assert [h["task_id"] for h in history] == ["task-42"]


async def _start_then_stop(mgr):
    await mgr.start()
    for _ in range(10):
        await asyncio.sleep(0.01)
    await mgr.shutdown()


@pytest.mark.parametrize("one_shot", [False, True])
def test_restart_mid_run_records_outcome_unknown_and_does_not_rerun(db_path, monkeypatch, one_shot):
    """Simulate the process dying mid-run: the executor is still waiting on its
    task when the run's coroutine is torn down. A fresh manager on the same DB
    then starts, as after a restart."""
    rec = _Notify()
    monkeypatch.setattr(manager_mod, "notify", rec)
    first = CronJobManager()
    job = _sms_job(first, one_shot=one_shot)
    job_id = job["id"]
    # The job was due: a past next_run_at is exactly what the old code left on
    # disk during a run, and what made the restart re-fire it.
    past = (datetime.now(LOCAL_TZ) - timedelta(minutes=1)).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE cron_jobs SET next_run_at = ? WHERE id = ?", (past, job_id))
    conn.commit()
    conn.close()

    started = asyncio.Event()

    async def _hangs(j):
        executor_mod.note_run_task_id("task-7")
        started.set()
        await asyncio.Event().wait()  # the turn is still running when the box dies

    async def _die_mid_run():
        monkeypatch.setattr(executor_mod, "execute_cron_job", _hangs)
        run = asyncio.ensure_future(first._execute_job(job_id))
        await asyncio.wait_for(started.wait(), timeout=5)
        run.cancel()  # the process goes away; nothing after this line of the run executes
        with pytest.raises(asyncio.CancelledError):
            await run

    asyncio.run(_die_mid_run())
    assert _row(db_path, job_id)["run_started_at"], "the interrupted run left its marker"

    reruns = []

    async def _must_not_run(j):
        reruns.append(j["id"])
        return "ran again"

    monkeypatch.setattr(executor_mod, "execute_cron_job", _must_not_run)
    restarted = CronJobManager()
    asyncio.run(_start_then_stop(restarted))

    assert reruns == [], "an interrupted run must not be re-run on boot"
    row = _row(db_path, job_id)
    assert row["run_started_at"] is None and row["run_task_id"] is None
    assert row["last_run_result"] == "outcome_unknown"
    assert row["run_count"] == 1
    history = restarted.get_job_history(job_id)
    assert len(history) == 1
    assert history[0]["delivery_status"] == "outcome_unknown"
    assert history[0]["task_id"] == "task-7"
    assert "restart" in history[0]["error"].lower()
    if one_shot:
        assert row["status"] == "paused", "an interrupted one-shot is consumed, not re-armed"
        assert row["next_run_at"] is None
    else:
        assert row["status"] == "active"
        assert datetime.fromisoformat(row["next_run_at"]) > datetime.now(LOCAL_TZ)
    alerts = [c for c in rec.calls if c.get("category") == "alert"]
    assert len(alerts) == 1
    assert "outcome unknown" in alerts[0]["title"].lower()


def test_marker_and_a_genuinely_missed_fire_catch_up_once(db_path, monkeypatch):
    """The box was down across the run AND the job's next fire: the interrupted
    run is outcome_unknown, and the fire that was missed while down still gets
    its single catch-up."""
    monkeypatch.setattr(manager_mod, "notify", _Notify())
    mgr = CronJobManager()
    job = _sms_job(mgr)
    long_ago = datetime.now(LOCAL_TZ) - timedelta(days=2)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE cron_jobs SET run_started_at = ?, run_task_id = ?, next_run_at = ? WHERE id = ?",
        (long_ago.isoformat(), "task-9", (long_ago + timedelta(days=1)).isoformat(), job["id"]),
    )
    conn.commit()
    conn.close()

    calls = []

    async def _fake_execute(jid):
        calls.append(jid)

    restarted = CronJobManager()
    monkeypatch.setattr(restarted, "_execute_job", _fake_execute)
    asyncio.run(_start_then_stop(restarted))

    assert calls == [job["id"]]
    assert restarted.get_job_history(job["id"])[0]["delivery_status"] == "outcome_unknown"


def test_scheduler_awaits_the_run_on_its_loop(db_path):
    """No thread hop and no 300/660 s wrapper timeout: APScheduler awaits the
    async _execute_job directly, so a long run is never logged as an exception
    or freed from max_instances while it is still going."""
    async def _run():
        mgr = CronJobManager()
        job = mgr.create_job(name="j", prompt="hi", schedule="0 15 * * *", operator="system")
        await mgr.start()
        try:
            scheduled = mgr.scheduler.get_job(job["id"])
            assert asyncio.iscoroutinefunction(scheduled.func)
            assert scheduled.func == mgr._execute_job
        finally:
            await mgr.shutdown()

    asyncio.run(_run())
    assert not hasattr(CronJobManager, "_execute_job_sync_wrapper")


def test_pausing_via_update_clears_next_run_at(db_path):
    """update_job(status='paused') (the ToolVault pause flag's path) left a
    stale next_run_at, unlike pause_job (O15-scheduler-admin-13)."""
    mgr = CronJobManager()
    job = mgr.create_job(name="j", prompt="hi", schedule="0 15 * * *", operator="system")
    assert job["next_run_at"]
    paused = mgr.update_job(job["id"], status="paused")
    assert paused["next_run_at"] is None


@pytest.mark.asyncio
async def test_run_now_on_a_paused_job_keeps_next_run_at_empty(db_path, monkeypatch):
    mgr = CronJobManager()
    job = mgr.create_job(name="j", prompt="hi", schedule="0 15 * * *", operator="system")
    mgr.pause_job(job["id"])

    async def _ok(j):
        return "done"

    monkeypatch.setattr(executor_mod, "execute_cron_job", _ok)
    await mgr.run_job_now(job["id"])
    assert mgr.get_job(job["id"])["next_run_at"] is None
