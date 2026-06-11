"""CU preflight — is THIS machine ready to run Computer Use?

Each check returns {"id", "status": ok|warn|fail, "detail", "remediation"}.
Aggregate status = worst individual status. Secondary tooling (vnc/adb) can
only warn, never fail — remote devices are optional.
"""
import os
import shutil

from Orchestrator.config import ANTHROPIC_API_KEY, GOOGLE_API_KEY, OPENAI_API_KEY
from Orchestrator.browser.config import (
    NATIVE_MODE, ACTIVE_DISPLAY, CHROME_PATH,
    detect_native_resolution, get_scale_factors, get_native_env,
)
from Orchestrator.browser.actions import _is_wayland_session


def _is_wayland() -> bool:
    return _is_wayland_session()


def _ydotool_socket_alive() -> bool:
    sock = os.environ.get("YDOTOOL_SOCKET", "/run/user/%d/.ydotool_socket" % os.getuid())
    return os.path.exists(sock)


def _check(id_, status, detail, remediation=""):
    return {"id": id_, "status": status, "detail": detail, "remediation": remediation}


def check_display() -> dict:
    env = get_native_env()
    if not NATIVE_MODE:
        return _check("display", "ok", "Sandbox mode (Xvfb) — display managed internally")
    missing = [k for k in ("XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS") if k not in env]
    if missing and _is_wayland():
        return _check("display", "warn",
                      f"Display :{ACTIVE_DISPLAY}; session env missing {missing}",
                      "Log into the desktop session — the service picks it up automatically")
    return _check("display", "ok",
                  f"Display :{ACTIVE_DISPLAY} ({'Wayland' if _is_wayland() else 'X11'})")


def check_input_backend() -> dict:
    if _is_wayland():
        if not shutil.which("ydotool"):
            return _check("input", "fail", "Wayland session, ydotool not installed",
                          "Install ydotool: sudo apt install ydotool")
        if not _ydotool_socket_alive():
            return _check("input", "fail", "ydotool installed but daemon not running",
                          "Start the daemon: systemctl enable --now ydotool")
        return _check("input", "ok", "Wayland + ydotool daemon alive")
    if not shutil.which("xdotool"):
        return _check("input", "fail", "X11 session, xdotool not installed",
                      "Install xdotool: sudo apt install xdotool")
    return _check("input", "ok", "X11 + xdotool")


def check_screenshot() -> dict:
    from Orchestrator.browser.screenshot import capture_screenshot
    try:
        png = capture_screenshot()
        if len(png) < 1000:
            return _check("screenshot", "fail", f"Capture returned {len(png)} bytes",
                          "Check XDG Desktop Portal / install scrot: sudo apt install scrot")
        return _check("screenshot", "ok", f"Captured {len(png)} bytes")
    except Exception as e:
        return _check("screenshot", "fail", f"Capture failed: {e}",
                      "Install scrot (sudo apt install scrot) and verify the desktop session is active")


def check_resolution() -> dict:
    w, h = detect_native_resolution(force=True)
    sx, sy = get_scale_factors()
    return _check("resolution", "ok", f"{w}x{h} native; scale {sx:.2f}x{sy:.2f}")


def check_api_keys() -> dict:
    have = {"anthropic": bool(ANTHROPIC_API_KEY), "google": bool(GOOGLE_API_KEY),
            "openai": bool(OPENAI_API_KEY)}
    missing = [k for k, v in have.items() if not v]
    if not any(have.values()):
        return _check("api_keys", "fail", "No CU backend API keys configured",
                      "Set ANTHROPIC_API_KEY / GOOGLE_API_KEY / OPENAI_API_KEY in .env")
    if missing:
        return _check("api_keys", "warn", f"Missing keys: {', '.join(missing)}",
                      "Those backends will be hidden from the model selector")
    return _check("api_keys", "ok", "All three CU backend keys present")


def check_chrome() -> dict:
    if os.path.isfile(CHROME_PATH) or shutil.which("google-chrome"):
        return _check("chrome", "ok", f"Chrome at {CHROME_PATH}")
    return _check("chrome", "warn", f"Chrome not found at {CHROME_PATH}",
                  "Only needed for sandbox mode; set computer_use.chrome_path in config.ini")


def check_remote_tools() -> dict:
    missing = [b for b in ("vncdotool", "adb") if not shutil.which(b)]
    if missing:
        return _check("remote", "warn", f"Remote-device tools missing: {', '.join(missing)}",
                      "Optional — only needed for VNC/Android targets")
    return _check("remote", "ok", "vncdotool + adb present")


_RANK = {"ok": 0, "warn": 1, "fail": 2}


def run_preflight(skip_screenshot: bool = False) -> dict:
    checks = [check_display(), check_input_backend()]
    if not skip_screenshot:
        checks.append(check_screenshot())
    checks += [check_resolution(), check_api_keys(), check_chrome(), check_remote_tools()]
    worst = max(checks, key=lambda c: _RANK[c["status"]])["status"]
    return {"status": worst, "checks": checks}
