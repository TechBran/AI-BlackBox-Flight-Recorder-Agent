"""Live-view activity feed + stop (design 2026-07-23, M4).

The cu-view page needs {status, latest_action, narration tail} for ANY live
session and ONE stop button that works for both launch paths. Three feeds
existed (chat SSE -> chat bubble only; task-row reasoning_text -> /tasks poll;
session status -> cu-status) — none reachable from the page by session id.
"""
import pytest

from Orchestrator.browser import session_manager as bsm
from Orchestrator.browser.session_manager import (
    ComputerUseSession, fold_event_to_reasoning)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(ComputerUseSession, "is_alive", lambda self: True)
    monkeypatch.setattr(ComputerUseSession, "destroy", lambda self: None)
    bsm._sessions.clear()
    bsm._operator_sessions.clear()
    yield
    bsm._sessions.clear()
    bsm._operator_sessions.clear()


# ── The shared narration fold ──


def test_fold_accumulates_thinking_and_content_both_shapes():
    s = ComputerUseSession("op")
    fold_event_to_reasoning(s, {"type": "thinking", "data": "I will click A. "})
    fold_event_to_reasoning(s, {"type": "content", "data": "Clicking now."})
    # Gemini/OpenAI yield dict-shaped content payloads.
    fold_event_to_reasoning(s, {"type": "content",
                                "data": {"text": " Done with A.", "step": 2}})
    assert "I will click A." in s.reasoning_tail
    assert "Clicking now." in s.reasoning_tail
    assert "Done with A." in s.reasoning_tail


def test_fold_records_actions_as_lines():
    s = ComputerUseSession("op")
    fold_event_to_reasoning(s, {"type": "cu_action",
                                "data": {"action": "left_click",
                                         "params": {"coordinate": [10, 20]},
                                         "step": 3}})
    assert "left_click" in s.reasoning_tail


def test_fold_keeps_a_bounded_rolling_tail():
    s = ComputerUseSession("op")
    for i in range(500):
        fold_event_to_reasoning(s, {"type": "content", "data": f"chunk-{i} " * 10})
    assert len(s.reasoning_tail) <= bsm.REASONING_TAIL_MAX_CHARS
    assert "chunk-499" in s.reasoning_tail        # newest survives
    assert "chunk-0 " not in s.reasoning_tail     # oldest rolled off


def test_fold_ignores_non_narration_events():
    s = ComputerUseSession("op")
    fold_event_to_reasoning(s, {"type": "cu_screenshot", "data": {"url": "/x"}})
    fold_event_to_reasoning(s, {"type": "usage", "data": {}})
    assert s.reasoning_tail == ""


def test_reset_task_state_clears_the_tail():
    s = ComputerUseSession("op")
    fold_event_to_reasoning(s, {"type": "content", "data": "old turn"})
    s.reset_task_state()
    assert s.reasoning_tail == ""


def test_drivers_fold_narration():
    """All three drivers must feed the session tail — chat-launched runs have
    no task row, so the session is the ONLY narration store the live view
    can reach."""
    import inspect
    from Orchestrator.browser import driver_anthropic
    from Orchestrator.gemini_cu import agent_loop as gloop
    from Orchestrator.openai_cu import agent_loop as oloop
    assert "fold_event_to_reasoning" in inspect.getsource(
        driver_anthropic.run_anthropic_cu_loop)
    assert "fold_event_to_reasoning" in inspect.getsource(gloop.run_gemini_cu_loop)
    assert "fold_event_to_reasoning" in inspect.getsource(oloop.run_openai_cu_loop)


# ── Activity endpoint ──


def test_activity_reports_a_browser_session():
    from Orchestrator.routes.browser_routes import cu_session_activity
    s = bsm.get_or_create_session("op")
    s.status = "running"
    s.current_step = 4
    s.total_steps = 150
    s.cu_log.append({"type": "action", "action": "left_click", "step": 4})
    fold_event_to_reasoning(s, {"type": "thinking", "data": "hunting the button"})
    s.task_id = "task-77"
    out = cu_session_activity(s.session_id)
    assert out["status"] == "running"
    assert out["step"] == 4 and out["total"] == 150
    assert out["latest_action"] == "left_click"
    assert "hunting the button" in out["reasoning_tail"]
    assert out["task_id"] == "task-77"
    assert out["operator"] == "op"


def test_activity_reports_a_gemini_session(monkeypatch):
    from Orchestrator.gemini_cu import session_manager as gsm
    from Orchestrator.routes.browser_routes import cu_session_activity
    gsm._sessions.clear()
    try:
        g = gsm.GeminiCUSession("op", "blackbox", "desktop")
        gsm._sessions[g.session_id] = g
        g.status = "running"
        g.current_step = 2
        out = cu_session_activity(g.session_id)
        assert out["status"] == "running" and out["step"] == 2
    finally:
        gsm._sessions.clear()


def test_activity_404s_unknown_session():
    from fastapi.responses import JSONResponse
    from Orchestrator.routes.browser_routes import cu_session_activity
    out = cu_session_activity("nope")
    assert isinstance(out, JSONResponse) and out.status_code == 404


# ── Stop endpoint ──


def test_stop_routes_to_task_cancel_when_task_launched(monkeypatch):
    from Orchestrator.routes import browser_routes
    s = bsm.get_or_create_session("op")
    s.status = "running"
    s.task_id = "task-9"
    called = {}

    class _Row:
        status = "processing"
    monkeypatch.setattr("Orchestrator.tasks.task_db",
                        type("D", (), {"get_task": staticmethod(lambda tid: _Row())})())
    monkeypatch.setattr("Orchestrator.tasks.cancel_task",
                        lambda tid: called.setdefault("cancelled", tid) or {"success": True})
    out = browser_routes.cu_session_stop(s.session_id)
    assert out["success"] is True
    assert called["cancelled"] == "task-9"


def test_stop_falls_back_to_request_stop_for_chat_launched(monkeypatch):
    from Orchestrator.routes import browser_routes
    s = bsm.get_or_create_session("op")
    s.status = "running"
    s.task_id = None
    flagged = {}
    monkeypatch.setattr(ComputerUseSession, "request_stop",
                        lambda self: flagged.setdefault("stopped", True))
    out = browser_routes.cu_session_stop(s.session_id)
    assert out["success"] is True
    assert flagged.get("stopped") is True
