"""
Sovereign Browser REST API endpoints
"""
import time
from typing import Optional, Dict, Any
from pydantic import BaseModel
from fastapi import Body, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
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
    # CU model id — resolves the backend (anthropic/openai/google) exactly as
    # the use_computer tool does; empty = the Anthropic default. Added for the
    # per-backend click-accuracy harness (M0, 2026-07-23); additive.
    model: Optional[str] = None


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
            "model": req.model or "",
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
        result = click(x, y, button, session_id=body.get("session_id"))
    return result


@app.post("/browser/type")
async def browser_type(body: dict = Body(...)):
    text = body.get("text", "")
    device_id = body.get("device_id", "blackbox")
    if device_id != "blackbox":
        from Orchestrator.browser.actions import execute_remote_action
        result = await execute_remote_action(device_id, "type", text=text)
    else:
        result = type_text(text, session_id=body.get("session_id"))
    return result


@app.post("/browser/key")
async def browser_key(body: dict = Body(...)):
    key = body.get("key", "")
    device_id = body.get("device_id", "blackbox")
    if device_id != "blackbox":
        from Orchestrator.browser.actions import execute_remote_action
        result = await execute_remote_action(device_id, "key", text=key)
    else:
        result = press_key(key, session_id=body.get("session_id"))
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
        result = scroll(x, y, direction, clicks, session_id=body.get("session_id"))
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


class CuSessionOpenIn(BaseModel):
    operator: Optional[str] = "system"


@app.post("/cu/session/open")
async def cu_session_open(req: Optional[CuSessionOpenIn] = Body(default=None)):
    """Desktop-first CU (2026-07-23): ensure-or-create a live VIRTUAL session
    for the operator WITHOUT any agent loop — the Splashtop-style desktop is up
    before the first prompt, so the user can drive it and a later agent task
    takes over the SAME session.

    Reuse semantics are exactly the task-attach path's
    (session_manager.get_or_create_session): an alive, unexpired session for
    the operator is returned as-is; a subsequent /browser/run or use_computer
    task for this operator picks THIS session up. The session stays subject to
    the normal idle expiry (SESSION_TIMEOUT + the display TTL reaper) — a
    manually opened desktop is never immortal.
    """
    from Orchestrator.browser.session_manager import (
        destroy_session_by_id, get_operator_session, get_or_create_session,
    )
    operator = (req.operator if req and req.operator else "system")

    # Compute `reused` with the SAME predicate get_or_create_session applies,
    # BEFORE calling it (it reports nothing back about which branch it took).
    existing = get_operator_session(operator)
    reused = bool(existing is not None and existing.is_alive()
                  and not existing.is_expired())

    try:
        session = get_or_create_session(operator)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    session.native_mode = False  # manual desktop sessions are always virtual
    try:
        ok = await session.ensure_browser("about:blank")
    except Exception as e:
        ok = False
        print(f"[CU-SESSION] manual open: ensure_browser raised: {e}")
    if not ok:
        # Don't pin a half-created session on the operator; a reused session
        # that failed to (re)start its browser is equally unusable.
        destroy_session_by_id(session.session_id)
        return JSONResponse(
            {"error": "Failed to start the desktop session (virtual display cap "
                      "reached, or Xvfb/Chrome could not start)"},
            status_code=502)

    session.touch()
    live = bool(session.display is not None and session.display.live_view)
    return {
        "session_id": session.session_id,
        "view_url": f"/cu/view/{session.session_id}",
        "reused": reused,
        "live_view": live,
        "operator": operator,
    }


@app.post("/cu/session/{session_id}/close")
def cu_session_close(session_id: str):
    """Explicitly end a CU session (manual desktop or otherwise). Calls the
    existing session cleanup (Chrome + display quartet teardown); 404 for an
    unknown id."""
    from Orchestrator.browser.session_manager import destroy_session_by_id
    if not destroy_session_by_id(session_id):
        return JSONResponse({"error": f"Unknown CU session: {session_id}"},
                            status_code=404)
    return {"success": True, "session_id": session_id}


