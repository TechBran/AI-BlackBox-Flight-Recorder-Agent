"""M9: per-session display wiring — ActionExecutor targets the session's :N with
unscaled coords; ComputerUseSession exposes display_number + a capture seam."""
import pytest
from Orchestrator.browser.actions import ActionExecutor
from Orchestrator.browser import session_manager as sm


def test_action_executor_records_display_and_native_flag():
    ex = ActionExecutor(display_number=101, native_mode=False)
    assert ex.display_number == 101
    assert ex.native_mode is False


def test_action_executor_virtual_coords_unscaled():
    ex = ActionExecutor(display_number=101, native_mode=False)
    assert ex.to_native(640, 360) == (640, 360)  # scale 1.0 in virtual mode


def test_xdotool_targets_the_instances_display(monkeypatch):
    seen = {}
    def fake_run(*args, **kw):
        seen["env_display"] = kw["env"].get("DISPLAY")
        class R:  # minimal CompletedProcess stand-in
            returncode = 0; stdout = ""; stderr = ""
        return R()
    monkeypatch.setattr("Orchestrator.browser.actions.subprocess.run", fake_run)
    monkeypatch.setattr("Orchestrator.browser.actions._use_ydotool", lambda: False)
    # This dev box runs CU_NATIVE_MODE=True; force _run_xdotool's virtual
    # else-branch so the per-session :N routing under test is exercised
    # (adaptation from plan, which assumed a virtual-mode box).
    monkeypatch.setattr("Orchestrator.browser.actions.NATIVE_MODE", False)
    ex = ActionExecutor(display_number=102, native_mode=False)
    # plan wrote ex.mouse_move(10, 20); the executor's public mouse-move helper
    # is _move(x, y) — the internal that routes to _run_xdotool.
    ex._move(10, 20)
    assert seen["env_display"] == ":102"  # NOT the singleton's display


def test_session_display_number_defaults_to_active_when_no_handle():
    s = sm.ComputerUseSession("op")
    from Orchestrator.browser.config import ACTIVE_DISPLAY
    assert s.display is None
    assert s.display_number == ACTIVE_DISPLAY
