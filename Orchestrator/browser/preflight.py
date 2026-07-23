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
# Reuse actions.py's ACTUAL runtime gating — YDOTOOL_BIN is hardcoded to the
# installer-built /usr/local/bin/ydotool (apt's 0.1.8 lacks --absolute mousemove),
# and _ydotool_available does the same S_ISSOCK+W_OK socket validation the
# executor uses. Names imported into this namespace so tests can monkeypatch
# preflight._ydotool_available.
from Orchestrator.browser.actions import (
    _is_wayland_session, _ydotool_available, YDOTOOL_BIN, YDOTOOL_SOCKET,
)


def _is_wayland() -> bool:
    return _is_wayland_session()


def _check(id_, status, detail, remediation=""):
    return {"id": id_, "status": status, "detail": detail, "remediation": remediation}


def check_display() -> dict:
    env = get_native_env()
    if not NATIVE_MODE:
        return _check("display", "ok", "Sandbox mode (Xvfb) — display managed internally")
    missing = [k for k in ("XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS") if k not in env]
    if missing:
        return _check("display", "warn",
                      f"Display :{ACTIVE_DISPLAY}; session env missing {missing}",
                      "Log into the desktop session — the service picks it up automatically")
    return _check("display", "ok",
                  f"Display :{ACTIVE_DISPLAY} ({'Wayland' if _is_wayland() else 'X11'})")


def check_input_backend() -> dict:
    if _is_wayland():
        if _ydotool_available():
            return _check("input", "ok",
                          f"Wayland + ydotool daemon alive ({YDOTOOL_BIN})")
        if not os.path.isfile(YDOTOOL_BIN):
            return _check("input", "fail",
                          f"ydotool not installed at {YDOTOOL_BIN} (apt's ydotool is "
                          "too old — the BlackBox installer builds v1.0.4 from source)",
                          "Re-run the BlackBox installer ydotool step (Scripts/install.sh)")
        return _check("input", "fail",
                      f"ydotool present but daemon socket {YDOTOOL_SOCKET} not usable",
                      "Start the daemon: systemctl enable --now ydotoold")
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


def check_resolution(force: bool = True) -> dict:
    w, h = detect_native_resolution(force=force)
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
    if os.path.isfile(CHROME_PATH):
        return _check("chrome", "ok", f"Chrome at {CHROME_PATH}")
    which_path = shutil.which("google-chrome")
    if which_path:
        return _check("chrome", "ok", f"Chrome at {which_path}")
    return _check("chrome", "warn", f"Chrome not found at {CHROME_PATH}",
                  "Only needed for sandbox mode; set computer_use.chrome_path in config.ini")


def check_remote_tools() -> dict:
    missing = [b for b in ("vncdotool", "adb") if not shutil.which(b)]
    if missing:
        return _check("remote", "warn", f"Remote-device tools missing: {', '.join(missing)}",
                      "Optional — only needed for VNC/Android targets")
    return _check("remote", "ok", "vncdotool + adb present")


def check_virtual_display() -> dict:
    if not shutil.which("Xvfb"):
        return _check("virtual_display", "fail",
                      "Xvfb not installed — per-session CU virtual displays unavailable",
                      "Install xvfb (MUST_HAVE in Scripts/onboarding/system-packages.txt); "
                      "re-run Scripts/install.sh")
    return _check("virtual_display", "ok", "Xvfb present (per-session CU displays)")


def check_live_view() -> dict:
    have_ws = bool(shutil.which("websockify"))
    # noVNC assets: pinned vendored copy first (Portal/vendor/novnc — D3,
    # 2026-07-23 live-view design), apt path as fallback — must agree with
    # display._live_view_available or preflight contradicts the live panel.
    from Orchestrator.utils.paths import resolve as _resolve_path
    have_novnc = (os.path.isdir(str(_resolve_path("Portal", "vendor", "novnc")))
                  or os.path.isdir("/usr/share/novnc"))
    if have_ws and have_novnc:
        return _check("live_view", "ok", "websockify + noVNC present (live view enabled)")
    missing = [n for n, ok in (("websockify", have_ws), ("novnc", have_novnc)) if not ok]
    return _check("live_view", "warn",
                  f"Live view degraded — missing {', '.join(missing)}",
                  "Install websockify/novnc (SHOULD_HAVE in system-packages.txt) to watch "
                  "CU sessions in the Portal/Android live-view panel")


_RANK = {"ok": 0, "warn": 1, "fail": 2}


def run_preflight(skip_screenshot: bool = False) -> dict:
    # Lambdas resolve the check functions from module globals at CALL time, so
    # tests can monkeypatch preflight.check_* and a raising check degrades to a
    # fail entry instead of 500ing the whole report.
    plan = [
        ("display", lambda: check_display()),
        ("input", lambda: check_input_backend()),
    ]
    if not skip_screenshot:
        plan.append(("screenshot", lambda: check_screenshot()))
    plan += [
        # force=False on the cheap path — avoids a second expensive Portal capture
        ("resolution", lambda: check_resolution(force=not skip_screenshot)),
        ("api_keys", lambda: check_api_keys()),
        ("chrome", lambda: check_chrome()),
        ("virtual_display", lambda: check_virtual_display()),
        ("live_view", lambda: check_live_view()),
        ("remote", lambda: check_remote_tools()),
    ]
    checks = []
    for check_id, fn in plan:
        try:
            checks.append(fn())
        except Exception as e:
            checks.append(_check(check_id, "fail", f"check crashed: {e}",
                                 "Report this to support"))
    worst = max(checks, key=lambda c: _RANK[c["status"]])["status"]
    return {"status": worst, "checks": checks}
