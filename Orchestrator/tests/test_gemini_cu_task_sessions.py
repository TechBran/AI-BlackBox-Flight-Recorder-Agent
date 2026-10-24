"""Gemini CU REST tasks run on their OWN session (audit O13-computer-use-15,
O18-toolvault-modules-10).

/gemini-cu/run and /stream used get_or_create_session, whose cache hit came back
as-is: a session created for an ADB phone kept environment='android' and the
phone's device_id when the next request targeted the desktop, so the desktop
task drove the phone (and carried the previous task's history). The background
driver was also launched with a bare asyncio.create_task, which keeps only a
weak reference and drops any exception on the floor.

Contracts under test:
  * each /run and /stream task builds a task-scoped session with exactly the
    requested device/environment, never touches the operator's chat session,
    and tears its session down on exit;
  * the task row learns the session id, and a cancel resolves THAT session;
  * start_gemini_cu_task holds a strong reference and records a launch failure
    on the task row; control_android_device launches through it.
"""
import asyncio
from types import SimpleNamespace

import pytest

from Orchestrator.gemini_cu import session_manager as gsm
from Orchestrator.models import TaskStatus
from Orchestrator.routes import gemini_cu_routes as gr


@pytest.fixture(autouse=True)
def _clean_sessions():
    gsm._sessions.clear()
    gsm._operator_sessions.clear()
    yield
    gsm._sessions.clear()
    gsm._operator_sessions.clear()


@pytest.fixture
def task_row(monkeypatch):
    from Orchestrator import tasks as tasks_mod
    task = SimpleNamespace(task_id="rt1", status=None, progress=0, result_url=None,
                           session_id=None, operator="op",
                           result_data={"device_id": "blackbox", "model": "m"})
    saved = []
    db = SimpleNamespace(get_task=lambda tid: task if tid == "rt1" else None,
                         save_task=lambda t: saved.append(t.status))
    monkeypatch.setattr(tasks_mod, "task_db", db)
    monkeypatch.setattr(tasks_mod, "_cancel_handles", {})
    monkeypatch.setattr(tasks_mod, "_cancel_requested", set())

    async def _no_snap(*a, **k):
        return None

    monkeypatch.setattr(gr, "_snapshot_cu_result", _no_snap)
    return task


def _phone_chat_session(operator="op"):
    """The operator's cached CHAT session from an earlier phone turn."""
    s = gsm.get_or_create_session(operator, "pixel-9", "android")
    s.conversation_history.append("earlier phone turn")
    return s


@pytest.mark.asyncio
async def test_run_task_uses_its_own_session_with_the_requested_target(task_row, monkeypatch):
    chat = _phone_chat_session()
    seen = {}

    async def fake_loop(session, prompt, model, system_prompt, url):
        seen.update(device=session.device_id, env=session.environment,
                    history=list(session.conversation_history), sid=session.session_id)
        yield {"type": "done", "data": {"content": "ok"}}

    monkeypatch.setattr(gr, "run_gemini_cu_loop", fake_loop)
    await gr._run_task("rt1", "op", "blackbox", "browser", "go", "m", None, None)

    assert seen["device"] == "blackbox" and seen["env"] == "browser"
    assert seen["history"] == []                       # no chat context leaked in
    assert seen["sid"] != chat.session_id
    assert task_row.status == TaskStatus.COMPLETED
    assert task_row.session_id == seen["sid"]          # the row can find its session
    assert seen["sid"] not in gsm._sessions            # torn down on exit
    assert gsm.get_session("op") is chat               # the chat session is untouched
    assert chat.device_id == "pixel-9" and chat.environment == "android"


@pytest.mark.asyncio
async def test_run_task_tears_down_its_session_when_the_driver_raises(task_row, monkeypatch):
    seen = {}

    async def boom(session, prompt, model, system_prompt, url):
        seen["sid"] = session.session_id
        raise RuntimeError("driver exploded")
        yield

    monkeypatch.setattr(gr, "run_gemini_cu_loop", boom)
    await gr._run_task("rt1", "op", "pixel-9", "android", "go", "m", None, None)
    assert task_row.status == TaskStatus.FAILED
    assert seen["sid"] not in gsm._sessions


