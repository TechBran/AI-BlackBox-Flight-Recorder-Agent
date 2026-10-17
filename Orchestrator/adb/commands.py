"""
ADB Commands — translates Computer Use actions into ADB shell commands.

Maps Gemini CU normalized coordinates (0-999) to device screen coordinates.

SECURITY (audit O14-cli-agents-devices-01): `adb -s <serial> shell <cmd>` hands
<cmd> to the PHONE's /system/bin/sh, which evaluates it with full expansion —
and adb joins any extra argv words into that same string, so an argv list is
no escape hatch. The text / keys / app names reaching this module come from
POST /adb/* AND from the Gemini CU agent loop, where they are model-generated
(on-screen prompt injection). So every command is built with _sh(), which
shlex-quotes EVERY token (POSIX single quotes stop all device-side expansion),
and package / activity / keycode values are validated before anything is sent.
"""
import asyncio
import base64
import re
import shlex
import tempfile
import os
from typing import Optional, Tuple, Dict, Any
from Orchestrator.adb.manager import get_adb_manager


# Android package name: dot-separated Java identifiers, at least two segments.
_PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")
# Activity: a class name, fully qualified or relative to the package (leading
# "."); "$" appears in inner-class names and is harmless once quoted.
_ACTIVITY_RE = re.compile(r"\.?[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*")
_KEYCODE_RE = re.compile(r"KEYCODE_[A-Z0-9_]+")
# A bare key name ("HOME", "enter") — normalised to KEYCODE_<NAME>.
_KEY_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_MAX_KEYCODE = 9999


def _sh(*tokens) -> str:
    """One device-shell command line with every token shlex-quoted."""
    return " ".join(shlex.quote(str(t)) for t in tokens)


def validate_package_name(package_name) -> str:
    if not isinstance(package_name, str) or not _PACKAGE_RE.fullmatch(package_name):
        raise ValueError(f"invalid Android package name: {package_name!r}")
    return package_name


def validate_activity_name(activity) -> str:
    if not isinstance(activity, str) or not _ACTIVITY_RE.fullmatch(activity):
        raise ValueError(f"invalid Android activity name: {activity!r}")
    return activity


def normalize_keycode(keycode) -> str:
    """Return a keycode `input keyevent` accepts: a non-negative int, or
    KEYCODE_<NAME>. A bare name ("HOME", "enter") becomes KEYCODE_<UPPER>.
    Anything else (key combos like "ctrl+a", shell syntax) is refused."""
    if isinstance(keycode, bool):
        raise ValueError(f"invalid keycode: {keycode!r}")
    if isinstance(keycode, int):
        if 0 <= keycode <= _MAX_KEYCODE:
            return str(keycode)
        raise ValueError(f"invalid keycode: {keycode!r}")
    if not isinstance(keycode, str):
        raise ValueError(f"invalid keycode: {keycode!r}")
    if keycode.isascii() and keycode.isdigit() and int(keycode) <= _MAX_KEYCODE:
        return keycode
    if _KEYCODE_RE.fullmatch(keycode):
        return keycode
    if _KEY_NAME_RE.fullmatch(keycode) and not keycode.upper().startswith("KEYCODE_"):
        return f"KEYCODE_{keycode.upper()}"
    raise ValueError(
        f"invalid keycode: {keycode!r} (use an Android key code number or "
        f"KEYCODE_<NAME>; key combinations are not supported)")


def _invalid(action: str, error: Exception, **fields) -> Dict:
    return {"success": False, "action": action, "invalid_input": True,
            "error": str(error), **fields}


