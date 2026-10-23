"""Cron runs whose tool calls failed are recorded as failed (audit W15).

On 2026-09-12 (and again on 2026-10-12) a monthly calendar job logged
``result=success`` although BOTH of its list_events calls had failed with
``invalid_grant``: the scheduler only looked at whether the /chat task finished
and produced text, and the model had politely written a reply about the error.

The fix, pinned here end to end:

  1. ``BlackBoxToolExecutor.execute`` (the one choke point every chat-loop tool
     call goes through) appends each call's outcome to a per-turn log
     (``Orchestrator.tool_outcomes``) when one is open;
  2. the /chat worker (``tasks.process_chat_task``) opens a fresh log per task
     and publishes it as ``result_data["tool_outcomes"]``;
  3. the cron executor reads it: a failed call that a later call of the same tool
     WITH THE SAME ARGUMENTS did not recover makes the run FAILED (no real tool
     succeeded) or PARTIAL (some did), carrying the tool error; "no results" is
     a successful call and never counts, and tool discovery or the clock
     succeeding does not make a run partial;
  4. the manager records that verdict (history row + last_run_result +
     error_count + one alert) and does NOT retry it — the turn completed and was
     minted, and re-running it would repeat any side effects that did succeed.
     For the same reason a one-shot is paused rather than left to fire again,
     and a delivery='notification' job still delivers the reply it produced.
"""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from Orchestrator.toolvault.context import ToolResult


INVALID_GRANT = (
    "Error executing list_events: ('invalid_grant: Bad Request', "
    "{'error': 'invalid_grant', 'error_description': 'Bad Request'})"
)
SPAN_DOWN = "TG200 SMS failed: span down"


# ---------------------------------------------------------------------------
# 1. The per-turn outcome log
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_tools(monkeypatch):
    """Route registry.get_executor to canned executors (no real tools run)."""
    from Orchestrator.toolvault import registry

    async def list_events_dead(params, ctx):
        return ToolResult(False, INVALID_GRANT)

    async def list_events_empty(params, ctx):
        return ToolResult(True, "[]")

    async def send_sms_ok(params, ctx):
        # The first chunk of a multi-part broadcast hits a dead span; the rest go out.
        if "(1/3)" in str(params.get("message", "")):
            return ToolResult(False, SPAN_DOWN)
        return ToolResult(True, "sent")

    async def toolvault_ok(params, ctx):
        return ToolResult(True, "send_sms: send a text message")

    async def clock_ok(params, ctx):
        return ToolResult(True, "2026-10-12T09:00:00-04:00")

    async def boom(params, ctx):
        raise RuntimeError("executor crashed")

    table = {
        "list_events": list_events_dead,
        "list_events_empty": list_events_empty,
        "send_sms": send_sms_ok,
        "toolvault": toolvault_ok,
        "get_current_time": clock_ok,
        "boom": boom,
    }
    monkeypatch.setattr(registry, "get_executor", lambda name: table.get(name))
    return table


def _run_tool(name, operator="Robin", args=None):
    from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor
    return asyncio.run(BlackBoxToolExecutor(operator=operator).execute(name, dict(args or {})))


def test_executor_records_every_outcome_into_the_open_log(fake_tools):
    from Orchestrator import tool_outcomes

    log = tool_outcomes.begin()
    try:
        _run_tool("list_events")
        _run_tool("send_sms")
        _run_tool("no_such_tool")
        _run_tool("boom")
    finally:
        tool_outcomes.end()

    assert [(e["tool"], e["success"]) for e in log] == [
        ("list_events", False), ("send_sms", True), ("no_such_tool", False), ("boom", False),
    ]
    assert "invalid_grant" in log[0]["error"]
    assert "error" not in log[1]
    assert "executor crashed" in log[3]["error"]


def test_without_an_open_log_recording_is_a_no_op(fake_tools):
    from Orchestrator import tool_outcomes

    tool_outcomes.end()
    result = _run_tool("list_events")
    assert result.success is False  # the call itself is unaffected