@pytest.mark.asyncio
async def test_cancel_reaches_the_task_session(task_row, monkeypatch):
    from Orchestrator import tasks as tasks_mod
    from Orchestrator.routes.chat_routes import resolve_cu_session
    _phone_chat_session()
    seen = {}

    async def fake_loop(session, prompt, model, system_prompt, url):
        handle = tasks_mod._cancel_handles.get("rt1")
        seen["handle_sid"] = handle and handle.get("session_id")
        seen["resolved"] = resolve_cu_session("op", session.session_id, model)
        seen["session"] = session
        tasks_mod.request_cooperative_cancel("rt1")      # the operator hits cancel
        yield {"type": "cu_step", "data": {"step": 1, "total": 5}}
        seen["stopped"] = session.stop_requested
        yield {"type": "done", "data": {"content": "half"}}

    monkeypatch.setattr(gr, "run_gemini_cu_loop", fake_loop)
    await gr._run_task("rt1", "op", "pixel-9", "android", "go", "gemini-2.5-computer-use", None, None)
    assert seen["handle_sid"] == seen["session"].session_id
    assert seen["resolved"] is seen["session"]
    assert seen["stopped"] is True
    assert task_row.status == TaskStatus.CANCELLED
    assert "rt1" not in tasks_mod._cancel_handles


@pytest.mark.asyncio
async def test_stream_uses_its_own_session_and_tears_it_down(monkeypatch):
    chat = _phone_chat_session()

    class _Reg:
        def get_device(self, did):
            return SimpleNamespace(protocol=None, name="Desk", id=did)

    monkeypatch.setattr(gr, "get_registry", lambda: _Reg())
    seen = {}

    async def fake_loop(session, prompt, model, system_prompt, url):
        seen.update(env=session.environment, device=session.device_id, sid=session.session_id)
        yield {"type": "done", "data": {"content": "ok"}}

    monkeypatch.setattr(gr, "run_gemini_cu_loop", fake_loop)
    resp = await gr.stream_gemini_cu(gr.GeminiCURequest(prompt="go", operator="op",
                                                        device_id="blackbox"))
    chunks = [c async for c in resp.body_iterator]
    assert any("[DONE]" in c for c in chunks)
    assert seen["env"] == "browser" and seen["device"] == "blackbox"
    assert seen["sid"] != chat.session_id and seen["sid"] not in gsm._sessions
    assert gsm.get_session("op") is chat


@pytest.mark.asyncio
async def test_start_task_keeps_a_strong_reference_and_records_failures(task_row, monkeypatch):
    from Orchestrator import tasks as tasks_mod
    updates = []
    monkeypatch.setattr(tasks_mod, "update_task", lambda tid, **kw: updates.append((tid, kw)))
    gate = asyncio.Event()

    async def blows_up(*a, **k):
        await gate.wait()
        raise RuntimeError("could not reach the device")

    monkeypatch.setattr(gr, "_run_task", blows_up)
    t = gr.start_gemini_cu_task("rt1", "op", "pixel-9", "android", "go", "m")
    assert t in gr._BACKGROUND_TASKS
    gate.set()
    await asyncio.gather(t, return_exceptions=True)
    await asyncio.sleep(0)
    assert t not in gr._BACKGROUND_TASKS
    assert updates and updates[0][0] == "rt1"
    assert updates[0][1]["status"] == TaskStatus.FAILED
    assert "could not reach the device" in updates[0][1]["error_message"]


def test_control_android_device_launches_through_the_public_starter(monkeypatch):
    import inspect
    from Orchestrator.toolvault import registry
    registry.invalidate_cache()
    src = inspect.getsource(registry.get_executor("control_android_device"))
    assert "start_gemini_cu_task" in src
    assert "_run_task" not in src
    assert "get_or_create_session" not in src


# ── the worker never owns a GEMINI_CU row (review: cancel hit another agent) ──
# create_task queued the row for the background worker, whose process_task
# "skipped" it and then, in its finally, dropped the cancel handle _run_task had
# just registered. cancel_task then routed by operator and stopped the
# operator's OTHER CU session (a running desktop agent).

def _gemini_row(task_id="rt1", operator="op"):
    from Orchestrator.models import TaskType
    return SimpleNamespace(task_id=task_id, task_type=TaskType.GEMINI_CU,
                           status=TaskStatus.PENDING, progress=0, result_url=None,
                           session_id=None, operator=operator,
                           result_data={"device_id": "pixel-9",
                                        "model": "gemini-2.5-computer-use-preview-10-2025"})


@pytest.fixture
def gemini_row(monkeypatch):
    from Orchestrator import tasks as tasks_mod
    task = _gemini_row()
    db = SimpleNamespace(get_task=lambda tid: task if tid == task.task_id else None,
                         save_task=lambda t: None)
    monkeypatch.setattr(tasks_mod, "task_db", db)
    monkeypatch.setattr(tasks_mod, "_cancel_handles", {})
    monkeypatch.setattr(tasks_mod, "_cancel_requested", set())

    def _upd(tid, **kw):
        for k, v in kw.items():
            if k != "error_message":
                setattr(task, k, v)
        if "error_message" in kw:
            task.error_message = kw["error_message"]

    monkeypatch.setattr(tasks_mod, "update_task", _upd)

    async def _no_snap(*a, **k):
        return None

    monkeypatch.setattr(gr, "_snapshot_cu_result", _no_snap)
    return task


