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
async def test_non_anthropic_backend_rejected(runner_env):
    """The task path is Anthropic-only (legacy parity); Gemini CU lives on the
    chat path."""
    result = await headless.run_cu_task(
        "t5", "system", "hi", model="gemini-2.5-computer-use-preview-10-2025")

    assert result["success"] is False
    assert "anthropic" in result["result_text"].lower()
