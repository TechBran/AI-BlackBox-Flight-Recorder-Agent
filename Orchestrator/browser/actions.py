"""
Action executor — translates Anthropic Computer Use actions into native input
injection on the active display.

E18 (Brandon 2026-05-17): added ydotool path for Wayland-compatible input.
xdotool only reaches XWayland windows; native Wayland apps don't see its
events. ydotool writes to /dev/uinput at the kernel layer so both X11 and
Wayland apps receive events. Wayland is detected by the presence of
$WAYLAND_DISPLAY (or wayland-0 socket under $XDG_RUNTIME_DIR); X11 falls
through to the existing xdotool path so nothing regresses on plain-X11
hosts.

Supports all 18 actions from the computer_20251124 tool type.
"""
import os
import subprocess
import time
import random
import io
from typing import Optional

from Orchestrator.browser.config import (
    DISPLAY_NUMBER, DISPLAY_WIDTH, DISPLAY_HEIGHT,
    NATIVE_MODE, ACTIVE_DISPLAY,
    get_native_env
)


# ── Wayland detection + ydotool config ──
YDOTOOL_BIN = "/usr/local/bin/ydotool"  # built from v1.0.4 source by install.sh
# Socket lives in /run/user/<uid>/ so it survives blackbox.service's
# PrivateTmp=true sandbox (which would mask /tmp). /run/user/<uid> is the
# standard Linux user-runtime dir that systemd preserves verbatim.
YDOTOOL_SOCKET = f"/run/user/{os.getuid()}/.ydotool_socket"


def _is_wayland_session() -> bool:
    """Detect whether the active session is Wayland.

    blackbox.service runs under systemd which strips XDG_SESSION_TYPE, so we
    can't rely on that env var. Instead probe for the Wayland display socket
    which Mutter creates inside the user's XDG_RUNTIME_DIR.
    """
    uid = os.getuid()
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    # Check for any wayland-* socket (typically wayland-0)
    try:
        for entry in os.listdir(runtime_dir):
            if entry.startswith("wayland-") and not entry.endswith(".lock"):
                return True
    except (FileNotFoundError, PermissionError):
        pass
    return False


def _ydotool_available() -> bool:
    """Check ydotool binary exists AND daemon socket is reachable."""
    if not os.path.isfile(YDOTOOL_BIN):
        return False
    try:
        st = os.stat(YDOTOOL_SOCKET)
        # Socket present + writable by us
        import stat as _stat
        return _stat.S_ISSOCK(st.st_mode) and os.access(YDOTOOL_SOCKET, os.W_OK)
    except FileNotFoundError:
        return False


# Cache the routing decision — re-evaluated on each ActionExecutor() construction
# is fine, but we don't want to stat() on every single key press.
_USE_YDOTOOL = None

def _use_ydotool() -> bool:
    global _USE_YDOTOOL
    if _USE_YDOTOOL is None:
        _USE_YDOTOOL = _is_wayland_session() and _ydotool_available()
        if _USE_YDOTOOL:
            print(f"[actions] Wayland detected + ydotool available — routing input via ydotool")
        else:
            print(f"[actions] Using xdotool (wayland={_is_wayland_session()}, ydotool_ok={_ydotool_available()})")
    return _USE_YDOTOOL


