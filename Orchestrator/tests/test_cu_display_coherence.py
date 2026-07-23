"""CU display coherence (production-readiness plan 2026-07-23, M0/M1).

THE invariant these tests encode: for a VIRTUAL CU session, the screenshot
display == the click display == the session handle's :N — regardless of the
box-global CU_NATIVE_MODE. Written RED against the 2026-07-23 regression:
the input/capture primitives' `if NATIVE_MODE:` short-circuits stomped the
per-session display with the real desktop (:0), so agents saw and clicked the
operator's actual screen while the live view streamed the barren virtual one.
"""
import pytest

from Orchestrator.browser import actions as amod
from Orchestrator.browser import screenshot as smod
from Orchestrator.browser import session_manager as sm
from Orchestrator.browser.actions import (
    ActionExecutor, COORD_SPACE_ANTHROPIC, COORD_SPACE_GEMINI,
)


def _recording_run(seen):
    """subprocess.run stand-in that records every (cmd, env) it sees."""
    def fake_run(cmd, env=None, **kw):
        seen.setdefault("cmds", []).append(list(cmd))
        seen.setdefault("envs", []).append(dict(env or {}))
        class R:
            returncode = 0
            stdout = ""
            stderr = b""
        return R()
    return fake_run


# ── The core fix: primitives honor the passed display over the global ──


def test_virtual_executor_clicks_its_own_display_even_on_native_box(monkeypatch):
    """A native_mode=False executor on a CU_NATIVE_MODE=True box must drive ITS
    display (:101), not the box's real desktop. This was the click half of the
    regression: _run_xdotool ignored display_number whenever NATIVE_MODE."""
    seen = {}
    monkeypatch.setattr(amod.subprocess, "run", _recording_run(seen))
    monkeypatch.setattr(amod, "_use_ydotool", lambda: False)
    monkeypatch.setattr(amod, "NATIVE_MODE", True)
    ex = ActionExecutor(display_number=101, native_mode=False)
    ex._move(10, 20)
    assert seen["envs"][-1].get("DISPLAY") == ":101"


def test_native_executor_still_uses_the_native_env(monkeypatch):
    """The explicit-native executor (main-desktop manual control) keeps the
    XAUTHORITY-bearing native env — the fix must not regress :0 driving."""
    seen = {}
    monkeypatch.setattr(amod.subprocess, "run", _recording_run(seen))
    monkeypatch.setattr(amod, "_use_ydotool", lambda: False)
    monkeypatch.setattr(amod, "NATIVE_MODE", True)
    monkeypatch.setattr(amod, "get_native_env",
                        lambda: {"DISPLAY": ":0", "XAUTHORITY": "/xauth"})
    ex = ActionExecutor(native_mode=True)
    ex._move(1, 2)
    assert seen["envs"][-1].get("DISPLAY") == ":0"
    assert seen["envs"][-1].get("XAUTHORITY") == "/xauth"


def test_virtual_executor_never_routes_input_through_ydotool(monkeypatch):
    """ydotool injects at /dev/uinput — the REAL kernel seat. It can never reach
    an Xvfb display, so on a Wayland host a virtual executor routing through
    ydotool clicks the operator's desktop. Virtual must pin xdotool."""
    monkeypatch.setattr(amod, "_use_ydotool", lambda: True)
    assert ActionExecutor(display_number=101, native_mode=False).use_ydotool is False
    assert ActionExecutor(native_mode=True).use_ydotool is True


def test_capture_display_honors_display_number_when_not_native(monkeypatch, tmp_path):
    """The screenshot half of the regression: capture_screenshot_display ignored
    display_number whenever NATIVE_MODE — agents grounded on the REAL desktop."""
    seen = {}

    def fake_run(cmd, env=None, **kw):
        seen["env"] = dict(env or {})
        from pathlib import Path
        Path(cmd[-1]).write_bytes(b"x" * 200)  # satisfy the size sanity check
        class R:
            returncode = 0
            stdout = b""
            stderr = b""
        return R()

    monkeypatch.setattr(smod.subprocess, "run", fake_run)
    monkeypatch.setattr(smod, "NATIVE_MODE", True)
    smod.capture_screenshot_display(101, native=False)
    assert seen["env"].get("DISPLAY") == ":101"


def test_session_capture_seam_pins_native_off_for_virtual_display(monkeypatch):
    """ComputerUseSession.capture_screenshot_bytes must capture the handle's :N
    with the native short-circuit explicitly OFF."""
    calls = {}

    def fake_capture(display_number, native=None):
        calls["display_number"] = display_number
        calls["native"] = native
        return b"p" * 200

    monkeypatch.setattr(
        "Orchestrator.browser.screenshot.capture_screenshot_display", fake_capture)
    s = sm.ComputerUseSession("op")

    class H:
        display_num = 102

        def touch(self):
            pass
    s.display = H()
    assert s.capture_screenshot_bytes() == b"p" * 200
    assert calls["display_number"] == 102
    assert calls["native"] is False


# ── Coordinate spaces on virtual displays ──


def test_anthropic_virtual_coords_are_identity():
    ex = ActionExecutor(display_number=101, native_mode=False, resolution=(1280, 720))
    assert ex.to_native(640, 360) == (640, 360)


