"""
A cron run whose end the executor could not see is 'outcome_unknown', and is
never retried (audit O15-scheduler-admin-02, -17).

The executor POSTs the job to /chat and polls the task. When the poll budget
ran out it used to raise a plain RuntimeError, the manager retried, and every
retry POSTed a NEW /chat task carrying the same DELIVERY instruction, while the
first task kept running and sent its SMS or placed its call anyway: one fire
could deliver three times and still be recorded as failed. The same holds for
a POST that timed out after it reached the box (the executor talks to its own
event loop; a timeout there means a busy loop, not a refused request) and for
a computer-use stream that timed out after the run began.

Now each of those raises CronOutcomeUnknown, which the manager records once
('outcome_unknown', with the task id), alerts on honestly, and does not retry.
A failure that provably happened before anything ran (connection refused) is
still retried.
"""

import asyncio
import json

import aiohttp
import pytest

from Orchestrator.scheduler import executor as executor_mod
from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler.manager import CronJobManager

PHONE = "+15550000000"  # reserved-style test number, never dialled


@pytest.fixture()
def temp_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_mod, "DB_PATH", tmp_path / "cron_unknown.db")
    monkeypatch.setattr(manager_mod, "RETRY_BACKOFF_SECONDS", [0] * (manager_mod.MAX_RETRIES + 1))
    return CronJobManager()


class _Notify:
    def __init__(self):
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)


def _install_chat(monkeypatch, *, status="processing", post_error=None):
    """A stub /chat whose task never leaves ``status``; records every POST."""
    posts = []

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, timeout=None):
            posts.append(json)
            if post_error is not None:
                raise post_error
            return _Resp({"task_id": f"task-{len(posts)}"})

        def get(self, url, timeout=None):
            return _Resp({"status": status, "progress": 10})

    monkeypatch.setattr(executor_mod.aiohttp, "ClientSession", _Session)
    monkeypatch.setattr(executor_mod, "_POLL_INTERVAL_SECS", 0)
    monkeypatch.setattr(executor_mod, "_POLL_TIMEOUT_SECS", 0.05)
    return posts


def _sms_job(mgr, **over):
    params = dict(name="briefing", prompt="news", schedule="0 7 * * *",
                  operator="alice", delivery="sms", delivery_target=PHONE)
    params.update(over)
    return mgr.create_job(**params)


# ---------------------------------------------------------------------------
# executor
# ---------------------------------------------------------------------------

def test_poll_timeout_raises_outcome_unknown_with_task_id(monkeypatch):
    _install_chat(monkeypatch)
    job = {"id": "cron_x", "name": "briefing", "prompt": "news", "operator": "alice",
           "delivery": "sms", "delivery_target": PHONE, "model": "", "provider": "google"}
    with pytest.raises(executor_mod.CronOutcomeUnknown) as info:
        asyncio.run(executor_mod.execute_cron_job(job))
    err = info.value
    assert err.task_id == "task-1"
    assert err.retryable is False
    assert err.delivery_status == "outcome_unknown"
    assert "task-1" in str(err) and "outcome unknown" in str(err).lower()


def test_post_timeout_raises_outcome_unknown(monkeypatch):
    _install_chat(monkeypatch, post_error=asyncio.TimeoutError())
    job = {"id": "cron_x", "name": "briefing", "prompt": "news", "operator": "alice",
           "delivery": "sms", "delivery_target": PHONE, "model": "", "provider": "google"}
    with pytest.raises(executor_mod.CronOutcomeUnknown) as info:
        asyncio.run(executor_mod.execute_cron_job(job))
    assert str(info.value), "the reason must never be empty (O15-17)"
    assert info.value.task_id is None


def test_connection_refused_is_an_ordinary_retryable_failure(monkeypatch):
    refused = aiohttp.ClientConnectionError("connection refused")
    _install_chat(monkeypatch, post_error=refused)
    job = {"id": "cron_x", "name": "briefing", "prompt": "news", "operator": "alice",
           "delivery": "sms", "delivery_target": PHONE, "model": "", "provider": "google"}
    with pytest.raises(RuntimeError) as info:
        asyncio.run(executor_mod.execute_cron_job(job))
    assert not isinstance(info.value, executor_mod.CronOutcomeUnknown)
    assert getattr(info.value, "retryable", True) is True