class ADBCommands:
    """Execute actions on an Android device via ADB."""

    def __init__(self, device_id: str, screen_width: int = 1080, screen_height: int = 2400):
        self.device_id = device_id
        self.screen_width = screen_width
        self.screen_height = screen_height
        self._adb = get_adb_manager()

    def _get_serial(self) -> str:
        from Orchestrator.device_registry import get_registry
        device = get_registry().get_device(self.device_id)
        return device.connection_string() if device else ""

    async def _shell(self, command: str, timeout: int = 10) -> Tuple[int, str]:
        serial = self._get_serial()
        rc, stdout, stderr = await self._adb._run_adb(
            "-s", serial, "shell", command, timeout=timeout
        )
        return rc, stdout

    def denormalize_coords(self, x: int, y: int) -> Tuple[int, int]:
        """Convert Gemini normalized coords (0-999) to device pixel coords."""
        real_x = int(x / 999 * self.screen_width)
        real_y = int(y / 999 * self.screen_height)
        return real_x, real_y

    async def detect_screen_size(self) -> Tuple[int, int]:
        rc, output = await self._shell(_sh("wm", "size"))
        if "x" in output:
            size_str = output.split(":")[-1].strip()
            w, h = size_str.split("x")
            self.screen_width = int(w)
            self.screen_height = int(h)
        return self.screen_width, self.screen_height

    async def screenshot(self) -> bytes:
        """Capture a screenshot from the device. Returns PNG bytes."""
        serial = self._get_serial()
        remote_path = "/sdcard/blackbox_screenshot.png"
        await self._shell(_sh("screencap", "-p", remote_path))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            rc, stdout, stderr = await self._adb._run_adb(
                "-s", serial, "pull", remote_path, tmp_path, timeout=15
            )
            if rc != 0:
                raise RuntimeError(f"ADB pull failed: {stderr}")
            with open(tmp_path, "rb") as f:
                png_bytes = f.read()
            return png_bytes
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            await self._shell(_sh("rm", "-f", remote_path))

    async def screenshot_base64(self) -> str:
        png_bytes = await self.screenshot()
        return base64.b64encode(png_bytes).decode()

    async def tap(self, x: int, y: int, normalized: bool = True) -> Dict:
        orig_x, orig_y = x, y
        if normalized:
            x, y = self.denormalize_coords(x, y)
        print(f"[ADB TAP] norm({orig_x},{orig_y}) -> pixel({x},{y}) screen={self.screen_width}x{self.screen_height}")
        rc, output = await self._shell(_sh("input", "tap", x, y))
        return {"success": rc == 0, "action": "tap", "x": x, "y": y}

    async def long_press(self, x: int, y: int, duration_ms: int = 1000,
                         normalized: bool = True) -> Dict:
        if normalized:
            x, y = self.denormalize_coords(x, y)
        rc, output = await self._shell(_sh("input", "swipe", x, y, x, y, duration_ms))
        return {"success": rc == 0, "action": "long_press", "x": x, "y": y}

    async def swipe(self, x1: int, y1: int, x2: int, y2: int,
                    duration_ms: int = 300, normalized: bool = True) -> Dict:
        if normalized:
            x1, y1 = self.denormalize_coords(x1, y1)
            x2, y2 = self.denormalize_coords(x2, y2)
        rc, output = await self._shell(_sh("input", "swipe", x1, y1, x2, y2, duration_ms))
        return {"success": rc == 0, "action": "swipe", "from": [x1, y1], "to": [x2, y2]}

    async def type_text(self, text: str) -> Dict:
        # `input text` reads "%s" as a space; everything else is typed as-is,
        # so after the space rewrite the only job left is quoting.
        rc, output = await self._shell(_sh("input", "text", text.replace(" ", "%s")))
        return {"success": rc == 0, "action": "type", "text": text}

    async def key_event(self, keycode: str) -> Dict:
        try:
            code = normalize_keycode(keycode)
        except ValueError as e:
            return _invalid("key", e, keycode=keycode)
        rc, output = await self._shell(_sh("input", "keyevent", code))
        return {"success": rc == 0, "action": "key", "keycode": code}

    async def go_home(self) -> Dict:
        return await self.key_event("KEYCODE_HOME")

    async def go_back(self) -> Dict:
        return await self.key_event("KEYCODE_BACK")

    async def open_app(self, package_name: str, activity: Optional[str] = None) -> Dict:
        try:
            validate_package_name(package_name)
            if activity:
                validate_activity_name(activity)
        except ValueError as e:
            return _invalid("open_app", e, package=package_name)
        if activity:
            rc, output = await self._shell(
                _sh("am", "start", "-n", f"{package_name}/{activity}"))
        else:
            rc, output = await self._shell(
                _sh("monkey", "-p", package_name, "-c",
                    "android.intent.category.LAUNCHER", "1"))
        return {"success": rc == 0, "action": "open_app", "package": package_name}

    async def get_current_app(self) -> str:
        rc, output = await self._shell(
            "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'"
        )
        return output

    async def scroll_down(self, x: int = 500, y: int = 500,
                          normalized: bool = True) -> Dict:
        if normalized:
            x, y = self.denormalize_coords(x, y)
        # Scroll ~40% of screen height, clamped to valid range
        scroll_dist = int(self.screen_height * 0.4)
        end_y = max(0, y - scroll_dist)
        print(f"[ADB SCROLL DOWN] at ({x},{y}) -> ({x},{end_y}) dist={scroll_dist}")
        rc, output = await self._shell(_sh("input", "swipe", x, y, x, end_y, 300))
        return {"success": rc == 0, "action": "scroll_down"}

    async def scroll_up(self, x: int = 500, y: int = 500,
                        normalized: bool = True) -> Dict:
        if normalized:
            x, y = self.denormalize_coords(x, y)
        scroll_dist = int(self.screen_height * 0.4)
        end_y = min(self.screen_height, y + scroll_dist)
        print(f"[ADB SCROLL UP] at ({x},{y}) -> ({x},{end_y}) dist={scroll_dist}")
        rc, output = await self._shell(_sh("input", "swipe", x, y, x, end_y, 300))
        return {"success": rc == 0, "action": "scroll_up"}
