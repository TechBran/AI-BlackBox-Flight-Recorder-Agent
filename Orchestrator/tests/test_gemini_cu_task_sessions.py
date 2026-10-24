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
