"""
A cron turn that ran is never submitted again (audit O15-scheduler-admin-02).

The poll-timeout fix made a run whose end was not seen 'outcome_unknown'. Three
other ways out of a turn that had already run were still plain RuntimeErrors,
which the manager retries, so one fire could send the same SMS three times:

  * the /chat task COMPLETED but its reply text was empty (a model that ends its
    turn on a tool result, or call_anthropic's tool-iteration cap), although
    result_data.tool_outcomes showed the send_sms that went out;
  * the /chat task FAILED after a tool round (a 529 on the provider request
    that follows a send_sms round);
  * a computer-use stream reported an error after the agent had acted on the
    desktop.

Each is now executor.CronTurnIncomplete: recorded once ('partial' when a tool
that did real work succeeded, else 'error'), never retried, and a one-shot is
paused like any run that ran. A failure that provably happened before any tool
ran (a FAILED task with no tool calls, a CU error before its first action) is
still retried. A cancelled task is not retried either, and is recorded as
'cancelled', not as a failure.
"""

import asyncio
import json

import pytest

from Orchestrator.scheduler import executor as executor_mod
from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler.manager import CronJobManager

PHONE = "+15550000000"  # reserved-style test number, never dialled

SMS_OK = {"tool": "send_sms", "success": True, "key": "a1"}
SMS_FAILED = {"tool": "send_sms", "success": False, "key": "a1", "error": "span down"}
DISCOVERY = {"tool": "toolvault", "success": True, "key": "d1", "incidental": True}


@pytest.fixture()
def temp_manager(tmp_path, monkeypatch):
    monkeypatch.setattr(manager_mod, "DB_PATH", tmp_path / "cron_turn_ran.db")
    monkeypatch.setattr(manager_mod, "RETRY_BACKOFF_SECONDS", [0] * (manager_mod.MAX_RETRIES + 1))
    rec = _Notify()
    monkeypatch.setattr(manager_mod, "notify", rec)
    mgr = CronJobManager()
    mgr._notify_recorder = rec
    return mgr


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


def _install_chat(monkeypatch, task_payload):
    """A stub /chat whose task immediately reports ``task_payload``; records every POST."""
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
            return _Resp({"task_id": f"task-{len(posts)}"})

        def get(self, url, timeout=None):
            return _Resp(task_payload)

    monkeypatch.setattr(executor_mod.aiohttp, "ClientSession", _Session)
    monkeypatch.setattr(executor_mod, "_POLL_INTERVAL_SECS", 0)
    return posts


def _sms_job(mgr, **over):
    params = dict(name="briefing", prompt="news", schedule="0 7 * * *",
                  operator="alice", delivery="sms", delivery_target=PHONE)
    params.update(over)
    return mgr.create_job(**params)


def _completed(**result_data):
    return {"status": "completed", "result_data": result_data}


def _failed(error, **result_data):
    data = {"error": error, "trace": "Traceback ..."}
    data.update(result_data)
    return {"status": "failed", "error_message": error, "result_data": data}


# ---------------------------------------------------------------------------
# A COMPLETED turn is never retried, whatever its reply looks like
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_completed_turn_with_sms_sent_but_empty_reply_is_submitted_once(
    temp_manager, monkeypatch
):
    posts = _install_chat(monkeypatch, _completed(ui_reply="", reply="", text="",
                                                  tool_outcomes=[SMS_OK]))
    job = _sms_job(temp_manager)

    await temp_manager._execute_job(job["id"])

    assert len(posts) == 1, f"a completed turn whose SMS went out was re-submitted {len(posts)} times"
    history = temp_manager.get_job_history(job["id"])
    assert len(history) == 1
    row = history[0]
    # The SMS went out, the reply is missing: partly done, never 'delivered'.
    assert row["delivery_status"] == "partial"
    assert "no reply" in row["error"].lower()
    assert "send_sms" in row["error"]
    assert row["task_id"] == "task-1"
    refreshed = temp_manager.get_job(job["id"])
    assert refreshed["last_run_result"] == "partial"
    assert refreshed["run_count"] == 1


