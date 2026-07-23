"""
Screenshot capture, encoding, and storage.

Native mode (Wayland):
  Primary: XDG Desktop Portal Screenshot (captures full composited desktop)
  Fallback: scrot via XWayland (may only see X11 windows)

Sandbox mode (Xvfb):
  Primary: scrot — captures the FULL virtual display
  Fallback: Chrome DevTools Protocol (CDP) — Chrome viewport only
"""
import subprocess
import base64
import io
import json
import os
import time
from pathlib import Path
from typing import Optional

from Orchestrator.browser.config import (
    DISPLAY_NUMBER, DISPLAY_WIDTH, DISPLAY_HEIGHT, CDP_PORT,
    NATIVE_MODE, ACTIVE_DISPLAY, CU_DISPLAY_WIDTH, CU_DISPLAY_HEIGHT,
    get_native_env
)


_PORTAL_SCREENSHOT_SCRIPT = '''
import dbus, dbus.mainloop.glib, sys, os
from gi.repository import GLib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
bus = dbus.bus.BusConnection(os.environ.get("DBUS_SESSION_BUS_ADDRESS",
    "unix:path=/run/user/%d/bus" % os.getuid()))
loop = GLib.MainLoop()
result = {}

def on_response(code, data):
    result["code"] = code
    result["uri"] = str(data.get("uri", "")) if code == 0 else ""
    loop.quit()

portal = bus.get_object("org.freedesktop.portal.Desktop",
                       "/org/freedesktop/portal/desktop")
iface = dbus.Interface(portal, "org.freedesktop.portal.Screenshot")
req = iface.Screenshot("", {"interactive": dbus.Boolean(False, variant_level=1)})
bus.get_object("org.freedesktop.portal.Desktop", req).connect_to_signal(
    "Response", on_response, dbus_interface="org.freedesktop.portal.Request")
GLib.timeout_add(8000, loop.quit)
loop.run()

if result.get("code") == 0 and result.get("uri"):
    print(result["uri"])
    sys.exit(0)
else:
    print("FAIL:" + str(result), file=sys.stderr)
    sys.exit(1)
'''


def capture_screenshot_portal() -> bytes:
    """Capture the full desktop via XDG Desktop Portal (Wayland-native).

    Uses org.freedesktop.portal.Screenshot with interactive=False.
    Runs via system Python3 (which has dbus-python) as a subprocess
    to avoid venv dependency issues.
    Returns PNG bytes.
    """
    env = os.environ.copy()
    env.update(get_native_env())

    result = subprocess.run(
        ["/usr/bin/python3", "-c", _PORTAL_SCREENSHOT_SCRIPT],
        capture_output=True, text=True, timeout=15, env=env
    )

    if result.returncode != 0:
        raise RuntimeError(f"Portal screenshot failed: {result.stderr.strip()}")

    uri = result.stdout.strip()
    if not uri or not uri.startswith("file://"):
        raise RuntimeError(f"Portal returned unexpected URI: {uri}")

    file_path = uri.replace("file://", "")
    png_bytes = Path(file_path).read_bytes()

    # Clean up the screenshot file Portal created
    try:
        Path(file_path).unlink()
    except OSError:
        pass

    if len(png_bytes) < 100:
        raise RuntimeError(f"Portal screenshot too small ({len(png_bytes)} bytes)")
    return png_bytes


def capture_screenshot_display(display_number: int = ACTIVE_DISPLAY,
                               native: Optional[bool] = None) -> bytes:
    """Capture the full display via scrot. Returns PNG bytes.

    This captures EVERYTHING on the display — Chrome, terminals, any GUI app.
    On Wayland, only X11/XWayland windows are captured (not the full desktop).

    ``native`` picks the environment: True -> the real desktop's env
    (get_native_env); False -> a clean env pinned to ``:display_number`` (a
    per-session Xvfb). None (legacy callers) falls back to the box-global
    NATIVE_MODE. Display-coherence fix (2026-07-23): this function previously
    ignored the passed display_number whenever NATIVE_MODE, so virtual-session
    agents grounded every decision on a screenshot of the operator's REAL
    desktop.
    """
    import tempfile
    use_native = NATIVE_MODE if native is None else bool(native)
    if use_native:
        env = get_native_env()
    else:
        env = {"DISPLAY": f":{display_number}", "PATH": "/usr/bin:/usr/local/bin:/bin"}

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            ["scrot", "--overwrite", tmp_path],
            env=env,
            capture_output=True,
            timeout=10
        )
        if result.returncode != 0:
            raise RuntimeError(f"scrot failed (exit {result.returncode}): {result.stderr.decode()[:200]}")
        png_bytes = Path(tmp_path).read_bytes()
        if len(png_bytes) < 100:
            raise RuntimeError(f"scrot produced tiny file ({len(png_bytes)} bytes)")
        return png_bytes
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def capture_screenshot_cdp(page_ws_url: str = "") -> bytes:
    """Capture screenshot via Chrome DevTools Protocol. Returns PNG bytes.

    Only captures Chrome's viewport — NOT the full display.
    Used as fallback when scrot is unavailable.
    """
    import websocket

    if not page_ws_url:
        page_ws_url = _get_page_ws_url()
    if not page_ws_url:
        raise RuntimeError("No CDP page WebSocket URL available")

    ws = websocket.create_connection(page_ws_url, timeout=10)
    try:
        msg = json.dumps({
            "id": 1,
            "method": "Page.captureScreenshot",
            "params": {"format": "png"}
        })
        ws.send(msg)

        while True:
            resp = json.loads(ws.recv())
            if resp.get("id") == 1:
                if "error" in resp:
                    raise RuntimeError(f"CDP error: {resp['error']}")
                b64_data = resp.get("result", {}).get("data", "")
                if not b64_data:
                    raise RuntimeError("CDP returned empty screenshot data")
                return base64.b64decode(b64_data)
    finally:
        ws.close()