@app.get("/cu/sessions")
def cu_sessions():
    """Live virtual-CU sessions — powers the Portal/Android "N agents running —
    watch" badge (D14: a badge, not a lock; concurrent sessions are allowed up to
    the cap). Native-mode exclusivity is enforced separately by display_arbiter.

    ADDITIVE (main-desktop streaming, Brandon 2026-07-23): a "main" key reports
    the REAL desktop's availability {available, display, resolution} so clients
    render the session-vs-main switcher from this ONE payload."""
    from Orchestrator.browser import native_stream
    from Orchestrator.browser.display import get_allocator, MAX_VIRTUAL_SESSIONS
    sessions = get_allocator().active_sessions()
    try:
        main = native_stream.public_main_status()
    except Exception as e:  # the badge must never die on a probe hiccup
        main = {"available": False, "reason": f"probe failed: {e}"}
    return {"active": bool(sessions), "count": len(sessions),
            "cap": MAX_VIRTUAL_SESSIONS, "sessions": sessions, "main": main}


@app.get("/cu/main/status")
def cu_main_status():
    """Availability of the REAL desktop for main-desktop streaming: logged-in X
    session + resolvable xauth -> {available, display, resolution}; otherwise
    {available: false, reason} (mirrors the preflight display check)."""
    from Orchestrator.browser import native_stream
    return native_stream.public_main_status()


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


_CU_NOTHING_TO_SHOW = ("<!doctype html><meta charset=utf-8><body "
    "style='font-family:system-ui;background:#0b0b0d;color:#ddd;padding:2rem'>"
    "<h3>Nothing to show</h3><p>No live Computer Use session is running and the "
    "main desktop is not streamable: {reason}.</p>")


def _serve_cu_client_page(session_id: str) -> HTMLResponse:
    """The served Splashtop-style client (Portal/cu-view/). The page reads its
    session id — including the literal "main" — from its own URL path and
    derives its WS path from location, so ONE asset serves both surfaces. Read
    fresh per request (prod runs live from the working tree)."""
    try:
        from Orchestrator.utils.paths import resolve as _resolve_path
        html = (_resolve_path("Portal", "cu-view", "index.html")
                .read_text(encoding="utf-8"))
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})
    except OSError as e:
        print(f"[CU-VIEW] cu-view asset missing, serving inline fallback: {e}")
        return HTMLResponse(_CU_VIEW_FALLBACK_HTML.format(session_id=session_id))


def _cu_view_main() -> HTMLResponse:
    """GET /cu/view/main — the SAME client page, backed by the native-desktop
    stream instead of a session's Xvfb."""
    from Orchestrator.browser import native_stream
    from Orchestrator.browser.display import _live_view_available
    status = native_stream.probe_main_desktop()
    if not status.get("available"):
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><body "
            "style='font-family:system-ui;background:#0b0b0d;color:#ddd;"
            "padding:2rem'><h3>Main desktop unavailable</h3><p>"
            f"{status.get('reason', 'log into the desktop session')}.</p>",
            status_code=503)
    if not _live_view_available():
        return HTMLResponse(_CU_VIEW_UNAVAILABLE)
    return _serve_cu_client_page("main")


def _cu_view_auto(query: str = ""):
    """GET /cu/view/auto — 302 to the right surface: a live agent/manual
    session first, else the main desktop when available, else a friendly
    nothing-to-show page naming the reason. Policy lives in the PURE
    native_stream.resolve_auto_view; this route only gathers inputs.
    The caller's query string is PRESERVED across the redirect — the Android
    app enters via auto carrying ?ti=&bi= host-inset params (fit pass
    2026-07-23) and the viewer page reads them from its own URL."""
    from fastapi.responses import RedirectResponse
    from Orchestrator.browser import native_stream
    from Orchestrator.browser.display import get_allocator
    target = native_stream.resolve_auto_view(
        get_allocator().active_sessions(), native_stream.probe_main_desktop())
    if target["kind"] == "none":
        return HTMLResponse(_CU_NOTHING_TO_SHOW.format(reason=target["reason"]))
    url = target["url"] + (f"?{query}" if query else "")
    return RedirectResponse(url, status_code=302)