@pytest.fixture
def other_desktop_agent(monkeypatch):
    """The operator's unrelated desktop CU agent (browser store, MRU pointer)."""
    from Orchestrator.browser import session_manager as bsm
    stops = []
    other = SimpleNamespace(session_id="desk-1", operator="op",
                            request_stop=lambda: stops.append("desk-1"))
    monkeypatch.setitem(bsm._sessions, "desk-1", other)
    monkeypatch.setitem(bsm._operator_sessions, "op", "desk-1")
    return stops


def test_create_task_does_not_queue_a_route_owned_gemini_row(monkeypatch):
    from Orchestrator import tasks as tasks_mod
    from Orchestrator.models import TaskType
    queue = []
    monkeypatch.setattr(tasks_mod, "task_queue", queue)
    monkeypatch.setattr(tasks_mod, "task_db", SimpleNamespace(save_task=lambda t: None))
    gem = tasks_mod.create_task(TaskType.GEMINI_CU, operator="op", prompt="go")
    img = tasks_mod.create_task(TaskType.IMAGE_GENERATION, operator="op", prompt="a cat")
    assert gem.task_id not in queue
    assert queue == [img.task_id]


def test_process_task_leaves_a_gemini_rows_cancel_bookkeeping_alone(gemini_row):
    from Orchestrator import tasks as tasks_mod
    tasks_mod.register_cancel_handle("rt1", "cu", operator="op", session_id="gem-1")
    tasks_mod.request_cooperative_cancel("rt1")
    tasks_mod.process_task(gemini_row)
    assert tasks_mod._cancel_handles.get("rt1", {}).get("session_id") == "gem-1"
    assert tasks_mod.is_cancel_requested("rt1")
    assert gemini_row.status == TaskStatus.PENDING     # the worker wrote nothing


@pytest.mark.asyncio
async def test_cancel_after_a_worker_pass_stops_only_the_tasks_own_session(
        gemini_row, other_desktop_agent, monkeypatch):
    from Orchestrator import tasks as tasks_mod
    seen = {}

    async def fake_loop(session, prompt, model, system_prompt, url):
        tasks_mod.process_task(gemini_row)            # a stray worker pass
        seen["cancel"] = tasks_mod.cancel_task("rt1")  # the operator hits Cancel
        yield {"type": "cu_step", "data": {"step": 1, "total": 5}}
        seen["stopped"] = session.stop_requested
        yield {"type": "done", "data": {"content": "half"}}

    monkeypatch.setattr(gr, "run_gemini_cu_loop", fake_loop)
    await gr._run_task("rt1", "op", "pixel-9", "android", "go",
                       "gemini-2.5-computer-use-preview-10-2025", None, None)
    assert other_desktop_agent == [], "the cancel stopped the operator's other CU agent"
    assert seen["stopped"] is True
    assert seen["cancel"]["cancelled"] is True
    assert gemini_row.status == TaskStatus.CANCELLED
    from Orchestrator import tasks as tasks_mod2
    assert not tasks_mod2.is_cancel_requested("rt1"), "_run_task clears its own cancel flag"


def test_handleless_cancel_uses_the_rows_own_session(gemini_row, other_desktop_agent):
    from Orchestrator import tasks as tasks_mod
    own = gsm.create_task_session("op", "pixel-9", "android")
    try:
        gemini_row.status = TaskStatus.PROCESSING
        gemini_row.session_id = own.session_id
        tasks_mod.cancel_task("rt1")
        assert own.stop_requested is True
        assert other_desktop_agent == []
    finally:
        gsm.destroy_task_session(own)


def test_cancel_of_a_finished_gemini_session_never_falls_back(gemini_row,
                                                              other_desktop_agent):
    """The row names a task session that is already gone: nothing else stops."""
    from Orchestrator import tasks as tasks_mod
    gemini_row.status = TaskStatus.PROCESSING
    gemini_row.session_id = "gone-session"
    tasks_mod.register_cancel_handle("rt1", "cu", operator="op", session_id="gone-session")
    res = tasks_mod.cancel_task("rt1")
    assert res["cancelled"] is True
    assert other_desktop_agent == []


# ── the non-stream tool path runs the driver on the server loop ─────────────
# call_anthropic / call_gemini run a tool through asyncio.run(...) on a worker
# thread. A driver created on THAT loop was cancelled the moment the tool
# returned, and the row stayed PENDING/PROCESSING forever (O18-toolvault-10).

