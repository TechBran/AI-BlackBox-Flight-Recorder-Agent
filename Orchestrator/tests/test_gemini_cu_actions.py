"""Gemini CU desktop actions must route through ActionExecutor (Wayland +
dynamic resolution) — direct _run_xdotool + stale NATIVE_WIDTH is the bug
this pass deletes."""
import inspect

import pytest

from Orchestrator.gemini_cu import agent_loop as G


def test_no_stale_resolution_constants():
    src = inspect.getsource(G)
    assert "NATIVE_WIDTH" not in src, "must use ActionExecutor.to_native (live resolution)"
    assert "_run_xdotool" not in src, "must route through ActionExecutor"


def test_no_hardcoded_display_in_prompt():
    class S:  # minimal session stub
        environment = "desktop"
    prompt = G._default_system_prompt(S())
    assert "1920x1080" not in prompt
    assert "display :0" not in prompt
    assert "get_current_time" not in prompt  # unsatisfiable instruction removed


@pytest.mark.asyncio
async def test_click_at_routes_through_executor(monkeypatch):
    """Clicks route through the SESSION-BOUND executor (2026-07-23 coherence
    contract: ensure_display binds session.actions; bare fallbacks are gone)."""
    calls = []
    class FakeExecutor:
        coord_space = "gemini-999"
        def execute(self, action, **params):
            calls.append((action, params)); return {"success": True}
        def to_native(self, x, y): return (x, y)
    class S:
        environment = "desktop"; device_id = "blackbox"
        native_mode = False; actions = FakeExecutor()
    result = await G._execute_predefined_action(S(), "click_at", {"x": 500, "y": 500})
    assert result["success"]
    assert calls and calls[0][0] == "left_click"


@pytest.mark.asyncio
async def test_virtual_session_without_executor_fails_loudly():
    """A destroyed-mid-step virtual session must ERROR, never fall back to a
    bare executor that would drive the operator's real desktop."""
    class S:
        environment = "desktop"; device_id = "blackbox"
        native_mode = False; actions = None
    result = await G._execute_predefined_action(S(), "click_at", {"x": 1, "y": 1})
    assert result["success"] is False