# xdotool keysym name → Linux input event keycode (from /usr/include/linux/input-event-codes.h)
# Covers the keys Anthropic CU commonly sends. Unknown keys fall through to xdotool
# (which on Wayland silently no-ops, but at least doesn't crash).
_XDOTOOL_TO_LINUX_KEYCODE = {
    # Letters (a-z)
    "a": 30, "b": 48, "c": 46, "d": 32, "e": 18, "f": 33, "g": 34, "h": 35,
    "i": 23, "j": 36, "k": 37, "l": 38, "m": 50, "n": 49, "o": 24, "p": 25,
    "q": 16, "r": 19, "s": 31, "t": 20, "u": 22, "v": 47, "w": 17, "x": 45,
    "y": 21, "z": 44,
    # Digits
    "0": 11, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10,
    # Common control keys
    "Return": 28, "return": 28, "Enter": 28, "enter": 28,
    "Escape": 1, "escape": 1, "Esc": 1, "esc": 1,
    "Tab": 15, "tab": 15,
    "BackSpace": 14, "backspace": 14, "Backspace": 14,
    "space": 57, "Space": 57,
    "Delete": 111, "delete": 111, "Del": 111,
    "Insert": 110, "insert": 110,
    "Home": 102, "home": 102,
    "End": 107, "end": 107,
    "Page_Up": 104, "Prior": 104, "PageUp": 104,
    "Page_Down": 109, "Next": 109, "PageDown": 109,
    "Up": 103, "up": 103,
    "Down": 108, "down": 108,
    "Left": 105, "left": 105,
    "Right": 106, "right": 106,
    # Modifiers
    "ctrl": 29, "Control": 29, "Control_L": 29, "Ctrl": 29,
    "Control_R": 97, "ctrl_r": 97,
    "shift": 42, "Shift": 42, "Shift_L": 42,
    "Shift_R": 54, "shift_r": 54,
    "alt": 56, "Alt": 56, "Alt_L": 56, "Meta": 56, "meta": 56,
    "Alt_R": 100, "alt_r": 100, "AltGr": 100,
    "super": 125, "Super": 125, "Super_L": 125, "Win": 125,
    "Super_R": 126,
    # Function keys
    "F1": 59, "F2": 60, "F3": 61, "F4": 62, "F5": 63, "F6": 64,
    "F7": 65, "F8": 66, "F9": 67, "F10": 68, "F11": 87, "F12": 88,
    # Punctuation/symbols
    "minus": 12, "equal": 13, "plus": 13,
    "bracketleft": 26, "bracketright": 27,
    "semicolon": 39, "apostrophe": 40, "grave": 41,
    "backslash": 43, "comma": 51, "period": 52, "slash": 53,
    # Print Screen and friends
    "Print": 99, "Sys_Req": 99,
    "Scroll_Lock": 70,
    "Pause": 119, "Break": 119,
    "Menu": 127,
    "Caps_Lock": 58, "Capslock": 58,
}


def _jitter(base_ms: float = 0) -> float:
    """Add slight random delay for human-like behavior."""
    return (base_ms + random.uniform(20, 80)) / 1000.0


def _run_xdotool(*args, display_number: int = ACTIVE_DISPLAY,
                 native: Optional[bool] = None) -> subprocess.CompletedProcess:
    """Run an xdotool command against a display.

    ``native`` picks the environment: True -> the real desktop's env
    (XAUTHORITY/DBUS via get_native_env); False -> a clean env pinned to
    ``:display_number`` (a per-session Xvfb). None (legacy callers) falls back
    to the box-global NATIVE_MODE.

    Display-coherence fix (2026-07-23): this function previously ignored the
    passed display_number whenever the box-global NATIVE_MODE was true, so a
    virtual session's clicks landed on the operator's REAL desktop while its
    live view streamed the virtual one.
    """
    use_native = NATIVE_MODE if native is None else bool(native)
    if use_native:
        env = {**os.environ, **get_native_env()}
    else:
        env = {"DISPLAY": f":{display_number}", "PATH": "/usr/bin:/usr/local/bin:/bin"}
    cmd = ["xdotool"] + list(args)
    try:
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=-1, stdout="", stderr="ACTION_TIMEOUT: xdotool command timed out after 10s")


def _run_ydotool(*args) -> subprocess.CompletedProcess:
    """Run a ydotool command. Daemon must already be running (via ydotoold.service)."""
    env = {**os.environ, "YDOTOOL_SOCKET": YDOTOOL_SOCKET, "PATH": "/usr/local/bin:/usr/bin:/bin"}
    cmd = [YDOTOOL_BIN] + list(args)
    try:
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=-1, stdout="", stderr="ACTION_TIMEOUT: ydotool command timed out after 10s")


def _parse_xdotool_key_combo(text: str):
    """Translate xdotool key string ("ctrl+a", "Return", "shift+F4") to list of
    (keycode, is_modifier) tuples ready to be turned into ydotool down/up events.

    Returns None if any token can't be translated — caller should fall back.
    """
    tokens = text.replace(" ", "").split("+")
    codes = []
    for tok in tokens:
        kc = _XDOTOOL_TO_LINUX_KEYCODE.get(tok)
        if kc is None:
            # Try lowercase fallback for letters typed as uppercase like "A"
            kc = _XDOTOOL_TO_LINUX_KEYCODE.get(tok.lower())
        if kc is None:
            return None
        codes.append(kc)
    return codes