@pytest.mark.asyncio
async def test_completed_turn_with_no_tools_and_empty_reply_is_an_error_not_retried(
    temp_manager, monkeypatch
):
    posts = _install_chat(monkeypatch, _completed(ui_reply="", tool_outcomes=[]))
    job = _sms_job(temp_manager, delivery="snapshot", delivery_target=None)

    await temp_manager._execute_job(job["id"])

    assert len(posts) == 1
    history = temp_manager.get_job_history(job["id"])
    assert [h["delivery_status"] for h in history] == ["error"]
    assert temp_manager.get_job(job["id"])["last_run_result"] == "error"


@pytest.mark.asyncio
async def test_completed_task_without_result_data_is_not_retried(temp_manager, monkeypatch):
    posts = _install_chat(monkeypatch, {"status": "completed", "result_data": None})
    job = _sms_job(temp_manager)

    await temp_manager._execute_job(job["id"])

    assert len(posts) == 1
    assert [h["delivery_status"] for h in temp_manager.get_job_history(job["id"])] == ["error"]


@pytest.mark.asyncio
async def test_a_one_shot_whose_completed_turn_wrote_no_reply_is_paused(temp_manager, monkeypatch):
    _install_chat(monkeypatch, _completed(ui_reply="", tool_outcomes=[SMS_OK]))
    job = _sms_job(temp_manager, one_shot=True)

    await temp_manager._execute_job(job["id"])

    refreshed = temp_manager.get_job(job["id"])
    assert refreshed is not None and refreshed["status"] == "paused"
    assert refreshed["next_run_at"] is None


def test_executor_raises_turn_incomplete_for_an_empty_completed_reply(monkeypatch):
    _install_chat(monkeypatch, _completed(ui_reply="  ", tool_outcomes=[SMS_OK]))
    job = {"id": "cron_x", "name": "briefing", "prompt": "news", "operator": "alice",
           "delivery": "sms", "delivery_target": PHONE, "model": "", "provider": "google"}
    with pytest.raises(executor_mod.CronTurnIncomplete) as info:
        asyncio.run(executor_mod.execute_cron_job(job))
    assert info.value.retryable is False
    assert info.value.delivery_status == "partial"


# ---------------------------------------------------------------------------
# A FAILED task: retried only when no tool call ran
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("outcomes, status", [
    ([SMS_OK], "partial"),          # the SMS went out, then the next provider round 529'd
    ([SMS_FAILED], "error"),        # a delivery tool ran; it may have had effects
    ([DISCOVERY, SMS_OK], "partial"),
])
async def test_failed_task_after_a_tool_call_is_not_resubmitted(
    temp_manager, monkeypatch, outcomes, status
):
    posts = _install_chat(monkeypatch, _failed("529 overloaded_error", tool_outcomes=outcomes))
    job = _sms_job(temp_manager)

    await temp_manager._execute_job(job["id"])

    assert len(posts) == 1, f"a turn that already called send_sms was re-submitted {len(posts)} times"
    history = temp_manager.get_job_history(job["id"])
    assert len(history) == 1
    assert history[0]["delivery_status"] == status
    assert "529" in history[0]["error"]
    assert "send_sms" in history[0]["error"]
    assert temp_manager.get_job(job["id"])["last_run_result"] == status


@pytest.mark.asyncio
@pytest.mark.parametrize("result_data", [
    {"tool_outcomes": []},            # the provider refused the first request
    {"tool_outcomes": [DISCOVERY]},   # only tool discovery ran: no side effects
    {},                               # failed before the tool log was opened
])
async def test_failed_task_before_any_tool_ran_is_still_retried(
    temp_manager, monkeypatch, result_data
):
    posts = _install_chat(monkeypatch, _failed("529 overloaded_error", **result_data))
    job = _sms_job(temp_manager)

    await temp_manager._execute_job(job["id"])

    assert len(posts) == manager_mod.MAX_RETRIES + 1
    history = temp_manager.get_job_history(job["id"])
    assert [h["delivery_status"] for h in history] == ["error"] * (manager_mod.MAX_RETRIES + 1)


