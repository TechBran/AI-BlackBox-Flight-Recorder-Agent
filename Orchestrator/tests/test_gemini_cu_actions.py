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
    calls = []
    class FakeExecutor:
        coord_space = "gemini-999"
        def __init__(self, *a, **k): assert k.get("coord_space") == "gemini-999"
        def execute(self, action, **params):
            calls.append((action, params)); return {"success": True}
        def to_native(self, x, y): return (x, y)
    monkeypatch.setattr(G, "ActionExecutor", FakeExecutor)
    class S:
        environment = "desktop"; device_id = "blackbox"
    result = await G._execute_predefined_action(S(), "click_at", {"x": 500, "y": 500})
    assert result["success"]
    assert calls and calls[0][0] == "left_click"