def _ydotool_key(text: str) -> bool:
    """Press a key combo via ydotool. Returns True on success, False if unknown key."""
    codes = _parse_xdotool_key_combo(text)
    if not codes:
        return False
    # Build event sequence: all keys down in order, then all up in reverse
    seq = []
    for kc in codes:
        seq.append(f"{kc}:1")
    for kc in reversed(codes):
        seq.append(f"{kc}:0")
    _run_ydotool("key", *seq)
    return True


# Coordinate spaces — import these constants at construction sites instead of
# retyping the string literals (a typo'd space would silently mis-scale clicks).
COORD_SPACE_ANTHROPIC = "anthropic-1280"  # model coords in a 1280x720 virtual screen
COORD_SPACE_GEMINI = "gemini-999"         # model coords normalized 0-999 on both axes
_COORD_SPACES = (COORD_SPACE_ANTHROPIC, COORD_SPACE_GEMINI)


class ActionExecutor:
    """Executes Anthropic Computer Use actions on the native display.

    Auto-detects Wayland vs X11 at construction time and routes input through
    ydotool (Wayland-compatible) or xdotool (X11-only) accordingly.
    """

    def __init__(self, display_number: int = DISPLAY_NUMBER,
                 coord_space: str = COORD_SPACE_ANTHROPIC, native_mode: Optional[bool] = None,
                 resolution: Optional[tuple] = None):
        self.display_number = display_number
        if coord_space not in _COORD_SPACES:
            raise ValueError(
                f"unknown coord_space {coord_space!r}; expected one of {_COORD_SPACES}")
        self.coord_space = coord_space
        self.native_mode = NATIVE_MODE if native_mode is None else native_mode
        # ydotool injects at the kernel seat (/dev/uinput) — events can only
        # ever reach the REAL session, never an Xvfb display. A virtual
        # executor must pin xdotool or its input lands on the operator's
        # desktop (the Wayland half of the 2026-07-23 coherence fix).
        self.use_ydotool = _use_ydotool() if self.native_mode else False
        # The bound display's (width, height) — the de-normalization basis for
        # normalized coord spaces on virtual displays.
        self.resolution = tuple(resolution) if resolution else None

    def to_native(self, x: int, y: int) -> tuple:
        """Convert model-space coordinates to native desktop pixels using the
        LIVE resolution (never the stale import-time constants).

        Imports are deliberately lazy so monkeypatched
        Orchestrator.browser.config.detect_native_resolution (tests) and live
        resolution changes are both honored at call time.
        """
        from Orchestrator.browser.config import (
            detect_native_resolution, CU_DISPLAY_WIDTH, CU_DISPLAY_HEIGHT,
        )
        if not self.native_mode:
            if self.coord_space == COORD_SPACE_GEMINI:
                # Gemini replies 0-999 normalized — de-normalize against the
                # SESSION display's resolution (identity passthrough compressed
                # every click into the top-left ~70% of the screen).
                if self.resolution:
                    w, h = self.resolution
                else:
                    from Orchestrator.gemini_cu.config import (
                        GEMINI_CU_WIDTH, GEMINI_CU_HEIGHT)
                    w, h = GEMINI_CU_WIDTH, GEMINI_CU_HEIGHT
                # /1000 per Google's CU contract (0-999 = 1000 buckets); /999
                # mapped coordinate 999 one past the last pixel (review find).
                return int(x / 1000 * w), int(y / 1000 * h)
            # anthropic-1280: model coords ARE pixels in the frame it saw, and a
            # virtual display renders at exactly that frame size — identity.
            return int(x), int(y)
        w, h = detect_native_resolution()
        if self.coord_space == COORD_SPACE_GEMINI:
            # /1000 per Google's CU contract — see the virtual branch above.
            return int(x / 1000 * w), int(y / 1000 * h)
        # anthropic-1280 (default): same math + int() truncation as the old
        # module-level _scale_coord (x * (w / CU_DISPLAY_WIDTH)) — bit-identical.
        sx, sy = w / CU_DISPLAY_WIDTH, h / CU_DISPLAY_HEIGHT
        return int(x * sx), int(y * sy)

    def _scale_coord(self, coord):
        """Scale a coordinate pair from model space to real display space."""
        if coord is None:
            return coord
        x, y = coord
        return self.to_native(x, y)

    def execute(self, action: str, **params) -> dict:
        """Execute a single CU action. Returns {success, message}."""
        handler = getattr(self, f"_action_{action}", None)
        if handler is None:
            return {"success": False, "message": f"Unknown action: {action}"}
        try:
            return handler(**params)
        except Exception as e:
            return {"success": False, "message": f"Action '{action}' failed: {e}"}

    # ── Input dispatch helpers — choose ydotool or xdotool per op ──

    def _move(self, x: int, y: int) -> None:
        if self.use_ydotool:
            _run_ydotool("mousemove", "--absolute", "--", str(x), str(y))
        else:
            _run_xdotool("mousemove", "--sync", str(x), str(y), display_number=self.display_number, native=self.native_mode)

    def _click_button(self, button: int) -> None:
        """button: 1=left, 2=middle, 3=right (xdotool numbering)."""
        if self.use_ydotool:
            # ydotool: 0xC0=left, 0xC1=right, 0xC2=middle (0x40=down, 0x80=up combined)
            btn_map = {1: "0xC0", 2: "0xC2", 3: "0xC1"}
            _run_ydotool("click", btn_map.get(button, "0xC0"))
        else:
            _run_xdotool("click", str(button), display_number=self.display_number, native=self.native_mode)

    def _click_button_repeat(self, button: int, repeat: int, delay_ms: int = 80) -> None:
        if self.use_ydotool:
            btn_map = {1: "0xC0", 2: "0xC2", 3: "0xC1"}
            _run_ydotool("click", "--repeat", str(repeat),
                         "--next-delay", str(delay_ms), btn_map.get(button, "0xC0"))
        else:
            _run_xdotool("click", "--repeat", str(repeat), "--delay", str(delay_ms), str(button), display_number=self.display_number, native=self.native_mode)

    def _button_down(self, button: int) -> None:
        if self.use_ydotool:
            # ydotool down: 0x40=left-down, 0x42=middle-down, 0x41=right-down
            down_map = {1: "0x40", 2: "0x42", 3: "0x41"}
            _run_ydotool("click", down_map.get(button, "0x40"))
        else:
            _run_xdotool("mousedown", str(button), display_number=self.display_number, native=self.native_mode)

    def _button_up(self, button: int) -> None:
        if self.use_ydotool:
            up_map = {1: "0x80", 2: "0x82", 3: "0x81"}
            _run_ydotool("click", up_map.get(button, "0x80"))
        else:
            _run_xdotool("mouseup", str(button), display_number=self.display_number, native=self.native_mode)

    def _type_text(self, text: str) -> None:
        if self.use_ydotool:
            _run_ydotool("type", "--key-delay", "12", text)
        else:
            _run_xdotool("type", "--clearmodifiers", "--delay", "12", text, display_number=self.display_number, native=self.native_mode)

    def _key_combo(self, text: str) -> None:
        """Press a key combo like 'Return' or 'ctrl+a'."""
        if self.use_ydotool:
            if _ydotool_key(text):
                return
            # Fall through: unknown key → try xdotool (silent no-op on Wayland,
            # but better than crashing — and useful if XWayland window is focused)
            print(f"[actions] ydotool can't translate key '{text}', falling through to xdotool")
        _run_xdotool("key", "--clearmodifiers", text, display_number=self.display_number, native=self.native_mode)

    def _key_down(self, text: str) -> None:
        if self.use_ydotool:
            codes = _parse_xdotool_key_combo(text)
            if codes:
                _run_ydotool("key", *[f"{kc}:1" for kc in codes])
                return
        _run_xdotool("keydown", "--clearmodifiers", text, display_number=self.display_number, native=self.native_mode)

    def _key_up(self, text: str) -> None:
        if self.use_ydotool:
            codes = _parse_xdotool_key_combo(text)
            if codes:
                # Release in reverse order
                _run_ydotool("key", *[f"{kc}:0" for kc in reversed(codes)])
                return
        _run_xdotool("keyup", "--clearmodifiers", text, display_number=self.display_number, native=self.native_mode)

    # --- Click actions ---

    def _action_left_click(self, coordinate=None, **kw) -> dict:
        if coordinate:
            x, y = self._scale_coord(coordinate)
            self._move(x, y)
            time.sleep(_jitter())
        self._click_button(1)
        return {"success": True, "message": f"Left click at {coordinate}"}

    def _action_right_click(self, coordinate=None, **kw) -> dict:
        if coordinate:
            x, y = self._scale_coord(coordinate)
            self._move(x, y)
            time.sleep(_jitter())
        self._click_button(3)
        return {"success": True, "message": f"Right click at {coordinate}"}

    def _action_middle_click(self, coordinate=None, **kw) -> dict:
        if coordinate:
            x, y = self._scale_coord(coordinate)
            self._move(x, y)
            time.sleep(_jitter())
        self._click_button(2)
        return {"success": True, "message": f"Middle click at {coordinate}"}

    def _action_double_click(self, coordinate=None, **kw) -> dict:
        if coordinate:
            x, y = self._scale_coord(coordinate)
            self._move(x, y)
            time.sleep(_jitter())
        self._click_button_repeat(1, 2, 80)
        return {"success": True, "message": f"Double click at {coordinate}"}

    def _action_triple_click(self, coordinate=None, **kw) -> dict:
        if coordinate:
            x, y = self._scale_coord(coordinate)
            self._move(x, y)
            time.sleep(_jitter())
        self._click_button_repeat(1, 3, 80)
        return {"success": True, "message": f"Triple click at {coordinate}"}

    # --- Type/Key actions ---

    def _action_type(self, text="", **kw) -> dict:
        time.sleep(_jitter(30))
        self._type_text(text)
        return {"success": True, "message": f"Typed {len(text)} chars"}

    def _action_key(self, text="", **kw) -> dict:
        # Anthropic sends combos like "ctrl+a" or "Return"
        time.sleep(_jitter())
        self._key_combo(text)
        return {"success": True, "message": f"Key press: {text}"}

    # --- Mouse movement ---

    def _action_mouse_move(self, coordinate=None, **kw) -> dict:
        if coordinate:
            x, y = self._scale_coord(coordinate)
            self._move(x, y)
        return {"success": True, "message": f"Mouse moved to {coordinate}"}

    # --- Scroll ---

    def _action_scroll(self, coordinate=None, direction="down", amount=3, **kw) -> dict:
        if coordinate:
            x, y = self._scale_coord(coordinate)
            self._move(x, y)
            time.sleep(_jitter())

        clicks = max(1, int(amount))
        if self.use_ydotool:
            # ydotool wheel: --wheel + relative Y. Positive Y = down, negative = up.
            # Each "tick" ≈ 120 wheel units (REL_WHEEL standard).
            # Anthropic's amount=N is meant as ~N*100px of scroll; map 1 tick per N.
            direction_sign = {"up": -1, "down": 1, "left": 0, "right": 0}.get(direction, 1)
            if direction in ("left", "right"):
                # Horizontal scroll: use --wheel with -x
                horiz_sign = {"left": -1, "right": 1}[direction]
                for _ in range(clicks):
                    _run_ydotool("mousemove", "--wheel", "--", str(horiz_sign), "0")
                    time.sleep(0.02)
            else:
                for _ in range(clicks):
                    _run_ydotool("mousemove", "--wheel", "--", "0", str(direction_sign))
                    time.sleep(0.02)
        else:
            # xdotool: button 4=up, 5=down, 6=left, 7=right
            button_map = {"up": "4", "down": "5", "left": "6", "right": "7"}
            button = button_map.get(direction, "5")
            for _ in range(clicks):
                _run_xdotool("click", button, display_number=self.display_number, native=self.native_mode)
                time.sleep(0.02)
        return {"success": True, "message": f"Scroll {direction} x{clicks} ticks at {coordinate}"}

    # --- Drag ---

    def _action_left_click_drag(self, start_coordinate=None, coordinate=None, **kw) -> dict:
        if start_coordinate and coordinate:
            sx, sy = self._scale_coord(start_coordinate)
            ex, ey = self._scale_coord(coordinate)
            self._move(sx, sy)
            time.sleep(_jitter(50))
            self._button_down(1)
            time.sleep(0.1)
            self._move(ex, ey)
            time.sleep(0.1)
            self._button_up(1)
            return {"success": True, "message": f"Drag ({sx},{sy}) -> ({ex},{ey})"}
        return {"success": False, "message": "Drag requires start_coordinate and coordinate"}

    # --- Mouse button hold ---

    def _action_left_mouse_down(self, **kw) -> dict:
        self._button_down(1)
        return {"success": True, "message": "Mouse button down"}

    def _action_left_mouse_up(self, **kw) -> dict:
        self._button_up(1)
        return {"success": True, "message": "Mouse button up"}

    # --- Key hold ---

    def _action_hold_key(self, text="", duration=1, **kw) -> dict:
        capped_duration = min(float(duration), 10)
        self._key_down(text)
        time.sleep(capped_duration)
        self._key_up(text)
        return {"success": True, "message": f"Held '{text}' for {capped_duration}s"}

    # --- Wait ---

    def _action_wait(self, duration=1, **kw) -> dict:
        capped = min(float(duration), 10)
        time.sleep(capped)
        return {"success": True, "message": f"Waited {capped}s"}

    # --- Screenshot (handled by agent loop, but stub here) ---

    def _action_screenshot(self, **kw) -> dict:
        return {"success": True, "message": "Screenshot captured (handled by agent loop)"}

    # --- Zoom (Opus 4.6 only — crop a region for closer inspection) ---

    def _action_zoom(self, region=None, **kw) -> dict:
        if not region or len(region) != 4:
            return {"success": False, "message": "Zoom requires region [x0, y0, x1, y1]"}
        # Actual zoom/crop is handled in driver_anthropic when building the tool_result
        return {"success": True, "message": f"Zoom region: {region}"}