@pytest.fixture
def server_loop(monkeypatch):
    import threading
    from Orchestrator.notifications import bridge
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    monkeypatch.setattr(bridge, "_MAIN_LOOP", loop)
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)
    loop.close()


def test_tool_started_on_a_throwaway_loop_runs_on_the_server_loop(gemini_row, server_loop,
                                                                  monkeypatch):
    import concurrent.futures
    seen = {}

    async def fake_loop(session, prompt, model, system_prompt, url):
        await asyncio.sleep(0.05)       # outlives the tool call's asyncio.run
        seen["loop"] = asyncio.get_running_loop()
        yield {"type": "done", "data": {"content": "done on the phone"}}

    monkeypatch.setattr(gr, "run_gemini_cu_loop", fake_loop)
    handle = {}

    async def tool_call():              # what the ToolVault executor does
        handle["fut"] = gr.start_gemini_cu_task("rt1", "op", "pixel-9", "android", "go")
        return "started"

    assert asyncio.run(tool_call()) == "started"
    fut = handle["fut"]
    assert isinstance(fut, concurrent.futures.Future)
    fut.result(timeout=5)
    assert seen["loop"] is server_loop
    assert gemini_row.status == TaskStatus.COMPLETED
    for _ in range(50):
        if fut not in gr._BACKGROUND_TASKS:
            break
        import time
        time.sleep(0.01)
    assert fut not in gr._BACKGROUND_TASKS


def test_a_driver_cancelled_with_its_loop_does_not_leave_the_row_running(gemini_row,
                                                                         monkeypatch):
    """No server loop captured (a bare script): the teardown cancel is recorded."""
    from Orchestrator.notifications import bridge
    monkeypatch.setattr(bridge, "_MAIN_LOOP", None)

    async def fake_loop(session, prompt, model, system_prompt, url):
        await asyncio.sleep(5)
        yield {"type": "done", "data": {"content": "never"}}

    monkeypatch.setattr(gr, "run_gemini_cu_loop", fake_loop)

    async def tool_call():
        gr.start_gemini_cu_task("rt1", "op", "pixel-9", "android", "go")
        return "started"

    asyncio.run(tool_call())
    assert gemini_row.status == TaskStatus.FAILED
    assert "interrupted" in gemini_row.error_message


def test_cancel_before_the_driver_starts_stops_nothing_else_and_never_runs(
        gemini_row, other_desktop_agent, monkeypatch):
    from Orchestrator import tasks as tasks_mod
    ran = []

    async def fake_loop(session, prompt, model, system_prompt, url):
        ran.append(session)
        yield {"type": "done", "data": {"content": "should not run"}}

    monkeypatch.setattr(gr, "run_gemini_cu_loop", fake_loop)
    res = tasks_mod.cancel_task("rt1")      # still PENDING, no session yet
    assert res["cancelled"] is True and other_desktop_agent == []
    asyncio.run(gr._run_task("rt1", "op", "pixel-9", "android", "go", "m", None, None))
    assert ran == []
    assert gemini_row.status == TaskStatus.CANCELLED
    assert not tasks_mod.is_cancel_requested("rt1")


# ── a task session stays reachable (review minor) ───────────────────────────

@pytest.mark.asyncio
async def test_stream_tells_its_client_the_session_id_first(monkeypatch):
    import json as _json

    class _Reg:
        def get_device(self, did):
            return SimpleNamespace(protocol=None, name="Desk", id=did)

    monkeypatch.setattr(gr, "get_registry", lambda: _Reg())
    seen = {}

    async def fake_loop(session, prompt, model, system_prompt, url):
        seen["sid"] = session.session_id
        yield {"type": "done", "data": {"content": "ok"}}

    monkeypatch.setattr(gr, "run_gemini_cu_loop", fake_loop)
    resp = await gr.stream_gemini_cu(gr.GeminiCURequest(prompt="go", operator="op",
                                                        device_id="blackbox"))
    chunks = [c async for c in resp.body_iterator]
    first = _json.loads(chunks[0][len("data: "):])
    assert first == {"type": "session", "data": {"session_id": seen["sid"]}}


@pytest.mark.asyncio
async def test_delete_operator_session_also_stops_their_task_sessions():
    chat = _phone_chat_session()
    mine = gsm.create_task_session("op", "pixel-9", "android")
    theirs = gsm.create_task_session("someone-else", "pixel-9", "android")
    try:
        res = await gr.end_session("op")
        assert res == {"status": "destroyed", "stopped_tasks": 1}
        assert mine.stop_requested is True
        assert theirs.stop_requested is False
        assert gsm.get_session("op") is None and chat.stop_requested is True
    finally:
        gsm.destroy_task_session(mine)
        gsm.destroy_task_session(theirs)