def test_the_log_reaches_tools_run_by_asyncio_run_on_a_worker_thread(fake_tools):
    """The /chat worker runs on a pooled thread and the call_* loops run each tool
    with asyncio.run — the log opened on the thread must see those calls."""
    from Orchestrator import tool_outcomes

    def worker():
        log = tool_outcomes.begin()
        try:
            _run_tool("list_events")
            return list(log)
        finally:
            tool_outcomes.end()

    with ThreadPoolExecutor(max_workers=1) as pool:
        entries = pool.submit(worker).result()
    assert [e["tool"] for e in entries] == ["list_events"]


def test_each_outcome_carries_a_fingerprint_of_its_arguments(fake_tools):
    """Same arguments (in any key order) -> same key; other arguments -> other key."""
    from Orchestrator import tool_outcomes

    log = tool_outcomes.begin()
    try:
        _run_tool("send_sms", args={"phone_number": "+15550000001", "message": "DAILY (2/3)"})
        _run_tool("send_sms", args={"message": "DAILY (2/3)", "phone_number": "+15550000001"})
        _run_tool("send_sms", args={"phone_number": "+15550000001", "message": "DAILY (3/3)"})
        _run_tool("send_sms", args={"phone_number": "+15550000002", "message": "DAILY (2/3)"})
    finally:
        tool_outcomes.end()

    keys = [e["key"] for e in log]
    assert all(isinstance(k, str) and k for k in keys)
    assert keys[0] == keys[1]
    assert len({keys[0], keys[2], keys[3]}) == 3


def test_discovery_and_clock_calls_are_marked_incidental(fake_tools):
    from Orchestrator import tool_outcomes

    log = tool_outcomes.begin()
    try:
        _run_tool("toolvault", args={"action": "search", "query": "send sms"})
        _run_tool("toolvault", args={"action": "read", "tool_name": "send_sms"})
        _run_tool("get_current_time")
        _run_tool("send_sms", args={"phone_number": "+15550000001", "message": "hi"})
        _run_tool("list_events_empty")
    finally:
        tool_outcomes.end()

    assert [bool(e.get("incidental")) for e in log] == [True, True, True, False, False]


def test_error_text_is_bounded():
    from Orchestrator import tool_outcomes

    log = tool_outcomes.begin()
    try:
        tool_outcomes.record("list_events", ToolResult(False, "x" * 10_000))
    finally:
        tool_outcomes.end()
    assert len(log[0]["error"]) <= tool_outcomes.ERROR_CHARS


@pytest.mark.parametrize("outcomes, expected", [
    ([], None),
    ([{"tool": "list_events", "success": True}], None),
    # a failed call recovered by a later call of the same tool with the SAME
    # arguments (a plain retry) is not a failure
    ([{"tool": "list_events", "success": False, "key": "k1", "error": "rate limited"},
      {"tool": "list_events", "success": True, "key": "k1"}], None),
    # failure only -> failed
    ([{"tool": "list_events", "success": False, "error": INVALID_GRANT},
      {"tool": "list_events", "success": False, "error": INVALID_GRANT}],
     {"partial": False, "tools": ["list_events", "list_events"]}),
    # failure + another tool that worked -> partial
    ([{"tool": "list_events", "success": False, "error": INVALID_GRANT},
      {"tool": "send_sms", "success": True}],
     {"partial": True, "tools": ["list_events"]}),
    # a success BEFORE the failure does not recover it
    ([{"tool": "list_events", "success": True},
      {"tool": "list_events", "success": False, "error": INVALID_GRANT}],
     {"partial": True, "tools": ["list_events"]}),
    # a later success of the same tool with OTHER arguments does not hide the
    # failure (chunk 1/3 of a broadcast failed, 2/3 and 3/3 went out)
    ([{"tool": "send_sms", "success": False, "key": "chunk1", "error": SPAN_DOWN},
      {"tool": "send_sms", "success": True, "key": "chunk2"},
      {"tool": "send_sms", "success": True, "key": "chunk3"}],
     {"partial": True, "tools": ["send_sms"]}),
    # discovery + clock succeeding while the real work failed is a FAILED run
    ([{"tool": "toolvault", "success": True, "key": "s", "incidental": True},
      {"tool": "toolvault", "success": True, "key": "r", "incidental": True},
      {"tool": "send_sms", "success": False, "key": "a", "error": SPAN_DOWN}],
     {"partial": False, "tools": ["send_sms"]}),
    # a discovery call corrected by the model (other arguments) left nothing
    # undone: discovery has no side effects
    ([{"tool": "toolvault", "success": False, "key": "q0", "incidental": True,
       "error": "Missing 'query' parameter for search action."},
      {"tool": "toolvault", "success": True, "key": "q1", "incidental": True},
      {"tool": "send_sms", "success": True, "key": "a"}], None),
    # entries without an argument key (unknown arguments) are never assumed
    # to be the same call
    ([{"tool": "send_sms", "success": False, "error": "SMS to +15550000001 failed"},
      {"tool": "send_sms", "success": True}],
     {"partial": True, "tools": ["send_sms"]}),
    # ...but a discovery failure nothing recovered still counts
    ([{"tool": "toolvault", "success": False, "key": "q0", "incidental": True,
       "error": "toolvault search unavailable"}],
     {"partial": False, "tools": ["toolvault"]}),
    # the W15 run itself: the clock worked, both calendar reads hit invalid_grant
    ([{"tool": "get_current_time", "success": True, "key": "t", "incidental": True},
      {"tool": "list_events", "success": False, "key": "a", "error": INVALID_GRANT},
      {"tool": "list_events", "success": False, "key": "b", "error": INVALID_GRANT}],
     {"partial": False, "tools": ["list_events", "list_events"]}),
])
def test_verdict(outcomes, expected):
    from Orchestrator import tool_outcomes

    got = tool_outcomes.verdict(outcomes)
    if expected is None:
        assert got is None
    else:
        assert got["partial"] is expected["partial"]
        assert [f["tool"] for f in got["failures"]] == expected["tools"]
        assert got["total"] == len(outcomes)


