"""Unit tests for browser/headless.run_cu_task — the event-queue drain that
folds driver events into the task-result contract dict.

The Anthropic driver is replaced with a scripted fake that pushes events to
session.event_queue (exactly the seam the real driver uses); everything
between the queue and the returned dict is REAL runner code.
"""
import asyncio

import pytest

from Orchestrator.browser import headless
from Orchestrator.browser.session_manager import ComputerUseSession
from Orchestrator.routes import chat_routes


@pytest.fixture
def runner_env(monkeypatch):
    """Stub the runner's external seams; leave the drain logic real."""
    session_holder = {}

    def fake_get_or_create(operator, session_id=None, device_id="blackbox"):
        s = ComputerUseSession(operator, device_id=device_id)
        session_holder["session"] = s
        return s

    monkeypatch.setattr(headless, "get_or_create_session", fake_get_or_create)
    monkeypatch.setattr(headless, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(headless, "NATIVE_MODE", True)

    async def _ensure_browser(self, url="about:blank"):
        return True

    monkeypatch.setattr(ComputerUseSession, "ensure_browser", _ensure_browser)
    monkeypatch.setattr(ComputerUseSession, "destroy", lambda self: None)

    monkeypatch.setattr(headless, "capture_screenshot", lambda *a, **k: b"\x89PNG-fake")
    monkeypatch.setattr(
        headless, "save_screenshot_to_uploads",
        lambda png, ident, step: f"/ui/uploads/{ident}_step{step:03d}.png")
    monkeypatch.setattr(
        headless, "screenshot_to_base64", lambda png: "ZmFrZQ==")

    # chat_routes helpers are imported lazily by the runner at call time
    monkeypatch.setattr(chat_routes, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(chat_routes, "build_cu_context", lambda *a, **k: ("", {}))

    async def _instant(_secs):
        return None

    monkeypatch.setattr(headless.asyncio, "sleep", _instant)
    return session_holder


def _scripted_driver(events, final_step):
    """Build a fake run_anthropic_cu_loop that replays scripted events."""
    async def fake_driver(session, history, system_prompt, tools, headers,
                          model, operator, user_text):
        session.current_step = final_step
        for evt in events:
            await session.event_queue.put(evt)
        await session.event_queue.put(None)  # sentinel
    return fake_driver


@pytest.mark.asyncio
async def test_drain_maps_events_to_contract(runner_env, monkeypatch):
    events = [
        {"type": "cu_step", "data": {"step": 1, "total": 40}},
        {"type": "content", "data": "ignored streaming delta"},
        {"type": "cu_screenshot", "data": {"url": "/ui/uploads/s1.png", "step": 1}},
        {"type": "usage", "data": {"prompt_tokens": 10, "completion_tokens": 4}},
        {"type": "cu_screenshot", "data": {"url": "/ui/uploads/s2.png", "step": 2}},
        {"type": "usage", "data": {"prompt_tokens": 7, "completion_tokens": 2}},
        {"type": "done", "data": {"thinking": "", "content": "All done"}},
    ]
    monkeypatch.setattr(headless, "run_anthropic_cu_loop", _scripted_driver(events, 3))

    result = await headless.run_cu_task("t1", "system", "do the thing")

    assert result["success"] is True
    assert result["result_text"] == "All done"
    # initial screenshot (saved by the runner) + the driver's two
    assert result["screenshots"] == [
        "/ui/uploads/cu_system_step001.png", "/ui/uploads/s1.png", "/ui/uploads/s2.png"]
    assert result["final_screenshot"] == "/ui/uploads/s2.png"
    assert result["steps"] == 3
    assert result["tokens"] == {"input": 17, "output": 6}


@pytest.mark.asyncio
async def test_drain_error_event_fails_task(runner_env, monkeypatch):
    events = [
        {"type": "cu_screenshot", "data": {"url": "/ui/uploads/s1.png", "step": 1}},
        {"type": "error", "data": "API error 400: bad model"},
    ]
    monkeypatch.setattr(headless, "run_anthropic_cu_loop", _scripted_driver(events, 1))

    result = await headless.run_cu_task("t2", "system", "explode")

    assert result["success"] is False
    assert result["result_text"] == "API error 400: bad model"
    assert result["final_screenshot"] == "/ui/uploads/s1.png"
    assert result["tokens"] == {"input": 0, "output": 0}


@pytest.mark.asyncio
async def test_drain_stop_event_fails_task(runner_env, monkeypatch):
    events = [
        {"type": "cu_stopped", "data": {"step": 2, "reason": "User requested stop"}},
        {"type": "done", "data": {"thinking": "", "content": "[Task stopped by user at step 2]"}},
    ]
    monkeypatch.setattr(headless, "run_anthropic_cu_loop", _scripted_driver(events, 2))

    result = await headless.run_cu_task("t3", "system", "stop me")

    assert result["success"] is False
    assert "stopped" in result["result_text"].lower()


@pytest.mark.asyncio
async def test_single_display_conflict_returns_failed_dict(runner_env, monkeypatch):
    """get_or_create_session raises RuntimeError when another operator holds
    the local display — the runner must return a clean FAILED contract dict,
    not crash the worker."""
    def boom(operator, session_id=None, device_id="blackbox"):
        raise RuntimeError("Cannot start session: alice has a running Computer Use task on the local display")

    monkeypatch.setattr(headless, "get_or_create_session", boom)

    result = await headless.run_cu_task("t4", "system", "conflict")

    assert result["success"] is False
    assert "running Computer Use task" in result["result_text"]
    assert result["screenshots"] == [] and result["final_screenshot"] is None


@pytest.mark.asyncio
async def test_non_anthropic_backend_rejected(runner_env, monkeypatch):
    """The task path is Anthropic-only FOR NOW (T5 lands the real multi-backend
    dispatch); Gemini CU currently lives on the chat path.

    The key gate is now backend-aware and checked BEFORE this guard, so a Gemini
    task on a box with no GOOGLE key would fail on the missing key rather than
    reach the guard under test here. A fake GOOGLE_API_KEY is set so this test
    keeps exercising the backend GUARD (its original intent) — the assertions
    (success False, "anthropic" in the text) are unchanged."""
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake-google-key")
    result = await headless.run_cu_task(
        "t5", "system", "hi", model="gemini-2.5-computer-use-preview-10-2025")

    assert result["success"] is False
    assert "anthropic" in result["result_text"].lower()


@pytest.mark.asyncio
async def test_anthropic_missing_key_fails(runner_env, monkeypatch):
    """Anthropic model + no ANTHROPIC_API_KEY -> failure naming the Anthropic key."""
    monkeypatch.setattr(headless, "ANTHROPIC_API_KEY", "")
    result = await headless.run_cu_task(
        "t-anth", "system", "hi", model="claude-opus-4-6")

    assert result["success"] is False
    assert "ANTHROPIC_API_KEY" in result["result_text"]


@pytest.mark.asyncio
async def test_gemini_missing_google_key_names_google_not_anthropic(runner_env, monkeypatch):
    """Gemini model + no GOOGLE_API_KEY must fail naming the GOOGLE key — NOT the
    Anthropic one. Before the gate reorder this failed with the Anthropic-only
    backend message (which names the WRONG vendor), so this test went red first."""
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "")
    result = await headless.run_cu_task(
        "t-gem", "system", "hi", model="gemini-2.5-computer-use-preview-10-2025")

    assert result["success"] is False
    assert "GOOGLE_API_KEY" in result["result_text"]
    # The failure must not name the wrong vendor's key.
    assert "anthropic" not in result["result_text"].lower()


@pytest.mark.asyncio
async def test_openai_missing_key_fails(runner_env, monkeypatch):
    """OpenAI model + no OPENAI_API_KEY -> failure naming the OPENAI key."""
    monkeypatch.setattr(headless, "OPENAI_API_KEY", "")
    result = await headless.run_cu_task(
        "t-oai", "system", "hi", model="gpt-5.5")

    assert result["success"] is False
    assert "OPENAI_API_KEY" in result["result_text"]
    assert "anthropic" not in result["result_text"].lower()


@pytest.mark.asyncio
async def test_gemini_with_google_key_reaches_backend_guard(runner_env, monkeypatch):
    """Gemini model + GOOGLE_API_KEY present -> the key gate is satisfied, so the
    request advances to T5's Anthropic-only fail-fast. Proves the key gate is no
    longer the blocker on a Google-provisioned box (fresh-box portability)."""
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake-google-key")
    result = await headless.run_cu_task(
        "t-gem2", "system", "hi", model="gemini-2.5-computer-use-preview-10-2025")

    assert result["success"] is False
    assert "anthropic" in result["result_text"].lower()
    # The key gate did NOT fire — the backend guard is what remains.
    assert "GOOGLE_API_KEY not set" not in result["result_text"]


@pytest.mark.asyncio
async def test_anthropic_key_present_proceeds_past_both_gates(runner_env, monkeypatch):
    """Anthropic model + key present -> proceeds past BOTH gates and reaches the
    driver (scripted here so no real browser work happens)."""
    events = [{"type": "done", "data": {"thinking": "", "content": "reached the driver"}}]
    monkeypatch.setattr(headless, "run_anthropic_cu_loop", _scripted_driver(events, 1))

    result = await headless.run_cu_task(
        "t-anth2", "system", "hi", model="claude-opus-4-6")

    assert result["success"] is True
    assert result["result_text"] == "reached the driver"


def test_fresh_event_queue_unbinds_dead_worker_loop():
    """Regression (Task 12 review C1): a session whose event_queue was bound
    inside a worker thread's asyncio.run() must be reusable from a NEW loop
    after fresh_event_queue() — without it, the chat path's next CU turn dies
    with "bound to a different event loop"."""
    import asyncio

    from Orchestrator.browser.session_manager import ComputerUseSession

    session = ComputerUseSession("queue-rebind-op")

    async def _bind_and_drain():
        # Bind the queue to this (soon-to-be-dead) loop the same way the
        # runner does: await get() against it.
        session.event_queue.put_nowait(None)
        await session.event_queue.get()

    asyncio.run(_bind_and_drain())  # worker-thread style run; loop now closed

    async def _chat_turn():
        # What stream_computer_use now does before launching its driver:
        session.fresh_event_queue()
        session.event_queue.put_nowait({"type": "done"})
        return await session.event_queue.get()

    event = asyncio.run(_chat_turn())  # second, distinct loop
    assert event == {"type": "done"}