def test_chat_worker_publishes_tool_outcomes_on_failure(monkeypatch, tmp_path):
    """process_chat_task carries the turn's tool log into a FAILED result_data,
    so the scheduler can tell a turn that failed after send_sms from one that
    never reached a tool."""
    from concurrent.futures import ThreadPoolExecutor

    import Orchestrator.tasks as tasks
    from Orchestrator import tool_outcomes
    from Orchestrator.models import Task, TaskDatabase, TaskStatus, TaskType
    from Orchestrator.routes import chat_routes
    from Orchestrator.toolvault.context import ToolResult
    from Orchestrator.volume import now_utc_iso

    def fake_call_anthropic(messages, model, operator="x"):
        tool_outcomes.record("send_sms", ToolResult(True, "sent"), {"to": "x", "message": "hi"})
        raise RuntimeError("Anthropic API returned 529: overloaded_error")

    import Orchestrator.toolvault.embeddings as tv_embeddings

    db = TaskDatabase(str(tmp_path / "tasks.db"))
    monkeypatch.setattr(tasks, "task_db", db)
    monkeypatch.setattr(chat_routes, "call_anthropic", fake_call_anthropic)
    # Tool selection embeds the query; keep it off the network (keyword search only).
    monkeypatch.setattr(tv_embeddings, "embed_query", lambda *a, **k: None)
    for name in ("get_recent_fossils_for_operator", "keyword_retrieve_for_operator",
                 "semantic_retrieve", "get_recent_checkpoints_for_operator", "hybrid_retrieve"):
        monkeypatch.setattr(tasks, name, lambda *a, **k: [])
    monkeypatch.setattr(tasks, "read_text_safe", lambda *a, **k: "")
    monkeypatch.setattr(tasks, "AUTO_ENABLE", False)
    monkeypatch.setattr(tasks, "should_create_checkpoint", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "perform_mint", lambda *a, **k: {"snap_id": "SNAP-TEST"})
    monkeypatch.setattr(tasks, "save_operator_state", lambda *a, **k: None)

    task = Task(task_id="t-fail", task_type=TaskType.CHAT, status=TaskStatus.PENDING,
                created_at=now_utc_iso(), updated_at=now_utc_iso(), operator="ToolOutcomeTester",
                result_data={"messages": [{"role": "user", "content": "text the summary"}],
                             "operator": "ToolOutcomeTester", "provider": "anthropic",
                             "model": "claude-test"})
    db.save_task(task)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(tasks.process_chat_task, task).result()
    failed = db.get_task("t-fail")

    assert failed.status == "failed"
    assert "529" in failed.error_message
    assert [(o["tool"], o["success"]) for o in failed.result_data["tool_outcomes"]] == [
        ("send_sms", True)]


# ---------------------------------------------------------------------------
# computer-use: an error after the agent acted is not re-run
# ---------------------------------------------------------------------------

def _install_cu_stream(monkeypatch, events):
    """A stub /chat/stream that replays ``events`` ((type, data) pairs) as SSE."""
    posts = []

    lines = []
    for etype, data in events:
        lines.append(f"event: {etype}\n".encode())
        lines.append(f"data: {json.dumps(data)}\n".encode())
        lines.append(b"\n")

    class _Content:
        def __init__(self):
            self._it = iter(lines)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration

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
            return _StreamResp()

    monkeypatch.setattr(executor_mod.aiohttp, "ClientSession", _Session)
    return posts


def _cu_job(mgr, **over):
    params = dict(name="desk", prompt="check the dashboard", schedule="0 7 * * *",
                  operator="alice", provider="computer-use", model="")
    params.update(over)
    return mgr.create_job(**params)