async def execute_remote_action(device_id: str, action: str, **params) -> dict:
    """Execute an action on a remote desktop via VNC over Tailscale."""
    from Orchestrator.device_registry import get_registry
    from Orchestrator.remote_desktop import VNCClient

    device = get_registry().get_device(device_id)
    if not device:
        return {"success": False, "error": f"Device not found: {device_id}"}

    client = VNCClient(
        device.tailscale_ip,
        device.vnc_port,
        device.metadata.get("vnc_password")
    )

    try:
        if action == "left_click":
            coord = params.get("coordinate", [0, 0])
            await client.click(coord[0], coord[1])
        elif action == "right_click":
            coord = params.get("coordinate", [0, 0])
            await client.click(coord[0], coord[1], button=3)
        elif action == "type":
            await client.type_text(params.get("text", ""))
        elif action == "key":
            await client.key(params.get("text", ""))
        elif action == "mouse_move":
            coord = params.get("coordinate", [0, 0])
            await client.move(coord[0], coord[1])
        elif action == "double_click":
            coord = params.get("coordinate", [0, 0])
            await client.click(coord[0], coord[1])
            await client.click(coord[0], coord[1])
        elif action == "triple_click":
            coord = params.get("coordinate", [0, 0])
            await client.click(coord[0], coord[1])
            await client.click(coord[0], coord[1])
            await client.click(coord[0], coord[1])
        elif action == "middle_click":
            coord = params.get("coordinate", [0, 0])
            await client.click(coord[0], coord[1], button=2)
        elif action == "scroll":
            coord = params.get("coordinate", [0, 0])
            direction = params.get("direction", "down")
            amount = params.get("amount", 3)
            if coord:
                await client.move(coord[0], coord[1])
            # VNC scroll: button 4=up, 5=down
            button = 4 if direction == "up" else 5
            for _ in range(int(amount)):
                await client._run("click", str(button))
        elif action == "left_click_drag":
            start = params.get("start_coordinate", [0, 0])
            end = params.get("coordinate", [0, 0])
            await client.move(start[0], start[1])
            await client._run("mousedown", "1")
            await client.move(end[0], end[1])
            await client._run("mouseup", "1")
        elif action == "screenshot":
            return {"success": True, "action": "screenshot"}
        elif action == "wait":
            import asyncio
            await asyncio.sleep(params.get("duration", 1))
            return {"success": True, "action": "wait"}
        elif action == "hold_key":
            # hold_key: press key, wait, release (Anthropic CU sends "text" for key name)
            key_name = params.get("text", "") or params.get("key", "")
            duration = params.get("duration", 0.5)
            await client._run("keydown", key_name)
            import asyncio
            await asyncio.sleep(duration)
            await client._run("keyup", key_name)
        else:
            return {"success": False, "error": f"Unsupported remote action: {action}"}

        return {"success": True, "action": action, "device_id": device_id}
    except Exception as e:
        return {"success": False, "error": str(e)}
