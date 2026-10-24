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

Implements every action of the computer_20251124 tool, which are also the 17
members of the 5.5-tier computer_toolset_20260801 (driver_anthropic translates
member blocks to these names): screenshot, left_click, right_click,
middle_click, double_click, triple_click, type, key, mouse_move, scroll,
left_click_drag, left_mouse_down, left_mouse_up, hold_key, wait, zoom and
cursor_position. The remote VNC path (execute_remote_action) covers the same
set.

Every action reports what actually happened: a non-zero exit or a timeout of
the input tool is a FAILED action (audit O13-computer-use-04), never a success
on a stale screen.
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


def _clamp_repeat(repeat) -> int:
    """The CU `key` action's repeat count: 1-100, default 1."""
    try:
        return max(1, min(100, int(repeat)))
    except (TypeError, ValueError):
        return 1


# One model scroll call spawned one input process per tick with no upper bound,
# so scroll_amount=5000 (or a raw Gemini magnitude) blocked the executor for
# minutes (audit O13-computer-use-07). The manual endpoint already caps at 10.
_SCROLL_MAX_TICKS = 50
_SCROLL_DEFAULT_TICKS = 3


def _clamp_scroll(amount) -> int:
    """Scroll ticks for one action: 1-50, default 3 (non-numeric -> default)."""
    if amount is None:
        return _SCROLL_DEFAULT_TICKS
    try:
        return max(1, min(_SCROLL_MAX_TICKS, int(float(amount))))
    except (TypeError, ValueError):
        return _SCROLL_DEFAULT_TICKS


# Input-tool subprocess budget. `type` runs at a 12 ms key delay (ydotool adds a
# 20 ms key hold), so a flat 10 s timeout killed the child at ~830 characters
# while the action still said "Typed N chars". Text is typed in chunks, each
# with a timeout sized to its length.
_INPUT_TIMEOUT_S = 10.0
_TYPE_CHUNK_CHARS = 200
_TYPE_SECS_PER_CHAR = 0.05


def _type_timeout(n_chars: int) -> float:
    return _INPUT_TIMEOUT_S + _TYPE_SECS_PER_CHAR * max(0, n_chars)


class InputActionFailed(Exception):
    """The input tool exited non-zero or timed out: the action did NOT happen."""


def _check(proc, tool: str, verb: str) -> subprocess.CompletedProcess:
    """Raise InputActionFailed unless ``proc`` exited 0. The message carries the
    tool's last stderr line (never the typed text, which is only argv)."""
    if proc is None or proc.returncode != 0:
        lines = ((getattr(proc, "stderr", "") or getattr(proc, "stdout", "") or "")
                 .strip().splitlines())
        reason = lines[-1][:200] if lines else f"exit code {getattr(proc, 'returncode', '?')}"
        raise InputActionFailed(f"{tool} {verb} failed: {reason}")
    return proc


def _run_xdotool(*args, display_number: int = ACTIVE_DISPLAY,
                 native: Optional[bool] = None,
                 timeout: float = _INPUT_TIMEOUT_S) -> subprocess.CompletedProcess:
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
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, returncode=-1, stdout="",
            stderr=f"ACTION_TIMEOUT: xdotool command timed out after {timeout:g}s")