def test_cancelled_task_is_not_retried(monkeypatch):
    _install_chat(monkeypatch, status="cancelled")
    job = {"id": "cron_x", "name": "briefing", "prompt": "news", "operator": "alice",
           "delivery": "snapshot", "delivery_target": "", "model": "", "provider": "google"}
    with pytest.raises(RuntimeError) as info:
        asyncio.run(executor_mod.execute_cron_job(job))
    assert "cancelled" in str(info.value)
    assert getattr(info.value, "retryable", True) is False


def test_task_id_reaches_the_run_sink(monkeypatch):
    """The manager learns the /chat task id the moment the POST returns."""
    _install_chat(monkeypatch)
    seen = []
    job = {"id": "cron_x", "name": "briefing", "prompt": "news", "operator": "alice",
           "delivery": "sms", "delivery_target": PHONE, "model": "", "provider": "google"}

    async def _run():
        token = executor_mod.RUN_TASK_ID_SINK.set(seen.append)
        try:
            await executor_mod.execute_cron_job(job)
        finally:
            executor_mod.RUN_TASK_ID_SINK.reset(token)

    with pytest.raises(executor_mod.CronOutcomeUnknown):
        asyncio.run(_run())
    assert seen == ["task-1"]


def _config_with(text):
    import configparser

    cfg = configparser.ConfigParser()
    cfg.read_string(text)
    return cfg


def test_poll_budget_is_minutes_not_three(monkeypatch):
    """180 s was shorter than an ordinary tool-using turn. Checks the code's
    default, not this box's config.ini (an operator may set the knob)."""
    import Orchestrator.config as config

    monkeypatch.setattr(config, "CFG", _config_with(""))
    assert executor_mod._poll_timeout_from_config() == 1800


@pytest.mark.parametrize("value, expected", [("600", 600), ("5", 60), ("soon", 1800)])
def test_poll_budget_knob(monkeypatch, value, expected):
    """[scheduler] poll_timeout_s is honoured, floored at 60 s; junk falls back."""
    import Orchestrator.config as config

    monkeypatch.setattr(config, "CFG", _config_with(f"[scheduler]\npoll_timeout_s = {value}\n"))
    assert executor_mod._poll_timeout_from_config() == expected


# ---------------------------------------------------------------------------
# manager: one submission, honest record, no retry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("delivery, target", [
    ("sms", PHONE), ("voice_call", PHONE), ("snapshot", None), ("notification", None),
])
async def test_never_completing_chat_is_submitted_exactly_once(
    temp_manager, monkeypatch, delivery, target
):
    posts = _install_chat(monkeypatch)
    rec = _Notify()
    monkeypatch.setattr(manager_mod, "notify", rec)
    job = _sms_job(temp_manager, delivery=delivery, delivery_target=target)

    await temp_manager._execute_job(job["id"])

    assert len(posts) == 1, f"a timed-out run must not be re-submitted; got {len(posts)} POSTs"
    history = temp_manager.get_job_history(job["id"])
    assert len(history) == 1
    row = history[0]
    assert row["delivery_status"] == "outcome_unknown"
    assert row["task_id"] == "task-1"
    assert "outcome unknown" in row["error"].lower()

    refreshed = temp_manager.get_job(job["id"])
    assert refreshed["last_run_result"] == "outcome_unknown"
    assert refreshed["run_count"] == 1
    assert refreshed["error_count"] == 0  # not a known failure
    assert refreshed["run_started_at"] is None and refreshed["run_task_id"] is None

    alerts = [c for c in rec.calls if c.get("category") == "alert"]
    assert len(alerts) == 1
    assert "outcome unknown" in alerts[0]["title"].lower()
    assert "not retried" in alerts[0]["body"].lower()