def capture_screenshot(display_number: int = ACTIVE_DISPLAY) -> bytes:
    """Capture a screenshot of the display and resize to CU model resolution.

    Native mode: always try Portal first (works on Wayland AND on X11+GNOME),
    fall through to scrot (works on plain X11 + as Wayland-XWayland fallback).
    Sandbox mode (Xvfb): scrot → CDP fallback.

    E17 (Brandon 2026-05-17): previous version branched on XDG_SESSION_TYPE env
    var which is stripped from systemd-launched processes, so Wayland systems
    were taking the X11 branch + getting black scrot captures. New approach:
    just try Portal first universally. Portal failure (no portal, not a desktop
    session, etc.) falls through to scrot. Removes brittle session detection.

    Returns PNG bytes at the CU model resolution.
    """
    if NATIVE_MODE:
        # Always try Portal first — works on Wayland (full composited desktop)
        # AND on X11+GNOME (also full desktop). Single best path on modern Linux.
        try:
            png_bytes = capture_screenshot_portal()
            png_bytes = resize_screenshot(png_bytes, CU_DISPLAY_WIDTH, CU_DISPLAY_HEIGHT)
            return png_bytes
        except Exception as e:
            print(f"[SCREENSHOT] Portal failed, trying scrot: {e}")

        # Fall through to scrot — works on plain X11. On Wayland this captures
        # only XWayland windows (often black) but better than nothing.
        try:
            png_bytes = capture_screenshot_display(display_number)
            png_bytes = resize_screenshot(png_bytes, CU_DISPLAY_WIDTH, CU_DISPLAY_HEIGHT)
            return png_bytes
        except Exception as e:
            print(f"[SCREENSHOT] scrot also failed: {e}")

        raise RuntimeError("All native screenshot methods failed (Portal, scrot)")

    # Sandbox mode: scrot → CDP fallback
    try:
        return capture_screenshot_display(display_number)
    except Exception as e:
        print(f"[SCREENSHOT] scrot failed, trying CDP: {e}")

    try:
        return capture_screenshot_cdp()
    except Exception as e:
        print(f"[SCREENSHOT] CDP also failed: {e}")

    raise RuntimeError("All screenshot methods failed (scrot, CDP)")


def screenshot_to_base64(png_bytes: bytes) -> str:
    """Encode PNG bytes to base64 string for the Anthropic API."""
    return base64.standard_b64encode(png_bytes).decode("utf-8")


def resize_screenshot(png_bytes: bytes, target_w: int = DISPLAY_WIDTH,
                      target_h: int = DISPLAY_HEIGHT) -> bytes:
    """Resize screenshot if needed. No-op when display matches target size."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes))
        if img.size == (target_w, target_h):
            return png_bytes
        img = img.resize((target_w, target_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        return png_bytes


def save_screenshot_to_uploads(png_bytes: bytes, task_id: str, step: int) -> str:
    """Save screenshot to Portal/uploads/ and return the URL path."""
    uploads_dir = Path(__file__).parent.parent.parent / "Portal" / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    filename = f"browser_{task_id}_step{step:03d}.png"
    save_path = uploads_dir / filename
    save_path.write_bytes(png_bytes)

    url = f"/ui/uploads/{filename}"
    return url


def _get_page_ws_url() -> str:
    """Discover the WebSocket URL for the first Chrome page tab via CDP."""
    import urllib.request
    try:
        req = urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=2)
        pages = json.loads(req.read().decode())
        for page in pages:
            if page.get("type") == "page":
                return page.get("webSocketDebuggerUrl", "")
    except Exception:
        pass
    return ""


async def capture_remote_screenshot(device_id: str) -> bytes:
    """Capture screenshot from a remote desktop via VNC over Tailscale."""
    from Orchestrator.device_registry import get_registry
    from Orchestrator.remote_desktop import VNCClient
    from Orchestrator.browser.config import CU_DISPLAY_WIDTH, CU_DISPLAY_HEIGHT

    device = get_registry().get_device(device_id)
    if not device:
        raise RuntimeError(f"Device not found: {device_id}")

    client = VNCClient(
        device.tailscale_ip,
        device.vnc_port,
        device.metadata.get("vnc_password")
    )
    return await client.screenshot(CU_DISPLAY_WIDTH, CU_DISPLAY_HEIGHT)