def _run_ydotool(*args, timeout: float = _INPUT_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run a ydotool command. Daemon must already be running (via ydotoold.service)."""
    env = {**os.environ, "YDOTOOL_SOCKET": YDOTOOL_SOCKET, "PATH": "/usr/local/bin:/usr/bin:/bin"}
    cmd = [YDOTOOL_BIN] + list(args)
    try:
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, returncode=-1, stdout="",
            stderr=f"ACTION_TIMEOUT: ydotool command timed out after {timeout:g}s")


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
    _check(_run_ydotool("key", *seq), "ydotool", "key")
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
        # Where this executor last put the pointer, in MODEL space. ydotool
        # writes to /dev/uinput and cannot read the pointer back, so on that
        # path cursor_position answers from here.
        self._last_pointer: Optional[tuple] = None

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

    def from_native(self, x: int, y: int) -> tuple:
        """Inverse of :meth:`to_native`: native desktop pixels -> model space
        (cursor_position answers in the coordinates the model clicks in)."""
        from Orchestrator.browser.config import (
            detect_native_resolution, CU_DISPLAY_WIDTH, CU_DISPLAY_HEIGHT,
        )
        if self.coord_space == COORD_SPACE_GEMINI:
            if self.native_mode:
                w, h = detect_native_resolution()
            elif self.resolution:
                w, h = self.resolution
            else:
                from Orchestrator.gemini_cu.config import GEMINI_CU_WIDTH, GEMINI_CU_HEIGHT
                w, h = GEMINI_CU_WIDTH, GEMINI_CU_HEIGHT
            return (min(999, int(x * 1000 / w)) if w else 0,
                    min(999, int(y * 1000 / h)) if h else 0)
        if not self.native_mode:
            return int(x), int(y)
        w, h = detect_native_resolution()
        return (int(round(x * CU_DISPLAY_WIDTH / w)) if w else 0,
                int(round(y * CU_DISPLAY_HEIGHT / h)) if h else 0)

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
    # Each one raises InputActionFailed when the tool reports failure, which
    # execute() turns into {"success": False, ...} for the model.

    def _xdo(self, *args, timeout: float = _INPUT_TIMEOUT_S):
        return _check(_run_xdotool(*args, display_number=self.display_number,
                                   native=self.native_mode, timeout=timeout),
                      "xdotool", args[0])

    def _ydo(self, *args, timeout: float = _INPUT_TIMEOUT_S):
        return _check(_run_ydotool(*args, timeout=timeout), "ydotool", args[0])

    def _move(self, x: int, y: int) -> None:
        if self.use_ydotool:
            self._ydo("mousemove", "--absolute", "--", str(x), str(y))
            return
        # xdotool 3.20160805's `mousemove --sync` waits for the pointer to LEAVE
        # where it started, so a move onto the pointer's own position never
        # returns: a second scroll at one spot, a click after a hover, or the
        # first scroll_document on a fresh Xvfb (pointer at the centre) timed
        # out and failed without acting. Skip a move that is already done, and
        # accept a timed-out one when the pointer did land on the target.
        if self._pointer_at(x, y):
            return
        proc = _run_xdotool("mousemove", "--sync", str(x), str(y),
                            display_number=self.display_number, native=self.native_mode)
        if proc.returncode != 0 and self._pointer_at(x, y, clamp=True):
            return
        _check(proc, "xdotool", "mousemove")

    def _pointer_xy(self) -> Optional[tuple]:
        """The live pointer in native pixels, or None when xdotool can't say."""
        proc = _run_xdotool("getmouselocation", "--shell",
                            display_number=self.display_number, native=self.native_mode)
        if proc is None or proc.returncode != 0:
            return None
        fields = dict(line.split("=", 1) for line in (proc.stdout or "").splitlines()
                      if "=" in line)
        try:
            return int(fields["X"]), int(fields["Y"])
        except (KeyError, ValueError):
            return None

    def _pointer_at(self, x: int, y: int, clamp: bool = False) -> bool:
        """True when the pointer is on native pixel (x, y). With ``clamp`` the
        target is first clamped to the screen, as the X server clamps a warp
        past the edge."""
        pos = self._pointer_xy()
        if pos is None:
            return False
        if clamp:
            proc = _run_xdotool("getdisplaygeometry",
                                display_number=self.display_number, native=self.native_mode)
            try:
                w, h = (int(v) for v in (proc.stdout or "").split()[:2])
                x, y = max(0, min(w - 1, x)), max(0, min(h - 1, y))
            except (TypeError, ValueError):
                pass
        return pos == (int(x), int(y))

    def _move_to(self, coordinate) -> None:
        """Move to a MODEL-space coordinate and remember it for cursor_position."""
        x, y = self._scale_coord(coordinate)
        self._move(x, y)
        self._last_pointer = (int(coordinate[0]), int(coordinate[1]))

    def _click_button(self, button: int) -> None:
        """button: 1=left, 2=middle, 3=right (xdotool numbering)."""
        if self.use_ydotool:
            # ydotool: 0xC0=left, 0xC1=right, 0xC2=middle (0x40=down, 0x80=up combined)
            btn_map = {1: "0xC0", 2: "0xC2", 3: "0xC1"}
            self._ydo("click", btn_map.get(button, "0xC0"))
        else:
            self._xdo("click", str(button))

    def _click_button_repeat(self, button: int, repeat: int, delay_ms: int = 80) -> None:
        if self.use_ydotool:
            btn_map = {1: "0xC0", 2: "0xC2", 3: "0xC1"}
            self._ydo("click", "--repeat", str(repeat),
                      "--next-delay", str(delay_ms), btn_map.get(button, "0xC0"))
        else:
            self._xdo("click", "--repeat", str(repeat), "--delay", str(delay_ms), str(button))

    def _button_down(self, button: int) -> None:
        if self.use_ydotool:
            # ydotool down: 0x40=left-down, 0x42=middle-down, 0x41=right-down
            down_map = {1: "0x40", 2: "0x42", 3: "0x41"}
            self._ydo("click", down_map.get(button, "0x40"))
        else:
            self._xdo("mousedown", str(button))

    def _button_up(self, button: int) -> None:
        if self.use_ydotool:
            up_map = {1: "0x80", 2: "0x82", 3: "0x81"}
            self._ydo("click", up_map.get(button, "0x80"))
        else:
            self._xdo("mouseup", str(button))

    def _type_text(self, text: str) -> None:
        """Type ``text`` in bounded chunks, each after ``--`` so text that starts
        with '-' is never parsed as an option, and with ydotool's escape
        processing off so a backslash is typed as a backslash (audit
        O13-computer-use-18). A failed chunk stops typing and says how much of
        the text landed."""
        done = 0
        for start in range(0, len(text), _TYPE_CHUNK_CHARS):
            chunk = text[start:start + _TYPE_CHUNK_CHARS]
            timeout = _type_timeout(len(chunk))
            try:
                if self.use_ydotool:
                    self._ydo("type", "--escape=0", "--key-delay", "12", "--", chunk,
                              timeout=timeout)
                else:
                    self._xdo("type", "--clearmodifiers", "--delay", "12", "--", chunk,
                              timeout=timeout)
            except InputActionFailed as e:
                raise InputActionFailed(
                    f"{e} (typed {done} of {len(text)} chars)") from None
            done += len(chunk)

    def _key_combo(self, text: str) -> None:
        """Press a key combo like 'Return' or 'ctrl+a'."""
        if self.use_ydotool:
            if _ydotool_key(text):
                return
            # Fall through: unknown key → try xdotool (silent no-op on Wayland,
            # but better than crashing — and useful if XWayland window is focused)
            print(f"[actions] ydotool can't translate key '{text}', falling through to xdotool")
        self._xdo("key", "--clearmodifiers", text)

    def _key_down(self, text: str) -> None:
        if self.use_ydotool:
            codes = _parse_xdotool_key_combo(text)
            if codes:
                self._ydo("key", *[f"{kc}:1" for kc in codes])
                return
        self._xdo("keydown", "--clearmodifiers", text)

    def _key_up(self, text: str) -> None:
        if self.use_ydotool:
            codes = _parse_xdotool_key_combo(text)
            if codes:
                # Release in reverse order
                self._ydo("key", *[f"{kc}:0" for kc in reversed(codes)])
                return
        self._xdo("keyup", "--clearmodifiers", text)

    # --- Click actions ---

    def _action_left_click(self, coordinate=None, **kw) -> dict:
        if coordinate:
            self._move_to(coordinate)
            time.sleep(_jitter())
        self._click_button(1)
        return {"success": True, "message": f"Left click at {coordinate}"}

    def _action_right_click(self, coordinate=None, **kw) -> dict:
        if coordinate:
            self._move_to(coordinate)
            time.sleep(_jitter())
        self._click_button(3)
        return {"success": True, "message": f"Right click at {coordinate}"}

    def _action_middle_click(self, coordinate=None, **kw) -> dict:
        if coordinate:
            self._move_to(coordinate)
            time.sleep(_jitter())
        self._click_button(2)
        return {"success": True, "message": f"Middle click at {coordinate}"}

    def _action_double_click(self, coordinate=None, **kw) -> dict:
        if coordinate:
            self._move_to(coordinate)
            time.sleep(_jitter())
        self._click_button_repeat(1, 2, 80)
        return {"success": True, "message": f"Double click at {coordinate}"}

    def _action_triple_click(self, coordinate=None, **kw) -> dict:
        if coordinate:
            self._move_to(coordinate)
            time.sleep(_jitter())
        self._click_button_repeat(1, 3, 80)
        return {"success": True, "message": f"Triple click at {coordinate}"}

    # --- Type/Key actions ---

    def _action_type(self, text="", **kw) -> dict:
        text = "" if text is None else str(text)
        time.sleep(_jitter(30))
        self._type_text(text)
        return {"success": True, "message": f"Typed {len(text)} chars"}

    def _action_key(self, text="", repeat=1, **kw) -> dict:
        # Anthropic sends combos like "ctrl+a" or "Return"; `repeat` (1-100)
        # presses it N times — ignoring it would press once.
        repeat = _clamp_repeat(repeat)
        time.sleep(_jitter())
        for i in range(repeat):
            if i:
                time.sleep(_jitter())
            self._key_combo(text)
        suffix = f" x{repeat}" if repeat > 1 else ""
        return {"success": True, "message": f"Key press: {text}{suffix}"}

    # --- Mouse movement ---

    def _action_mouse_move(self, coordinate=None, **kw) -> dict:
        if coordinate:
            self._move_to(coordinate)
        return {"success": True, "message": f"Mouse moved to {coordinate}"}

    def _action_cursor_position(self, **kw) -> dict:
        """Report the pointer position in MODEL space (W19: this action used to
        come back "Unknown action", which on the 5.5 toolset failed the whole
        batch). xdotool reads the live pointer; ydotool cannot read anything
        back, so that path answers with the last position this executor moved
        to, and falls back to xdotool (XWayland's pointer) when it never moved.
        """
        if self.use_ydotool and self._last_pointer is not None:
            mx, my = self._last_pointer
        else:
            out = self._xdo("getmouselocation", "--shell").stdout or ""
            fields = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
            try:
                nx, ny = int(fields["X"]), int(fields["Y"])
            except (KeyError, ValueError):
                raise InputActionFailed(
                    f"xdotool getmouselocation returned no position: {out.strip()[:120]!r}")
            mx, my = self.from_native(nx, ny)
        return {"success": True, "message": f"Cursor position: ({mx}, {my})",
                "coordinate": [mx, my]}

    # --- Scroll ---

    def _action_scroll(self, coordinate=None, direction=None, amount=None,
                       scroll_direction=None, scroll_amount=None, **kw) -> dict:
        # Claude names these scroll_direction/scroll_amount; the Gemini/OpenAI
        # loops pass direction/amount. Without the aliases every Claude scroll
        # fell back to "down x3".
        direction = direction or scroll_direction or "down"
        clicks = _clamp_scroll(amount if amount is not None else scroll_amount)
        if coordinate:
            self._move_to(coordinate)
            time.sleep(_jitter())

        if self.use_ydotool:
            # ydotool wheel: --wheel + relative Y. Positive Y = down, negative = up.
            # Each "tick" ≈ 120 wheel units (REL_WHEEL standard).
            # Anthropic's amount=N is meant as ~N*100px of scroll; map 1 tick per N.
            direction_sign = {"up": -1, "down": 1, "left": 0, "right": 0}.get(direction, 1)
            if direction in ("left", "right"):
                # Horizontal scroll: use --wheel with -x
                horiz_sign = {"left": -1, "right": 1}[direction]
                for _ in range(clicks):
                    self._ydo("mousemove", "--wheel", "--", str(horiz_sign), "0")
                    time.sleep(0.02)
            else:
                for _ in range(clicks):
                    self._ydo("mousemove", "--wheel", "--", "0", str(direction_sign))
                    time.sleep(0.02)
        else:
            # xdotool: button 4=up, 5=down, 6=left, 7=right
            button_map = {"up": "4", "down": "5", "left": "6", "right": "7"}
            button = button_map.get(direction, "5")
            for _ in range(clicks):
                self._xdo("click", button)
                time.sleep(0.02)
        return {"success": True, "message": f"Scroll {direction} x{clicks} ticks at {coordinate}"}

    # --- Drag ---

    def _action_left_click_drag(self, start_coordinate=None, coordinate=None, **kw) -> dict:
        if start_coordinate and coordinate:
            sx, sy = self._scale_coord(start_coordinate)
            ex, ey = self._scale_coord(coordinate)
            self._move_to(start_coordinate)
            time.sleep(_jitter(50))
            self._button_down(1)
            try:
                time.sleep(0.1)
                self._move_to(coordinate)
                time.sleep(0.1)
            finally:
                # Never leave the button held: a failed move still releases it
                # (the failure itself is what the model sees).
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
        try:
            time.sleep(capped_duration)
        finally:
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
        if not _valid_region(region):
            return {"success": False, "message": "Zoom requires region [x0, y0, x1, y1]"}
        # Actual zoom/crop is handled in driver_anthropic when building the tool_result
        return {"success": True, "message": f"Zoom region: {region}"}


def _valid_region(region) -> bool:
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        return False
    try:
        x0, y0, x1, y1 = (float(v) for v in region)
    except (TypeError, ValueError):
        return False
    return x1 > x0 and y1 > y0


# Last pointer position per remote device, in the coordinates the model sent.
# vncdotool has no "where is the pointer" query, so cursor_position on a remote
# desktop answers with where this box last put it.
_REMOTE_POINTER: dict = {}


async def execute_remote_action(device_id: str, action: str, **params) -> dict:
    """Execute an action on a remote desktop via VNC over Tailscale.

    The VNCClient methods raise on a failed or timed-out vncdotool call, so a
    dead link reports success=False instead of a silent success."""
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

    def _point(default=(0, 0)):
        coord = params.get("coordinate") or default
        return int(coord[0]), int(coord[1])

    async def _click_at(button=1, times=1):
        x, y = _point()
        for _ in range(times):
            await client.click(x, y, button=button)
        _REMOTE_POINTER[device_id] = (x, y)

    try:
        if action == "left_click":
            await _click_at()
        elif action == "right_click":
            await _click_at(button=3)
        elif action == "middle_click":
            await _click_at(button=2)
        elif action == "double_click":
            await _click_at(times=2)
        elif action == "triple_click":
            await _click_at(times=3)
        elif action == "type":
            text = params.get("text", "") or ""
            if text:  # vncdotool's `type` with no text crashes instead of no-oping
                await client.type_text(text)
        elif action == "key":
            for _ in range(_clamp_repeat(params.get("repeat", 1))):
                await client.key(params.get("text", ""))
        elif action == "mouse_move":
            x, y = _point()
            await client.move(x, y)
            _REMOTE_POINTER[device_id] = (x, y)
        elif action == "scroll":
            direction = params.get("direction") or params.get("scroll_direction") or "down"
            amount = params.get("amount")
            if amount is None:
                amount = params.get("scroll_amount")
            if params.get("coordinate"):
                x, y = _point()
                await client.move(x, y)
                _REMOTE_POINTER[device_id] = (x, y)
            # VNC scroll: button 4=up, 5=down
            button = 4 if direction == "up" else 5
            for _ in range(_clamp_scroll(amount)):
                await client.scroll_click(button)
        elif action == "left_click_drag":
            start = params.get("start_coordinate", [0, 0])
            end = params.get("coordinate", [0, 0])
            await client.move(start[0], start[1])
            await client.mouse_down(1)
            try:
                await client.move(end[0], end[1])
            finally:
                await client.mouse_up(1)
            _REMOTE_POINTER[device_id] = (int(end[0]), int(end[1]))
        elif action == "left_mouse_down":
            await client.mouse_down(1)
        elif action == "left_mouse_up":
            await client.mouse_up(1)
        elif action == "cursor_position":
            pos = _REMOTE_POINTER.get(device_id)
            if pos is None:
                return {"success": False,
                        "error": "Cursor position unknown: the pointer has not been moved "
                                 "on this remote desktop yet"}
            return {"success": True, "action": action, "device_id": device_id,
                    "message": f"Cursor position: ({pos[0]}, {pos[1]})",
                    "coordinate": [pos[0], pos[1]]}
        elif action == "zoom":
            # The driver crops the next screenshot to the region; only validate here.
            if not _valid_region(params.get("region")):
                return {"success": False, "error": "Zoom requires region [x0, y0, x1, y1]"}
            return {"success": True, "action": "zoom", "device_id": device_id}
        elif action == "screenshot":
            return {"success": True, "action": "screenshot"}
        elif action == "wait":
            import asyncio
            await asyncio.sleep(min(float(params.get("duration", 1) or 0), 10))
            return {"success": True, "action": "wait"}
        elif action == "hold_key":
            # hold_key: press key, wait, release (Anthropic CU sends "text" for key name)
            key_name = params.get("text", "") or params.get("key", "")
            duration = min(float(params.get("duration", 0.5) or 0), 10)
            await client.key_down(key_name)
            import asyncio
            try:
                await asyncio.sleep(duration)
            finally:
                await client.key_up(key_name)
        else:
            return {"success": False, "error": f"Unsupported remote action: {action}"}

        return {"success": True, "action": action, "device_id": device_id}
    except Exception as e:
        return {"success": False, "error": str(e)}