def test_gemini_virtual_coords_denormalize_to_session_resolution():
    """Gemini replies 0-999 normalized. On a virtual display those MUST be
    de-normalized against the session's own resolution — identity passthrough
    (the pre-fix virtual behavior) lands every click in the top-left ~70%.
    Divisor is /1000 per Google's contract (999 stays in-bounds)."""
    ex = ActionExecutor(display_number=101, coord_space=COORD_SPACE_GEMINI,
                        native_mode=False, resolution=(1440, 900))
    assert ex.to_native(999, 999) == (1438, 899)
    assert ex.to_native(0, 0) == (0, 0)
    assert ex.to_native(500, 500) == (720, 450)


def test_gemini_virtual_denormalization_defaults_to_backend_resolution():
    """resolution omitted → fall back to the gemini backend's canonical virtual
    resolution rather than silently passing 0-999 through as pixels."""
    from Orchestrator.gemini_cu.config import GEMINI_CU_WIDTH, GEMINI_CU_HEIGHT
    ex = ActionExecutor(display_number=101, coord_space=COORD_SPACE_GEMINI,
                        native_mode=False)
    assert ex.to_native(999, 999) == (int(999 / 1000 * GEMINI_CU_WIDTH),
                                      int(999 / 1000 * GEMINI_CU_HEIGHT))


# ── Gemini sessions own a display like Anthropic/OpenAI sessions do ──


def test_gemini_session_allocates_and_binds_its_own_display(monkeypatch):
    """GeminiCUSession historically NEVER allocated a virtual display (display
    stayed None → display_number fell back to ACTIVE_DISPLAY = the real :0) and
    its click path built a bare ActionExecutor. ensure_display() must allocate
    a per-session display and bind a session executor to it."""
    from Orchestrator.gemini_cu.session_manager import GeminiCUSession

    class _H:
        display_num = 103
        width = 1440
        height = 900

        def touch(self):
            pass

        def get_env(self):
            return {"DISPLAY": ":103"}

    class _Alloc:
        def __init__(self):
            self.released = []
            self.backend = None

        def allocate(self, sid, backend="anthropic", operator="system"):
            self.backend = backend
            return _H()

        def release(self, sid):
            self.released.append(sid)

    alloc = _Alloc()
    monkeypatch.setattr("Orchestrator.browser.display.get_allocator", lambda: alloc)

    s = GeminiCUSession("op", "blackbox", "desktop")
    assert s.ensure_display() is True
    assert alloc.backend in ("gemini", "google")
    assert s.display is not None and s.display.display_num == 103
    assert s.display_number == 103
    ex = s.actions
    assert ex is not None
    assert ex.display_number == 103
    assert ex.coord_space == COORD_SPACE_GEMINI
    assert ex.native_mode is False
    assert ex.to_native(999, 999) == (1438, 899)

    s.destroy()
    assert alloc.released == [s.session_id]


def test_gemini_android_session_needs_no_display(monkeypatch):
    """Android CU goes over ADB — ensure_display must be a no-op success and
    never touch the allocator."""
    from Orchestrator.gemini_cu.session_manager import GeminiCUSession

    def _boom():
        raise AssertionError("allocator must not be consulted for android")

    monkeypatch.setattr("Orchestrator.browser.display.get_allocator", _boom)
    s = GeminiCUSession("op", "phone-1", "android")
    assert s.ensure_display() is True
    assert s.display is None


# ── Manual live-viewer endpoints scale exactly once ──


def test_manual_viewer_click_scales_exactly_once(monkeypatch):
    """interaction.py pre-scaled model coords via get_scale_factors AND the
    executor scaled again in to_native — manual Portal clicks landed 2x+ off
    target on native boxes. Exactly ONE scaling may survive."""
    from Orchestrator.browser import interaction as imod
    seen = {}
    monkeypatch.setattr(amod.subprocess, "run", _recording_run(seen))
    monkeypatch.setattr(amod, "_use_ydotool", lambda: False)
    monkeypatch.setattr(amod, "NATIVE_MODE", True)
    monkeypatch.setattr(
        "Orchestrator.browser.config.detect_native_resolution",
        lambda force=False: (2560, 1440))
    monkeypatch.setattr(imod, "NATIVE_MODE", True, raising=False)
    monkeypatch.setattr(imod, "get_scale_factors", lambda: (2.0, 2.0), raising=False)
    monkeypatch.setattr(imod, "_EXECUTOR", ActionExecutor(native_mode=True))

    imod.click(640, 360)

    moves = [c for c in seen["cmds"] if "mousemove" in c]
    assert moves, "click() must move the pointer before clicking"
    assert moves[-1][-2:] == ["1280", "720"]  # 640*2, 360*2 — scaled ONCE


# ── Declared tool dims stay locked to the virtual resolution ──


def test_backend_virtual_resolution_matches_declared_tool_dims():
    """The Anthropic computer tool declares DISPLAY_WIDTH/HEIGHT px and the
    OpenAI pipeline resizes to 1280x720; the per-session virtual displays are
    spawned from resolution_for_backend. These must never drift apart, or the
    model grounds on a different geometry than it declares/clicks."""
    from Orchestrator.browser.display import resolution_for_backend
    from Orchestrator.browser.config import DISPLAY_WIDTH, DISPLAY_HEIGHT
    from Orchestrator.openai_cu.config import OPENAI_CU_WIDTH, OPENAI_CU_HEIGHT
    assert resolution_for_backend("anthropic") == (DISPLAY_WIDTH, DISPLAY_HEIGHT)
    assert resolution_for_backend("openai") == (OPENAI_CU_WIDTH, OPENAI_CU_HEIGHT)