def test_verdict_marks_a_failure_a_differently_argued_call_may_have_corrected():
    """A later success with other arguments may be the model fixing its own
    arguments — or other work entirely. The failure stays, flagged as such."""
    from Orchestrator import tool_outcomes

    got = tool_outcomes.verdict([
        {"tool": "list_events", "success": False, "key": "bad", "error": "time_max is required"},
        {"tool": "list_events", "success": True, "key": "good"},
    ])
    assert got["partial"] is True
    assert got["failures"][0]["retried"] is True


# ---------------------------------------------------------------------------
# 2. The /chat worker publishes the log in result_data
# ---------------------------------------------------------------------------

def _run_chat_task(monkeypatch, db, task_id, tool_names, pool):
    import Orchestrator.tasks as tasks
    from Orchestrator.models import Task, TaskStatus, TaskType
    from Orchestrator.routes import chat_routes
    from Orchestrator.volume import now_utc_iso

    def fake_call_anthropic(messages, model, operator="x"):
        for name in tool_names:
            _run_tool(name, operator=operator)
        return "I could not reach your calendar.", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, ""

    monkeypatch.setattr(tasks, "task_db", db)
    monkeypatch.setattr(chat_routes, "call_anthropic", fake_call_anthropic)
    for name in ("get_recent_fossils_for_operator", "keyword_retrieve_for_operator",
                 "semantic_retrieve", "get_recent_checkpoints_for_operator", "hybrid_retrieve"):
        monkeypatch.setattr(tasks, name, lambda *a, **k: [])
    monkeypatch.setattr(tasks, "read_text_safe", lambda *a, **k: "")
    monkeypatch.setattr(tasks, "AUTO_ENABLE", False)
    monkeypatch.setattr(tasks, "should_create_checkpoint", lambda *a, **k: False)
    monkeypatch.setattr(tasks, "perform_mint", lambda *a, **k: {"snap_id": "SNAP-TEST"})
    monkeypatch.setattr(tasks, "save_operator_state", lambda *a, **k: None)

    task = Task(task_id=task_id, task_type=TaskType.CHAT, status=TaskStatus.PENDING,
                created_at=now_utc_iso(), updated_at=now_utc_iso(), operator="ToolOutcomeTester",
                result_data={"messages": [{"role": "user", "content": "check my calendar"}],
                             "operator": "ToolOutcomeTester", "provider": "anthropic",
                             "model": "claude-test"})
    db.save_task(task)
    pool.submit(tasks.process_chat_task, task).result()
    return db.get_task(task_id)