@pytest.mark.asyncio
async def test_outcome_unknown_one_shot_is_paused_not_rearmed(temp_manager, monkeypatch):
    _install_chat(monkeypatch)
    monkeypatch.setattr(manager_mod, "notify", _Notify())
    job = _sms_job(temp_manager, one_shot=True)

    await temp_manager._execute_job(job["id"])

    refreshed = temp_manager.get_job(job["id"])
    assert refreshed is not None, "an outcome-unknown one-shot is kept, not deleted"
    assert refreshed["status"] == "paused"
    assert refreshed["next_run_at"] is None


@pytest.mark.asyncio
async def test_empty_exception_text_still_names_the_failure(temp_manager, monkeypatch):
    """A bare exception class used to leave 'failed ()' in the alert (O15-17)."""
    async def _boom(job):
        raise TimeoutError()

    monkeypatch.setattr(executor_mod, "execute_cron_job", _boom)
    monkeypatch.setattr(manager_mod, "notify", _Notify())
    job = _sms_job(temp_manager, delivery="snapshot", delivery_target=None)
    await temp_manager._execute_job(job["id"])
    assert all(h["error"] for h in temp_manager.get_job_history(job["id"]))


def test_oversight_digest_reports_outcome_unknown_runs(monkeypatch, isolated_stores):
    """The Flight Recorder's health digest surfaces them next to failures."""
    import Orchestrator.scheduler as scheduler_pkg
    from Orchestrator import oversight

    class _Sched:
        def get_job(self, job_id):
            return object()

    class _Mgr:
        scheduler = _Sched()

        def list_jobs(self):
            return [{"id": "cron_x", "name": "briefing", "status": "active"}]

        def get_job_history(self, job_id, limit=5):
            return [{"run_at": "2026-10-20T07:30:00", "delivery_status": "outcome_unknown",
                     "error": "outcome unknown: chat task t-1 was still processing"}]

    monkeypatch.setattr(scheduler_pkg, "get_scheduler_manager", lambda: _Mgr())
    failures = oversight.collect_oversight_signals()["cron"]["recent_failures"]
    assert [(f["run_at"], f["outcome"]) for f in failures] == [
        ("2026-10-20T07:30:00", "outcome_unknown")]


# ---------------------------------------------------------------------------
# computer-use: a stream that began and then timed out
# ---------------------------------------------------------------------------

def _install_cu_stream(monkeypatch, *, fail_before_headers=False):
    posts = []

    class _Content:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise asyncio.TimeoutError()

    class _StreamResp(_Resp):
        def __init__(self):
            super().__init__({}, 200)
            self.content = _Content()

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, timeout=None):
            posts.append(json)
            if fail_before_headers:
                raise aiohttp.ClientConnectionError("connection refused")
            return _StreamResp()

    monkeypatch.setattr(executor_mod.aiohttp, "ClientSession", _Session)
    return posts


def _cu_job():
    return {"id": "cron_cu", "name": "desk", "prompt": "check the dashboard",
            "operator": "alice", "delivery": "snapshot", "delivery_target": "",
            "model": "", "provider": "computer-use"}


def test_cu_stream_timeout_after_start_is_outcome_unknown(monkeypatch):
    _install_cu_stream(monkeypatch)
    with pytest.raises(executor_mod.CronOutcomeUnknown):
        asyncio.run(executor_mod.execute_cron_job(_cu_job()))


def test_cu_connect_failure_is_retryable(monkeypatch):
    _install_cu_stream(monkeypatch, fail_before_headers=True)
    with pytest.raises(RuntimeError) as info:
        asyncio.run(executor_mod.execute_cron_job(_cu_job()))
    assert not isinstance(info.value, getattr(executor_mod, "CronOutcomeUnknown", ()))


def test_cu_stream_budget_covers_the_cu_session(monkeypatch):
    """The stream gave up at a fixed 600 s while a CU session may run its
    configured budget (1800 s on this box) plus the 30 s backstop."""
    import Orchestrator.config as config
    monkeypatch.setattr(config, "CU_SESSION_TIMEOUT", 1800)
    assert executor_mod._cu_stream_timeout() > 1800 + 30
    monkeypatch.setattr(config, "CU_SESSION_TIMEOUT", 60)
    assert executor_mod._cu_stream_timeout() >= 600  # never below the old floor
