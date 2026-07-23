"""
Sovereign Browser REST API endpoints
"""
import time
from typing import Optional, Dict, Any
from pydantic import BaseModel
from fastapi import Body, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from starlette.requests import Request
from starlette.websockets import WebSocketState

from Orchestrator.checkpoint import app
from Orchestrator.models import TaskType, TaskStatus
from Orchestrator.tasks import create_task
from Orchestrator.browser.interaction import click, type_text, press_key, scroll


class BrowserRunIn(BaseModel):
    prompt: str
    url: Optional[str] = None
    operator: Optional[str] = "system"
    system_prompt: Optional[str] = None
    device_id: Optional[str] = "blackbox"
    native_mode: Optional[bool] = False


@app.post("/browser/run")
def browser_run(req: BrowserRunIn):
    """Start a Sovereign Browser task. Returns task_id for polling."""
    from Orchestrator.browser.config import is_domain_allowed

    # Validate URL if provided
    if req.url and not is_domain_allowed(req.url):
        return {"error": f"Domain blocked by security policy: {req.url}"}, 403

    task = create_task(
        TaskType.USE_COMPUTER,
        operator=req.operator or "system",
        prompt=req.prompt,
        result_data={
            "url": req.url,
            "system_prompt": req.system_prompt,
            "device_id": req.device_id or "blackbox",
            "native_mode": bool(req.native_mode),
        }
    )

    print(f"[BROWSER] Task {task.task_id} created: {req.prompt[:100]}")
    return {
        "task_id": task.task_id,
        "status": "pending",
        "message": "Sovereign Browser task queued. Poll /tasks/{task_id} for progress."
    }


@app.get("/browser/status")
def browser_status():
    """Check Sovereign Browser system status (display, Chrome)."""
    try:
        from Orchestrator.browser.config import (
            NATIVE_MODE, ACTIVE_DISPLAY, DISPLAY_WIDTH, DISPLAY_HEIGHT,
            NATIVE_WIDTH, NATIVE_HEIGHT
        )
        if NATIVE_MODE:
            return {
                "display_running": True,
                "display": f":{ACTIVE_DISPLAY}",
                "resolution": f"{NATIVE_WIDTH}x{NATIVE_HEIGHT}",
                "cu_resolution": f"{DISPLAY_WIDTH}x{DISPLAY_HEIGHT}",
                "native_mode": True,
            }
        # Virtual mode (M9): displays are per-session, allocated on demand. Report
        # live virtual-CU sessions instead of a single global display.
        from Orchestrator.browser.display import get_allocator, MAX_VIRTUAL_SESSIONS
        sessions = get_allocator().active_sessions()
        # D8 (2026-07-23 live-view design): interactive viewers read
        # cu_resolution for click scaling/aspect and silently kept a stale
        # 1280x720 default when the field was absent — wrong for a 1440x900
        # Gemini session. Report the FIRST session's native WxH (matching the
        # viewer's open-sessions[0] behavior), falling back to the global CU
        # default when idle. Additive-only shape change.
        if sessions:
            cu_resolution = f"{sessions[0]['width']}x{sessions[0]['height']}"
        else:
            cu_resolution = f"{DISPLAY_WIDTH}x{DISPLAY_HEIGHT}"
        return {
            "display_running": bool(sessions),
            "native_mode": False,
            "virtual_sessions": len(sessions),
            "cap": MAX_VIRTUAL_SESSIONS,
            "sessions": sessions,
            "cu_resolution": cu_resolution,
        }
    except Exception as e:
        return {"display_running": False, "error": str(e)}


@app.get("/browser/screenshot")
def browser_screenshot():
    """Capture a screenshot from the display right now."""
    try:
        from Orchestrator.browser.screenshot import capture_screenshot, save_screenshot_to_uploads
        import time

        # Live full-desktop capture (native display). Per-session virtual displays
        # are captured via their own session handles; this endpoint is the native
        # snapshot used by the interactive viewer.
        png_bytes = capture_screenshot()
        task_id = f"live_{int(time.time())}"
        url = save_screenshot_to_uploads(png_bytes, task_id, 0)

        return {"screenshot_url": url, "size_bytes": len(png_bytes)}
    except Exception as e:
        return {"error": str(e)}


# ── Interactive viewer endpoints ──────────────────────────────────────────


@app.post("/browser/click")
async def browser_click(body: dict = Body(...)):
    x = int(body.get("x", 0))
    y = int(body.get("y", 0))
    button = body.get("button", "left")
    device_id = body.get("device_id", "blackbox")
    if device_id != "blackbox":
        from Orchestrator.browser.actions import execute_remote_action
        result = await execute_remote_action(device_id, "left_click" if button == "left" else "right_click", coordinate=[x, y])
    else:
        result = click(x, y, button)
    return result