def test_chat_worker_publishes_tool_outcomes(monkeypatch, tmp_path, fake_tools):
    from Orchestrator.models import TaskDatabase

    db = TaskDatabase(str(tmp_path / "tasks.db"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = _run_chat_task(monkeypatch, db, "t-dead", ["list_events", "list_events"], pool)
        # Same pooled thread, next task: it must start with a FRESH log.
        second = _run_chat_task(monkeypatch, db, "t-clean", [], pool)

    assert first.status == "completed", first.error_message
    outcomes = first.result_data["tool_outcomes"]
    assert [(o["tool"], o["success"]) for o in outcomes] == [("list_events", False)] * 2
    assert "invalid_grant" in outcomes[0]["error"]

    assert second.status == "completed", second.error_message
    assert second.result_data["tool_outcomes"] == []


# ---------------------------------------------------------------------------
# 3. The cron executor turns failed tool calls into a failed/partial verdict
# ---------------------------------------------------------------------------

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


def _fake_session_factory(result_data):
    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, timeout=None):
            return _Resp({"task_id": "task-1"})

        def get(self, url, timeout=None):
            return _Resp({"status": "completed", "result_data": result_data})

    return _Session


@pytest.fixture
def cron_chat(monkeypatch):
    from Orchestrator.scheduler import executor as executor_mod

    monkeypatch.setattr(executor_mod, "_POLL_INTERVAL_SECS", 0)

    def install(result_data):
        monkeypatch.setattr(executor_mod.aiohttp, "ClientSession", _fake_session_factory(result_data))
    return install


def _job(**over):
    job = {"id": "cron_bill", "name": "Monthly Bill Invite", "prompt": "make the invite",
           "operator": "Robin", "delivery": "snapshot", "delivery_target": "",
           "model": "gemini-test", "provider": "google"}
    job.update(over)
    return job


def _execute(job):
    from Orchestrator.scheduler import executor as executor_mod
    return asyncio.run(executor_mod.execute_cron_job(job))


REPLY = "I could not reach your calendar."


def test_failed_tool_calls_fail_the_run(cron_chat):
    from Orchestrator.scheduler.executor import CronToolFailure

    cron_chat({"ui_reply": REPLY, "tool_outcomes": [
        {"tool": "list_events", "success": False, "error": INVALID_GRANT},
        {"tool": "list_events", "success": False, "error": INVALID_GRANT},
    ]})

    with pytest.raises(CronToolFailure) as info:
        _execute(_job())
    err = info.value
    assert err.partial is False
    assert err.retryable is False
    assert err.delivery_status == "error"
    assert err.result_text == REPLY
    assert "list_events" in str(err) and "invalid_grant" in str(err)


def test_some_tools_failing_is_a_partial_run(cron_chat):
    from Orchestrator.scheduler.executor import CronToolFailure

    cron_chat({"ui_reply": REPLY, "tool_outcomes": [
        {"tool": "list_events", "success": False, "error": INVALID_GRANT},
        {"tool": "send_sms", "success": True},
    ]})

    with pytest.raises(CronToolFailure) as info:
        _execute(_job())
    assert info.value.partial is True
    assert info.value.delivery_status == "partial"
    assert "list_events" in str(info.value)
    assert "send_sms" not in str(info.value)


@pytest.mark.parametrize("result_data", [
    {"ui_reply": REPLY},                                        # older server / no tools
    {"ui_reply": REPLY, "tool_outcomes": []},                   # no tool calls
    {"ui_reply": REPLY, "tool_outcomes": [                      # "no results" is a success
        {"tool": "list_events", "success": True}]},
    {"ui_reply": REPLY, "tool_outcomes": [                      # recovered retry
        {"tool": "list_events", "success": False, "key": "k1", "error": "rate limited"},
        {"tool": "list_events", "success": True, "key": "k1"}]},
])
def test_healthy_runs_still_succeed(cron_chat, result_data):
    cron_chat(result_data)
    assert _execute(_job()) == REPLY


def _turn_log(fake_tools, calls):
    """Run ``calls`` [(tool, args), ...] through the real executor; return the log."""
    from Orchestrator import tool_outcomes

    log = tool_outcomes.begin()
    try:
        for name, args in calls:
            _run_tool(name, args=args)
    finally:
        tool_outcomes.end()
    return list(log)


def test_a_failed_broadcast_chunk_is_not_hidden_by_the_chunks_that_went_out(cron_chat, fake_tools):
    """The live Daily AI News Briefing shape: discovery, then three send_sms chunks
    to the same number in one turn. Chunk 1/3 failing must not read 'delivered'."""
    from Orchestrator.scheduler.executor import CronToolFailure

    outcomes = _turn_log(fake_tools, [
        ("toolvault", {"action": "search", "query": "send sms"}),
        ("toolvault", {"action": "read", "tool_name": "send_sms"}),
        ("send_sms", {"phone_number": "+15550000001", "message": "DAILY AI NEWS (1/3)"}),
        ("send_sms", {"phone_number": "+15550000001", "message": "DAILY AI NEWS (2/3)"}),
        ("send_sms", {"phone_number": "+15550000001", "message": "DAILY AI NEWS (3/3)"}),
    ])
    cron_chat({"ui_reply": "SMS Broadcast Complete", "tool_outcomes": outcomes})

    with pytest.raises(CronToolFailure) as info:
        _execute(_job())
    err = info.value
    assert err.partial is True
    assert "send_sms" in str(err) and "span down" in str(err)
    assert "other arguments" in str(err)
    assert "toolvault" not in str(err)


def test_the_w15_run_is_a_failed_run_not_a_partial_one(cron_chat, fake_tools):
    """The clock answering does not make a run whose calendar reads all failed 'partial'."""
    from Orchestrator.scheduler.executor import CronToolFailure

    outcomes = _turn_log(fake_tools, [
        ("get_current_time", {}),
        ("list_events", {"calendar_id": "primary", "time_min": "2026-10-01"}),
        ("list_events", {"calendar_id": "primary", "time_min": "2026-10-12"}),
    ])
    cron_chat({"ui_reply": REPLY, "tool_outcomes": outcomes})

    with pytest.raises(CronToolFailure) as info:
        _execute(_job())
    assert info.value.partial is False
    assert info.value.delivery_status == "error"
    assert "list_events (x2)" in str(info.value)


def test_a_plain_retry_with_the_same_arguments_recovers(cron_chat, fake_tools):
    outcomes = [
        {"tool": "list_events", "success": False, "key": "same", "error": "rate limited"},
        {"tool": "list_events", "success": True, "key": "same"},
    ]
    cron_chat({"ui_reply": REPLY, "tool_outcomes": outcomes})
    assert _execute(_job()) == REPLY


# ---------------------------------------------------------------------------
# 4. The manager records the verdict honestly and does not retry it
# ---------------------------------------------------------------------------

class _NotifyRecorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)