@app.post("/cu/view/diag")
async def cu_view_diag(request: Request):
    """Client-side diagnostic beacon for the served live-view page (Android
    WebView black-screen hunt, 2026-07-23). The page POSTs lifecycle events
    (page-load / module-boot / rfb-connect / js-error / js-rejection) so a
    WebView with no devtools can testify server-side about what it actually
    did. Log-only; never errors back at the page."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    ua = request.headers.get("user-agent", "?")
    print(f"[CU-VIEW-DIAG] {body.get('event', '?')} sid={body.get('sid', '?')} "
          f"detail={str(body.get('detail', ''))[:300]} ua={ua[:120]}")
    return {"ok": True}


@app.get("/cu/view/{session_id}", response_class=HTMLResponse)
def cu_view(session_id: str, request: Request):
    """Serve the interactive CU live-view client (Portal/cu-view/, design
    2026-07-23 §4/M2). Route contract unchanged for real session ids: 404 for
    unknown sessions, install-hint page when live_view is off, HTML otherwise.
    The page reads its session id from its own URL path and sizes itself from
    /cu/sessions — no server-side templating. Two RESERVED ids are dispatched
    specially (never treated as session lookups): "main" streams the REAL
    desktop; "auto" 302s to the best available surface."""
    # Request-level breadcrumb (pairs with the page's /cu/view/diag beacons):
    # WHICH client asked for WHICH id — the first question when a frontend
    # renders black is whether its navigation ever reached us at all.
    _ua = request.headers.get("user-agent", "?")
    print(f"[CU-VIEW] GET /cu/view/{session_id} ua={_ua[:120]}")
    if session_id == "main":
        return _cu_view_main()
    if session_id == "auto":
        return _cu_view_auto(request.url.query or "")
    from Orchestrator.browser.display import get_allocator
    h = get_allocator().get(session_id)
    if h is None:
        print(f"[CU-VIEW] GET /cu/view/{session_id} -> 404 unknown session")
        return HTMLResponse("<!doctype html><body>No active CU session for that id.",
                            status_code=404)
    if not h.live_view:
        return HTMLResponse(_CU_VIEW_UNAVAILABLE)
    return _serve_cu_client_page(session_id)


async def _proxy_ws_to_loopback(websocket: WebSocket, target: str) -> None:
    """Pump an ALREADY-ACCEPTED client WS to a loopback websockify target and
    propagate the upstream close code. Shared by the per-session and
    main-desktop stream paths — ONE proxy code path."""
    import asyncio
    import websockets
    from websockets.exceptions import ConnectionClosed

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


async def _cu_view_ws_main(websocket: WebSocket) -> None:
    """WS /cu/view/main/ws — proxy to the refcounted REAL-desktop stream
    (native_stream spawns x11vnc+websockify on the first viewer, reaps after
    the last + grace). SECURITY: this streams the REAL desktop; the Tailscale
    perimeter is the auth boundary as everywhere else, and every connect and
    disconnect is logged here for auditability."""
    from Orchestrator.browser import native_stream
    await websocket.accept()
    mgr = native_stream.get_native_stream()
    try:
        ws_port = mgr.acquire()
    except RuntimeError as e:
        print(f"[CU-MAIN] main-desktop stream refused: {e}")
        await websocket.close(code=1008, reason=str(e)[:120])
        return
    client = getattr(websocket, "client", None)
    peer = f"{client.host}:{client.port}" if client else "unknown"
    print(f"[CU-MAIN] main-desktop stream WS CONNECT from {peer} "
          f"(viewers={mgr.viewers})")
    try:
        await _proxy_ws_to_loopback(websocket, f"ws://127.0.0.1:{ws_port}/")
    finally:
        mgr.release()
        print(f"[CU-MAIN] main-desktop stream WS DISCONNECT from {peer} "
              f"(viewers={mgr.viewers})")


@app.websocket("/cu/view/{session_id}/ws")
async def cu_view_ws(websocket: WebSocket, session_id: str):
    """Reverse-proxy the viewer's WebSocket to this session's loopback websockify.
    Loopback-only target; the Tailscale perimeter is the auth boundary (§9).
    The reserved id "main" is dispatched to the refcounted native-desktop
    stream — never a session lookup."""
    from Orchestrator.browser.display import get_allocator

    if session_id == "main":
        await _cu_view_ws_main(websocket)
        return
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
    await _proxy_ws_to_loopback(websocket, f"ws://127.0.0.1:{h.ws_port}/")
