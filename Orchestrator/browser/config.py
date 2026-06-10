"""
Sovereign Browser configuration constants
"""
import io
import os
import subprocess
import time as _time
from pathlib import Path

from Orchestrator.config import (
    ANTHROPIC_API_KEY, CU_NATIVE_MODE, CU_CHROME_PATH,
    CU_MODEL_DEFAULT, CU_MAX_ITERATIONS, CU_SESSION_TIMEOUT,
)

# ── Native Desktop Mode ──
# When True, CU operates on the real Linux desktop
# instead of a sandboxed Xvfb virtual display.
NATIVE_MODE = CU_NATIVE_MODE


def _detect_native_display() -> int:
    """Auto-detect the active X11 display number from the running GNOME session.
    Checks gnome-shell, then Xorg process, then falls back to DISPLAY env var.
    """
    # Method 1: Find gnome-shell's DISPLAY from /proc
    try:
        result = subprocess.run(
            ["pgrep", "-x", "gnome-shell"], capture_output=True, text=True, timeout=5
        )
        for pid in result.stdout.strip().split():
            env_file = f"/proc/{pid}/environ"
            if os.path.isfile(env_file):
                try:
                    with open(env_file, "rb") as f:
                        env_data = f.read().decode("utf-8", errors="replace")
                    for entry in env_data.split("\0"):
                        if entry.startswith("DISPLAY=:"):
                            return int(entry.split(":")[1].split(".")[0])
                except (PermissionError, ValueError):
                    continue
    except Exception:
        pass
    # Method 2: Parse Xorg command line for -displayfd or display arg
    try:
        result = subprocess.run(
            ["pgrep", "-a", "Xorg"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            for part in parts:
                if part.startswith(":") and part[1:].isdigit():
                    return int(part[1:])
    except Exception:
        pass
    # Method 3: Environment variable
    display = os.environ.get("DISPLAY", "")
    if display.startswith(":"):
        try:
            return int(display.split(":")[1].split(".")[0])
        except ValueError:
            pass
    return 0  # Last resort fallback


# Real desktop display — auto-detected from running session
NATIVE_DISPLAY_NUMBER = _detect_native_display()


# CU model resolution — what the model sees and clicks on.
# Fixed at 1280x720 (Anthropic's optimal range for computer_20251124).
# Scale factors are computed from actual desktop → CU resolution.
CU_DISPLAY_WIDTH = 1280
CU_DISPLAY_HEIGHT = 720

# ── Virtual display settings (used when NATIVE_MODE = False) ──
DISPLAY_NUMBER = 99
DISPLAY_WIDTH = CU_DISPLAY_WIDTH if NATIVE_MODE else 1280
DISPLAY_HEIGHT = CU_DISPLAY_HEIGHT if NATIVE_MODE else 720
DISPLAY_DEPTH = 24

# The active display number used throughout the system
ACTIVE_DISPLAY = NATIVE_DISPLAY_NUMBER if NATIVE_MODE else DISPLAY_NUMBER


# ── Dynamic native resolution detection (E18) ──
# Previously NATIVE_WIDTH/NATIVE_HEIGHT were detected ONCE at module-import
# time via xrandr, which (1) returns nothing on Wayland because xrandr is X11-
# only, and (2) bakes in the assumption that the customer's display is 1920x1080
# if detection failed. Brandon's 2026-05-17 question: "are we locked at that
# screen resolution, or does it dynamically fix itself based on how big the
# screen is?" — answer was "locked, with a 1080p fallback".
#
# New approach: detect on demand, with a 60s cache. Try xrandr first (cheap),
# fall back to taking a Portal screenshot and reading its PNG dimensions (works
# on Wayland). Customers running 4K, ultrawide, or anything non-1080p now get
# correct click coordinate translation without code changes.

_RES_CACHE = {"w": None, "h": None, "ts": 0.0}
_RES_CACHE_TTL_S = 60.0


def _xrandr_resolution():
    """Try xrandr --current. Returns (w, h) or None."""
    try:
        env = {"DISPLAY": f":{NATIVE_DISPLAY_NUMBER}", "PATH": "/usr/bin:/bin"}
        xa = os.environ.get("XAUTHORITY", "")
        if xa:
            env["XAUTHORITY"] = xa
        result = subprocess.run(
            ["xrandr", "--current"],
            capture_output=True, text=True, timeout=5, env=env
        )
        for line in result.stdout.splitlines():
            if "current" in line.lower():
                # "Screen 0: minimum 16 x 16, current 3440 x 1440, ..."
                parts = line.split("current")[1].split(",")[0].strip()
                w, h = parts.split(" x ")
                return int(w.strip()), int(h.strip())
    except Exception:
        pass
    return None


def _portal_screenshot_resolution():
    """Fallback: take a Portal screenshot and read PNG dimensions. Works on Wayland."""
    try:
        # Imported lazily to avoid circular import at module load
        from Orchestrator.browser.screenshot import capture_screenshot_portal
        from PIL import Image
        png_bytes = capture_screenshot_portal()
        with Image.open(io.BytesIO(png_bytes)) as img:
            return img.size  # (w, h)
    except Exception:
        pass
    return None


def detect_native_resolution(force: bool = False):
    """Return the real display (width, height), cached for 60s.

    Tries xrandr first (cheap, X11-only). If xrandr returns nothing (Wayland)
    falls back to a Portal screenshot whose PNG header gives us the dimensions.
    Final fallback: 1920x1080.

    Args:
        force: bypass the cache and re-detect now.
    """
    now = _time.time()
    if not force and _RES_CACHE["w"] and (now - _RES_CACHE["ts"]) < _RES_CACHE_TTL_S:
        return _RES_CACHE["w"], _RES_CACHE["h"]

    result = _xrandr_resolution() or _portal_screenshot_resolution() or (1920, 1080)
    _RES_CACHE["w"], _RES_CACHE["h"], _RES_CACHE["ts"] = result[0], result[1], now
    return result


def get_scale_factors():
    """Return current (SCALE_X, SCALE_Y) computed from the live native resolution.

    Always use this in click-translation code paths — the module-level
    SCALE_X/SCALE_Y are stale snapshots from import time.
    """
    if not NATIVE_MODE:
        return 1.0, 1.0
    w, h = detect_native_resolution()
    return w / CU_DISPLAY_WIDTH, h / CU_DISPLAY_HEIGHT


# Module-level constants — kept for backward compatibility. Initial values are
# detected at import via xrandr only (Portal fallback skipped because
# screenshot.py imports this module — circular). Callers that need fresh values
# (especially click translation) should use get_scale_factors() instead.
_initial = _xrandr_resolution() or (1920, 1080)
NATIVE_WIDTH, NATIVE_HEIGHT = _initial
_RES_CACHE["w"], _RES_CACHE["h"], _RES_CACHE["ts"] = NATIVE_WIDTH, NATIVE_HEIGHT, _time.time()
SCALE_X = NATIVE_WIDTH / CU_DISPLAY_WIDTH if NATIVE_MODE else 1.0
SCALE_Y = NATIVE_HEIGHT / CU_DISPLAY_HEIGHT if NATIVE_MODE else 1.0

# X11 auth — needed for systemd services to access the real display
def _detect_xauthority():
    """Find the XAUTHORITY file for the native display.
    On Wayland+XWayland (GNOME/Mutter), it's /run/user/<uid>/.mutter-Xwaylandauth.*
    On plain X11, it's ~/.Xauthority
    """
    # Check if already set in environment
    xa = os.environ.get("XAUTHORITY", "")
    if xa and os.path.isfile(xa):
        return xa
    # XWayland (Mutter)
    import glob
    uid = os.getuid()
    for pattern in [f"/run/user/{uid}/.mutter-Xwaylandauth.*",
                    f"/run/user/{uid}/.Xauthority"]:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    # Classic X11
    home = os.path.expanduser("~")
    classic = os.path.join(home, ".Xauthority")
    if os.path.isfile(classic):
        return classic
    return ""

def _detect_dbus_session():
    """Find the D-Bus session bus address for the user session.
    Needed for XDG Portal access from systemd services.
    """
    addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if addr:
        return addr
    uid = os.getuid()
    bus_path = f"/run/user/{uid}/bus"
    if os.path.exists(bus_path):
        return f"unix:path={bus_path}"
    return ""


def get_native_env() -> dict:
    """Get environment dict suitable for X11/Wayland commands on the native display.

    E17 (Brandon 2026-05-17): XAUTHORITY + DBUS detection used to be cached as
    module-level constants computed at import time. But blackbox.service starts
    at boot BEFORE the user logs into GNOME — at that moment no Xauthority file
    exists yet and the user DBUS session isn't running. The constants get
    captured as empty strings and stay empty forever. Compute FRESH on each
    call so the service picks up the session whenever the user logs in.
    Also add XDG_RUNTIME_DIR which the Portal subprocess relies on.
    """
    env = {"DISPLAY": f":{ACTIVE_DISPLAY}", "PATH": "/usr/bin:/usr/local/bin:/bin"}
    if NATIVE_MODE:
        xauth = _detect_xauthority()
        if xauth:
            env["XAUTHORITY"] = xauth
        dbus_addr = _detect_dbus_session()
        if dbus_addr:
            env["DBUS_SESSION_BUS_ADDRESS"] = dbus_addr
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    return env


# Legacy aliases kept for backward compatibility; prefer get_native_env() which
# computes fresh on each call.
XAUTHORITY = _detect_xauthority() if NATIVE_MODE else ""
DBUS_SESSION_BUS_ADDRESS = _detect_dbus_session() if NATIVE_MODE else ""

# Chrome settings
CHROME_PATH = CU_CHROME_PATH
BROWSER_PKG_DIR = Path(__file__).parent
PROFILE_BASE = BROWSER_PKG_DIR / "profiles"
CDP_PORT = 9222  # Chrome DevTools Protocol debug port

# Agent loop limits
MAX_ITERATIONS = CU_MAX_ITERATIONS
SESSION_TIMEOUT = CU_SESSION_TIMEOUT  # seconds

# Anthropic Computer Use API
ANTHROPIC_BETA_HEADER = "computer-use-2025-11-24"
COMPUTER_TOOL_TYPE = "computer_20251124"
CU_MODEL = CU_MODEL_DEFAULT
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

# Domain security
# Always block dangerous domains. In native mode, allow localhost but block cloud metadata.
_COMMON_BLOCKLIST = [
    "169.254.",           # Link-local / cloud metadata
    "metadata.google.internal",
    "metadata.google",
    "metadata.aws",       # AWS IMDS
]

_PRIVATE_NETWORK_BLOCKLIST = [
    "10.",                # Private class A
    "192.168.",           # Private class C
    "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.",
    "172.24.", "172.25.", "172.26.", "172.27.",
    "172.28.", "172.29.", "172.30.", "172.31.",
]

if NATIVE_MODE:
    # Native mode: allow localhost (agent needs it) but block cloud metadata
    DOMAIN_BLOCKLIST = _COMMON_BLOCKLIST
else:
    # Headless mode: block all private networks and localhost
    DOMAIN_BLOCKLIST = _COMMON_BLOCKLIST + _PRIVATE_NETWORK_BLOCKLIST + [
        "localhost", "127.0.0.1", "0.0.0.0", "::1",
    ]

# None = allow all external domains. Set to list to restrict.
DOMAIN_ALLOWLIST = None


def is_domain_allowed(url: str) -> bool:
    """Check if a URL's domain is allowed for navigation (SSRF prevention)."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        # Block internal/private addresses
        for blocked in DOMAIN_BLOCKLIST:
            if host == blocked or host.startswith(blocked):
                return False
        # If allowlist is set, only allow listed domains
        if DOMAIN_ALLOWLIST is not None:
            return any(host.endswith(d) for d in DOMAIN_ALLOWLIST)
        return True
    except Exception:
        return False