@pytest.fixture
def temp_manager(tmp_path, monkeypatch):
    from Orchestrator.scheduler import manager as manager_mod
    from Orchestrator.scheduler.manager import CronJobManager

    monkeypatch.setattr(manager_mod, "DB_PATH", tmp_path / "cron_jobs_tools.db")
    monkeypatch.setattr(manager_mod, "RETRY_BACKOFF_SECONDS", [0] * (manager_mod.MAX_RETRIES + 1))
    rec = _NotifyRecorder()
    monkeypatch.setattr(manager_mod, "notify", rec)
    mgr = CronJobManager()
    mgr._notify_recorder = rec
    return mgr


def _create(mgr, **over):
    fields = dict(name="Monthly Bill Invite", prompt="make the invite",
                  schedule="0 9 12 * *", operator="Robin")
    fields.update(over)
    return mgr.create_job(**fields)


def test_a_run_whose_tools_failed_is_recorded_failed(temp_manager, cron_chat):
    cron_chat({"ui_reply": REPLY, "tool_outcomes": [
        {"tool": "list_events", "success": False, "error": INVALID_GRANT},
        {"tool": "list_events", "success": False, "error": INVALID_GRANT},
    ]})
    job = _create(temp_manager)

    asyncio.run(temp_manager._execute_job(job["id"]))

    history = temp_manager.get_job_history(job["id"])
    assert len(history) == 1, "a completed turn with failed tools must not be retried"
    row = history[0]
    assert row["delivery_status"] == "error"
    assert "invalid_grant" in row["error"]
    assert row["result"] == REPLY

    after = temp_manager.get_job(job["id"])
    assert after["last_run_result"] == "error"
    assert after["error_count"] == 1
    assert after["run_count"] == 1

    alerts = [c for c in temp_manager._notify_recorder.calls if c.get("category") == "alert"]
    assert len(alerts) == 1
    assert alerts[0]["operator"] == "Robin"
    assert "invalid_grant" in alerts[0]["body"]


