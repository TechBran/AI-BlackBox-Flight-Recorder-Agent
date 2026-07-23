"""
interaction.py — manual /browser/click /browser/type /browser/key /browser/scroll
endpoints used by the Portal live viewer for direct user clicks.

E18 (Brandon 2026-05-17): delegates to ActionExecutor so the Wayland-vs-X11
input routing (ydotool vs xdotool) is shared with the Computer Use agent path.
Previously this module used xdotool directly, which silently failed on native
Wayland apps.
"""

from Orchestrator.browser.config import DISPLAY_WIDTH, DISPLAY_HEIGHT
from Orchestrator.browser.actions import ActionExecutor


# Single executor instance for the live viewer endpoints. ActionExecutor's
# Wayland/ydotool detection happens once at construction, then sticks for the
# lifetime of the process. Manual clicks share the same routing decision as
# the CU agent.
_EXECUTOR = ActionExecutor()


def _executor_for(session_id: str = None) -> ActionExecutor:
    """Session-aware executor resolution (M5, 2026-07-23): a session_id naming
    a live virtual display gets an executor bound to THAT display; otherwise
    the native-desktop singleton. (The Splashtop viewer drives sessions over
    RFB directly; this covers the legacy /browser/* manual endpoints.)"""
    if session_id:
        from Orchestrator.browser.display import get_allocator
        h = get_allocator().get(session_id)
        if h is not None:
            return ActionExecutor(display_number=h.display_num, native_mode=False,
                                  resolution=(h.width, h.height))
    return _EXECUTOR


def _scale_xy(x: int, y: int, executor: ActionExecutor = None) -> tuple[int, int]:
    """Clamp to the target surface's model resolution. Model-space ->
    desktop-pixel scaling happens in ONE place — the executor's to_native
    (2026-07-23 fix: this helper ALSO pre-scaled via get_scale_factors, so
    every native-mode manual click was scaled twice and landed ~2.7x off
    target / off-screen on wide displays). A session-bound executor clamps to
    ITS display's resolution (a 1440x900 gemini surface is reachable edge to
    edge); the default clamps to the global CU model resolution."""
    max_w, max_h = (executor.resolution
                    if executor is not None and executor.resolution
                    else (DISPLAY_WIDTH, DISPLAY_HEIGHT))
    x = max(0, min(int(x), max_w))
    y = max(0, min(int(y), max_h))
    return x, y


def click(x: int, y: int, button: str = "left", session_id: str = None) -> dict:
    ex = _executor_for(session_id)
    real_x, real_y = _scale_xy(x, y, ex)
    if button == "double":
        result = ex.execute("double_click", coordinate=[real_x, real_y])
        return {"success": result.get("success", False), "action": "double_click", "x": x, "y": y}
    action = {"left": "left_click", "middle": "middle_click", "right": "right_click"}.get(button, "left_click")
    result = ex.execute(action, coordinate=[real_x, real_y])
    return {"success": result.get("success", False), "action": "click", "x": x, "y": y, "button": button}


def type_text(text: str, session_id: str = None) -> dict:
    if not text:
        return {"success": False, "error": "Empty text"}
    result = _executor_for(session_id).execute("type", text=text)
    return {"success": result.get("success", False), "action": "type", "length": len(text)}


def press_key(key: str, session_id: str = None) -> dict:
    if not key:
        return {"success": False, "error": "Empty key"}
    # Keep the existing safety check — the manual /browser/key endpoint should
    # only accept simple key names from untrusted UI input.
    allowed_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+_")
    if not all(c in allowed_chars for c in key):
        return {"success": False, "error": f"Invalid key name: {key}"}
    result = _executor_for(session_id).execute("key", text=key)
    return {"success": result.get("success", False), "action": "key", "key": key}


def scroll(x: int, y: int, direction: str = "down", clicks: int = 3, session_id: str = None) -> dict:
    ex = _executor_for(session_id)
    real_x, real_y = _scale_xy(x, y, ex)
    clicks = max(1, min(int(clicks), 10))
    result = ex.execute(
        "scroll", coordinate=[real_x, real_y], direction=direction, amount=clicks)
    return {"success": result.get("success", False), "action": "scroll", "direction": direction, "clicks": clicks}
