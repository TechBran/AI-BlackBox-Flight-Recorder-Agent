"""
Sovereign Browser REST API endpoints
"""
import time
from typing import Optional, Dict, Any
from pydantic import BaseModel
from fastapi import Body
from starlette.requests import Request

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
        return {
            "display_running": bool(sessions),
            "native_mode": False,
            "virtual_sessions": len(sessions),
            "cap": MAX_VIRTUAL_SESSIONS,
            "sessions": sessions,
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
    return {"sessions": sessions, "count": len(sessions), "cap": MAX_VIRTUAL_SESSIONS}