def test_a_partial_run_is_recorded_partial(temp_manager, cron_chat):
    cron_chat({"ui_reply": REPLY, "tool_outcomes": [
        {"tool": "list_events", "success": False, "error": INVALID_GRANT},
        {"tool": "create_event", "success": True},
    ]})
    job = _create(temp_manager, one_shot=True)

    asyncio.run(temp_manager._execute_job(job["id"]))

    after = temp_manager.get_job(job["id"])
    assert after is not None, "a one-shot whose run partly failed survives for inspection"
    history = temp_manager.get_job_history(job["id"])
    assert len(history) == 1
    assert history[0]["delivery_status"] == "partial"
    assert "list_events" in history[0]["error"]
    assert after["last_run_result"] == "partial"
    assert after["error_count"] == 1

    alerts = [c for c in temp_manager._notify_recorder.calls if c.get("category") == "alert"]
    assert len(alerts) == 1
    assert "partially failed" in alerts[0]["title"]


PARTIAL_OUTCOMES = [
    {"tool": "gemini_web_search", "success": True, "key": "w"},
    {"tool": "list_events", "success": False, "key": "e", "error": INVALID_GRANT},
]
FAILED_OUTCOMES = [
    {"tool": "list_events", "success": False, "key": "e", "error": INVALID_GRANT},
]


@pytest.mark.parametrize("outcomes, status", [
    (PARTIAL_OUTCOMES, "partial"),
    (FAILED_OUTCOMES, "error"),
])
def test_a_one_shot_whose_turn_completed_is_paused_not_fired_again(temp_manager, cron_chat, outcomes, status):
    """The turn ran (and any side effect that worked happened): a one-shot must not
    stay armed to fire again at the next match of its expression. It is paused,
    so it survives with its history for inspection, and can be resumed on purpose."""
    cron_chat({"ui_reply": REPLY, "tool_outcomes": outcomes})
    job = _create(temp_manager, one_shot=True, schedule="0 15 * * *")

    asyncio.run(temp_manager._execute_job(job["id"]))

    after = temp_manager.get_job(job["id"])
    assert after is not None, "the one-shot survives for inspection"
    assert after["status"] == "paused"
    assert after["next_run_at"] is None
    assert after["last_run_result"] == status
    sched_job = temp_manager.scheduler.get_job(job["id"])
    assert sched_job is None or getattr(sched_job, "next_run_time", "unset") is None, \
        "the APScheduler trigger must not fire again"
    assert [r["delivery_status"] for r in temp_manager.get_job_history(job["id"])] == [status]

    alerts = [c for c in temp_manager._notify_recorder.calls if c.get("category") == "alert"]
    assert len(alerts) == 1
    assert "paused" in alerts[0]["body"]


def test_a_one_shot_whose_chat_call_never_ran_still_survives_armed(temp_manager, monkeypatch):
    """Unchanged (M2.6): when the turn never ran (transport failure after every
    retry), the one-shot survives and stays scheduled — nothing happened yet."""
    from Orchestrator.scheduler import executor as executor_mod

    async def unreachable(job):
        raise RuntimeError("Failed to connect to chat API")

    monkeypatch.setattr(executor_mod, "execute_cron_job", unreachable)
    job = _create(temp_manager, one_shot=True, schedule="0 15 * * *")

    asyncio.run(temp_manager._execute_job(job["id"]))

    after = temp_manager.get_job(job["id"])
    assert after is not None
    assert after["status"] == "active"
    assert after["last_run_result"] == "error"