@app.post("/browser/type")
async def browser_type(body: dict = Body(...)):
    text = body.get("text", "")
    device_id = body.get("device_id", "blackbox")
    if device_id != "blackbox":
        from Orchestrator.browser.actions import execute_remote_action
        result = await execute_remote_action(device_id, "type", text=text)
    else:
        result = type_text(text)
    return result


@app.post("/browser/key")
async def browser_key(body: dict = Body(...)):
    key = body.get("key", "")
    device_id = body.get("device_id", "blackbox")
    if device_id != "blackbox":
        from Orchestrator.browser.actions import execute_remote_action
        result = await execute_remote_action(device_id, "key", text=key)
    else:
        result = press_key(key)
    return result


@app.post("/browser/scroll")
async def browser_scroll(body: dict = Body(...)):
    x = int(body.get("x", 640))
    y = int(body.get("y", 360))
    direction = body.get("direction", "down")
    clicks = int(body.get("clicks", 3))
    device_id = body.get("device_id", "blackbox")
    if device_id != "blackbox":
        from Orchestrator.browser.actions import execute_remote_action
        result = await execute_remote_action(device_id, "scroll", coordinate=[x, y], direction=direction, amount=clicks)
    else:
        result = scroll(x, y, direction, clicks)
    return result


@app.get("/browser/screenshot/live")
async def browser_screenshot_live(request: Request = None):
    """Fast screenshot endpoint for the interactive viewer.
    Returns JPEG for bandwidth efficiency (~100KB vs ~800KB PNG).
    Supports ?device_id= query param for remote devices.
    """
    try:
        from Orchestrator.browser.screenshot import capture_screenshot, capture_remote_screenshot
        from Orchestrator.config import UPLOADS_DIR
        from PIL import Image
        import io

        device_id = request.query_params.get("device_id", "blackbox") if request else "blackbox"
        if device_id != "blackbox":
            png_bytes = await capture_remote_screenshot(device_id)
        else:
            png_bytes = capture_screenshot()
        ts = int(time.time() * 1000)

        # Convert to JPEG for much smaller file size (100-150KB vs 800KB PNG)
        img = Image.open(io.BytesIO(png_bytes))
        if img.mode == 'RGBA':
            img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=70, optimize=True)
        jpg_bytes = buf.getvalue()

        filename = f"browser_live_{ts}.jpg"
        save_path = UPLOADS_DIR / filename
        save_path.write_bytes(jpg_bytes)
        url = f"/ui/uploads/{filename}"

        # Clean up old live screenshots (keep last 5)
        for pattern in ["browser_live_*.jpg", "browser_live_*.png"]:
            live_files = sorted(UPLOADS_DIR.glob(pattern), key=lambda p: p.stat().st_mtime)
            for old_file in live_files[:-5]:
                try:
                    old_file.unlink()
                except OSError:
                    pass

        return {"url": url, "timestamp": ts}
    except Exception as e:
        return {"error": str(e), "success": False}


@app.get("/cu/preflight")
def cu_preflight(skip_screenshot: bool = False):
    """Machine-readiness report for Computer Use. Frontends render fails
    as banners with the remediation text."""
    from Orchestrator.browser import preflight
    return preflight.run_preflight(skip_screenshot=skip_screenshot)


@app.get("/cu/sessions")
def cu_sessions():
    """Live virtual-CU sessions — powers the Portal/Android "N agents running —
    watch" badge (D14: a badge, not a lock; concurrent sessions are allowed up to
    the cap). Native-mode exclusivity is enforced separately by display_arbiter."""
    from Orchestrator.browser.display import get_allocator, MAX_VIRTUAL_SESSIONS
    sessions = get_allocator().active_sessions()
    return {"active": bool(sessions), "count": len(sessions),
            "cap": MAX_VIRTUAL_SESSIONS, "sessions": sessions}