@pytest.mark.asyncio
@pytest.mark.parametrize("acted", [
    ("cu_action", {"action": "left_click", "step": 1}),
    ("cu_bash_output", {"output": "ok", "step": 1}),
    ("cu_file_edit", {"path": "/tmp/x", "step": 1}),
    ("cu_step", {"step": 2, "total": 50}),   # step 2 begins: step 1's actions ran
])
async def test_cu_error_after_the_agent_acted_is_not_rerun(temp_manager, monkeypatch, acted):
    posts = _install_cu_stream(monkeypatch, [
        ("cu_step", {"step": 1, "total": 50}),
        acted,
        ("error", "provider 529 overloaded"),
    ])
    job = _cu_job(temp_manager, one_shot=True)

    await temp_manager._execute_job(job["id"])

    assert len(posts) == 1, f"a CU run that had acted was launched {len(posts)} times"
    history = temp_manager.get_job_history(job["id"])
    assert [h["delivery_status"] for h in history] == ["error"]
    assert "529" in history[0]["error"]
    refreshed = temp_manager.get_job(job["id"])
    assert refreshed["status"] == "paused", "the desktop task ran; the one-shot must not re-arm"


@pytest.mark.asyncio
async def test_cu_error_before_any_action_is_still_retried(temp_manager, monkeypatch):
    posts = _install_cu_stream(monkeypatch, [
        ("cu_step", {"step": 1, "total": 50}),
        ("error", "provider 529 overloaded"),
    ])
    job = _cu_job(temp_manager)

    await temp_manager._execute_job(job["id"])

    assert len(posts) == manager_mod.MAX_RETRIES + 1


def test_cu_credits_exhausted_after_acting_is_still_a_billing_refusal(monkeypatch):
    _install_cu_stream(monkeypatch, [
        ("cu_step", {"step": 1, "total": 50}),
        ("cu_action", {"action": "screenshot", "step": 1}),
        ("error_detail", {"kind": "credits_exhausted"}),
        ("error", "credit balance is too low"),
    ])
    job = {"id": "cron_cu", "name": "desk", "prompt": "p", "operator": "alice",
           "delivery": "snapshot", "delivery_target": "", "model": "",
           "provider": "computer-use"}
    with pytest.raises(executor_mod.CronCreditsExhausted):
        asyncio.run(executor_mod.execute_cron_job(job))


# ---------------------------------------------------------------------------
# A cancelled run is the operator's decision: not retried, not a failure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_cancelled_run_is_recorded_cancelled_and_a_one_shot_is_paused(
    temp_manager, monkeypatch
):
    posts = _install_chat(monkeypatch, {"status": "cancelled"})
    job = _sms_job(temp_manager, one_shot=True)

    await temp_manager._execute_job(job["id"])

    assert len(posts) == 1
    history = temp_manager.get_job_history(job["id"])
    assert [h["delivery_status"] for h in history] == ["cancelled"]
    refreshed = temp_manager.get_job(job["id"])
    assert refreshed is not None
    # The task may have sent its SMS before the cancel landed: keep the
    # one-shot, but never let it fire again on its own.
    assert refreshed["status"] == "paused"
    assert refreshed["last_run_result"] == "cancelled"
    assert refreshed["error_count"] == 0
    alerts = [c for c in temp_manager._notify_recorder.calls if c.get("category") == "alert"]
    assert len(alerts) == 1
    assert "cancelled" in alerts[0]["title"].lower()


@pytest.mark.asyncio
async def test_a_cancelled_recurring_run_stays_active(temp_manager, monkeypatch):
    _install_chat(monkeypatch, {"status": "cancelled"})
    job = _sms_job(temp_manager)

    await temp_manager._execute_job(job["id"])

    refreshed = temp_manager.get_job(job["id"])
    assert refreshed["status"] == "active"
    assert refreshed["last_run_result"] == "cancelled"
    assert refreshed["error_count"] == 0