@pytest.mark.parametrize("outcomes", [PARTIAL_OUTCOMES, FAILED_OUTCOMES])
def test_a_notification_job_whose_tools_failed_still_delivers_its_reply(temp_manager, cron_chat, outcomes):
    """delivery='notification' pushes the reply the turn produced (as before this
    change), and the alert carrying the tool error comes on top of it."""
    cron_chat({"ui_reply": REPLY, "tool_outcomes": outcomes})
    job = _create(temp_manager, delivery="notification")

    asyncio.run(temp_manager._execute_job(job["id"]))

    calls = temp_manager._notify_recorder.calls
    alerts = [c for c in calls if c.get("category") == "alert"]
    deliveries = [c for c in calls if c.get("category") == "cron"]
    assert len(alerts) == 1 and "invalid_grant" in alerts[0]["body"]
    assert len(deliveries) == 1
    assert deliveries[0]["body"] == REPLY
    assert deliveries[0]["title"] == "Monthly Bill Invite"


def test_a_notification_job_that_never_ran_delivers_only_the_alert(temp_manager, monkeypatch):
    from Orchestrator.scheduler import executor as executor_mod

    async def unreachable(job):
        raise RuntimeError("Failed to connect to chat API")

    monkeypatch.setattr(executor_mod, "execute_cron_job", unreachable)
    job = _create(temp_manager, delivery="notification")

    asyncio.run(temp_manager._execute_job(job["id"]))

    calls = temp_manager._notify_recorder.calls
    assert [c.get("category") for c in calls] == ["alert"]


def test_a_healthy_run_is_still_a_success(temp_manager, cron_chat):
    cron_chat({"ui_reply": REPLY, "tool_outcomes": [{"tool": "list_events", "success": True}]})
    job = _create(temp_manager)

    asyncio.run(temp_manager._execute_job(job["id"]))

    history = temp_manager.get_job_history(job["id"])
    assert [r["delivery_status"] for r in history] == ["delivered"]
    assert history[0]["error"] is None
    after = temp_manager.get_job(job["id"])
    assert after["last_run_result"] == "success"
    assert after["error_count"] == 0


def test_transport_failures_are_still_retried(temp_manager, monkeypatch):
    """Only the completed-turn tool verdict skips retries; an unreachable /chat
    is still retried exactly as before."""
    from Orchestrator.scheduler import executor as executor_mod
    from Orchestrator.scheduler import manager as manager_mod

    async def unreachable(job):
        raise RuntimeError("Failed to connect to chat API")

    monkeypatch.setattr(executor_mod, "execute_cron_job", unreachable)
    job = _create(temp_manager)

    asyncio.run(temp_manager._execute_job(job["id"]))
    assert len(temp_manager.get_job_history(job["id"])) == manager_mod.MAX_RETRIES + 1


# ---------------------------------------------------------------------------
# 5. The Flight Recorder's health digest counts partial runs as failures
# ---------------------------------------------------------------------------

def test_oversight_counts_partial_runs(monkeypatch, isolated_stores):
    # isolated_stores: the oversight sweep also reads the ledger's index.
    import Orchestrator.scheduler as scheduler_pkg
    from Orchestrator import oversight

    class _Sched:
        def get_job(self, job_id):
            return object()

    class _Mgr:
        scheduler = _Sched()

        def list_jobs(self):
            return [{"id": "cron_bill", "name": "Monthly Bill Invite", "status": "active"}]

        def get_job_history(self, job_id, limit=5):
            return [
                {"run_at": "2026-10-12T09:00:50", "delivery_status": "partial", "error": "list_events: invalid_grant"},
                {"run_at": "2026-09-12T09:00:50", "delivery_status": "delivered", "error": None},
            ]

    monkeypatch.setattr(scheduler_pkg, "get_scheduler_manager", lambda: _Mgr())
    signals = oversight.collect_oversight_signals()
    failures = signals["cron"]["recent_failures"]
    assert [f["run_at"] for f in failures] == ["2026-10-12T09:00:50"]