# Fallback-only inline viewer (pre-M2 minimal client). The REAL client is the
# served asset set Portal/cu-view/{index.html,cu-view.js,cu-view.css} — the
# Splashtop-style touchpad/zoom/extra-keys UX (design 2026-07-23 §4, M2).
# This string survives solely so /cu/view/{sid} never 500s if the asset tree
# is missing on a box (fresh-box gate: degraded, never dead).
_CU_VIEW_FALLBACK_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>CU Live View — {session_id}</title>
<style>html,body{{margin:0;height:100%;background:#0b0b0d;overflow:hidden}}
#screen{{width:100vw;height:100vh}}</style></head>
<body><div id="screen"></div>
<script type="module">
import RFB from '/cu/novnc/core/rfb.js';
const proto = location.protocol === 'https:' ? 'wss' : 'ws';
const url = `${{proto}}://${{location.host}}/cu/view/{session_id}/ws`;
const rfb = new RFB(document.getElementById('screen'), url, {{}});
// D1 (2026-07-23 design, closes local-model-stack Q5): interactive by
// default — pointer/keys flow over RFB into this session's OWN Xvfb (D2),
// sidestepping the global-display /browser/* input path entirely.
// ?viewonly=1 opts back into watch-only. Perimeter unchanged (§6): the
// tailnet could already drive via the open /browser/click|type|key routes.
rfb.viewOnly = new URLSearchParams(location.search).get('viewonly') === '1';
rfb.scaleViewport = true;     // fit 1280x720 / 1440x900 into the panel
rfb.resizeSession = false;    // never resize the agent's screen (D6)
</script></body></html>"""

_CU_VIEW_UNAVAILABLE = ("<!doctype html><meta charset=utf-8><body "
    "style='font-family:system-ui;background:#0b0b0d;color:#ddd;padding:2rem'>"
    "<h3>Live view unavailable</h3><p>noVNC / websockify are not installed on "
    "this box (SHOULD_HAVE in system-packages.txt). The CU session is still "
    "running — install <code>novnc</code> + <code>websockify</code> to watch.</p>")


@app.get("/cu/view/{session_id}", response_class=HTMLResponse)
def cu_view(session_id: str):
    """Serve the interactive CU live-view client (Portal/cu-view/, design
    2026-07-23 §4/M2). Route contract unchanged: 404 for unknown sessions,
    install-hint page when live_view is off, HTML otherwise. The page reads
    its session id from its own URL path and sizes itself from /cu/sessions —
    no server-side templating. Read fresh per request (prod runs live from
    the working tree); assets ship via the /ui static mount."""
    from Orchestrator.browser.display import get_allocator
    h = get_allocator().get(session_id)
    if h is None:
        return HTMLResponse("<!doctype html><body>No active CU session for that id.",
                            status_code=404)
    if not h.live_view:
        return HTMLResponse(_CU_VIEW_UNAVAILABLE)
    try:
        from Orchestrator.utils.paths import resolve as _resolve_path
        html = (_resolve_path("Portal", "cu-view", "index.html")
                .read_text(encoding="utf-8"))
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})
    except OSError as e:
        print(f"[CU-VIEW] cu-view asset missing, serving inline fallback: {e}")
        return HTMLResponse(_CU_VIEW_FALLBACK_HTML.format(session_id=session_id))


@app.websocket("/cu/view/{session_id}/ws")
async def cu_view_ws(websocket: WebSocket, session_id: str):
    """Reverse-proxy the viewer's WebSocket to this session's loopback websockify.
    Loopback-only target; the Tailscale perimeter is the auth boundary (§9)."""
    import asyncio
    import websockets
    from websockets.exceptions import ConnectionClosed
    from Orchestrator.browser.display import get_allocator

    h = get_allocator().get(session_id)
    # Plain accept() — mirror the proven app_proxy_websocket pattern. noVNC 1.x
    # and websockify both default to binary frames without requiring the
    # Sec-WebSocket-Protocol header, and a transparent proxy does not forward it
    # (forcing subprotocol="binary" when the client offered none breaks the
    # handshake).
    await websocket.accept()
    if h is None or not h.live_view:
        await websocket.close(code=1008, reason="No live view for session")
        return
    target = f"ws://127.0.0.1:{h.ws_port}/"
    try:
        upstream = await websockets.connect(target, max_size=None, open_timeout=10)
    except Exception as e:
        print(f"[CU-VIEW] upstream connect failed ({target}): {e}")
        await websocket.close(code=1011, reason="Upstream unavailable")
        return

    async def c2u():
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                if msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
                elif msg.get("text") is not None:
                    await upstream.send(msg["text"])
        except (WebSocketDisconnect, ConnectionClosed):
            pass

    async def u2c():
        try:
            async for frame in upstream:
                if isinstance(frame, bytes):
                    await websocket.send_bytes(frame)
                else:
                    await websocket.send_text(frame)
        except (ConnectionClosed, WebSocketDisconnect, RuntimeError):
            pass

    t1 = asyncio.create_task(c2u())
    t2 = asyncio.create_task(u2c())
    try:
        _done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        # Await the cancelled tasks so they don't emit "Task was destroyed but it
        # is pending" warnings — mirror the app_proxy_websocket reference.
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await upstream.close()
        # Prefer upstream's close code so e.g. a websockify-side close flows to the
        # viewer, matching the reference proxy's close-code propagation.
        code = upstream.close_code if upstream.close_code is not None else 1000
        reason = upstream.close_reason or ""
        try:
            if websocket.application_state == WebSocketState.CONNECTED:
                await websocket.close(code=code, reason=reason)
        except Exception:
            pass
