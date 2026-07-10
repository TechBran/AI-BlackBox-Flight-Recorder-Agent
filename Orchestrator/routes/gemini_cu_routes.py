"""REST API routes for Gemini Computer Use."""
import asyncio
import json
import uuid
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List

from Orchestrator.gemini_cu import (
    get_or_create_session, get_session, destroy_session, run_gemini_cu_loop
)
from Orchestrator.gemini_cu.config import DEFAULT_CU_MODEL
from Orchestrator.device_registry import get_registry, DeviceProtocol

router = APIRouter(prefix="/gemini-cu", tags=["gemini-cu"])


async def _snapshot_cu_result(task_id: str, operator: str, device_id: str,
                               prompt: str, result_text: str,
                               screenshots: List[str], steps: int):
    """Save Gemini CU task result as a BlackBox snapshot via /chat/save
    direct persistence + auto-mint (no LLM round-trip)."""
    import httpx
    summary = (
        f"GEMINI COMPUTER USE — TASK RESULT\n\n"
        f"Task ID: {task_id}\n"
        f"Device: {device_id}\n"
        f"Prompt: {prompt}\n"
        f"Steps: {steps}\n"
        f"Screenshots: {len(screenshots)}\n\n"
        f"Result:\n{result_text}\n\n"
        f"Screenshots captured: {', '.join(screenshots)}"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "http://localhost:9091/chat/save",
            json={
                "operator": operator,
                "user_message": f"Gemini Computer Use task on {device_id}: {prompt}",
                "assistant_response": summary,
                "model": DEFAULT_CU_MODEL,
            }
        )
    # httpx does not raise on 4xx/5xx — without this a rejected save would
    # log success while the snapshot was silently never minted.
    resp.raise_for_status()
    print(f"[GEMINI CU] Snapshot saved for task {task_id}")


class GeminiCURequest(BaseModel):
    prompt: str
    operator: str
    device_id: str = "blackbox"
    model: str = DEFAULT_CU_MODEL
    url: Optional[str] = None
    system_prompt: Optional[str] = None


@router.post("/run")
async def run_gemini_cu(body: GeminiCURequest):
    """Start a Gemini Computer Use task. Returns a task_id for status polling."""
    registry = get_registry()
    device = registry.get_device(body.device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Device not found: {body.device_id}")

    if device.protocol == DeviceProtocol.ADB:
        environment = "android"
        try:
            from Orchestrator.adb import get_adb_manager
            result = await get_adb_manager().ensure_connected(body.device_id)
            if not result["success"]:
                raise HTTPException(status_code=400,
                                    detail=f"Cannot connect to device: {result.get('error')}")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500,
                                detail=f"ADB connection error: {str(e)}")
    else:
        environment = "browser"

    # Import task system
    try:
        from Orchestrator.tasks import create_task
        from Orchestrator.models import TaskType
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"Task system import error: {e}")

    task = create_task(
        TaskType.GEMINI_CU,
        operator=body.operator,
        prompt=body.prompt,
        result_data={
            "device_id": body.device_id,
            "environment": environment,
            "model": body.model,
            "url": body.url,
        }
    )

    asyncio.create_task(_run_task(
        task.task_id, body.operator, body.device_id, environment,
        body.prompt, body.model, body.system_prompt, body.url
    ))

    return {
        "task_id": task.task_id,
        "status": "pending",
        "device_id": body.device_id,
        "environment": environment
    }


async def _run_task(task_id, operator, device_id, environment,
                    prompt, model, system_prompt, url):
    """Background task that runs the Gemini CU loop and updates the task."""
    from Orchestrator.tasks import task_db
    from Orchestrator.models import TaskStatus
    task = task_db.get_task(task_id)
    if not task:
        return

    session = get_or_create_session(operator, device_id, environment)
    # A fresh task = fresh stop state (G2-T8). get_or_create_session returns the
    # PERSISTENT per-operator session (300s TTL). A cancel of a PRIOR task left
    # stop_requested=True on it, and the task path never resets it (run_gemini_cu_loop
    # resets status/current_step but NOT stop_requested, and _run_task doesn't call
    # reset_task_state — only the chat CU path does). Without this line the operator's
    # NEXT task would break at step 1 and be silently voided by the mint guard below.
    session.stop_requested = False
    screenshots = []
    final_text = ""

    # ── Per-launch display claim (M1-T6). This is the sixth CU launch site.
    #    Only local-display environments claim; android is ADB and never touches
    #    the local X server (control_android_device routes through here too and
    #    must stay unaffected — gate on the display, not the route). The claim key
    #    is per-launch (this task id); released in the finally below. ──
    from Orchestrator.browser.display_arbiter import (
        try_claim, release_claim, is_local_environment,
    )
    _claims_display = is_local_environment(environment)
    _claim_id = f"gemini-route-run:{task_id}"
    if _claims_display:
        owner = try_claim("gemini-task", operator, _claim_id, session_id=session.session_id)
        if owner is not None:
            task.status = TaskStatus.FAILED
            task.result_data["error"] = f"Cannot start Gemini CU — {owner.describe()}. Stop it first."
            task_db.save_task(task)
            return

    try:
        task.status = TaskStatus.PROCESSING
        task_db.save_task(task)
        async for event in run_gemini_cu_loop(
            session, prompt, model, system_prompt, url
        ):
            event_type = event.get("type")
            if event_type == "cu_step":
                print(f"[GEMINI CU] Task {task_id} — Step {event['data']['step']}/{event['data']['total']}")
            elif event_type == "cu_action":
                print(f"[GEMINI CU] Task {task_id} — Action: {event['data']['action']} params={event['data'].get('params', {})}")
            elif event_type == "cu_screenshot":
                screenshots.append(event["data"]["url"])
                print(f"[GEMINI CU] Task {task_id} — Screenshot: {event['data']['url']}")
            elif event_type == "content":
                print(f"[GEMINI CU] Task {task_id} — Text: {event['data']['text'][:150]}")
            elif event_type == "cu_safety":
                print(f"[GEMINI CU] Task {task_id} — Safety decision acknowledged")
            elif event_type == "done":
                final_text = event["data"].get("content", "")
                print(f"[GEMINI CU] Task {task_id} — Done: {final_text[:150]}")
            elif event_type == "error":
                print(f"[GEMINI CU] Task {task_id} — Error: {event['data']['message']}")
                task.status = TaskStatus.FAILED
                task.result_data["error"] = event["data"]["message"]
                task_db.save_task(task)
                return

        # MINT HYGIENE (G2-T8): if this task was cancelled, land it CANCELLED and
        # RETURN before writing a "completed" record and before the auto-snapshot.
        # A cancelled agent's partial output must NEVER enter the immutable ledger
        # (the ledger is memory future sessions treat as ground truth — a minted
        # half-finished result reads exactly like completed work; that is the
        # Android error-mint bug). Mirrors the USE_COMPUTER guard in
        # tasks.process_browser_use.
        #
        # SIGNAL: session.stop_requested is authoritative and race-free HERE.
        # request_stop() sets it on the exact session this task drives, and the
        # gemini loop exits cleanly via `break` (not an exception), so we reach
        # this point. It reflects only THIS task's cancel because _run_task
        # cleared it at entry (see the reset above) — reset_task_state() DOES
        # clear stop_requested and HAS callers (the chat CU path in chat_routes),
        # but the task path does not use it, hence that explicit reset. The worker
        # thread's process_task `finally` clears the per-task_id is_cancel_requested
        # flag (GEMINI_CU returns from process_task early) but never touches
        # stop_requested. is_cancel_requested is OR'd in for symmetry with the
        # USE_COMPUTER guard + a flag-only cancel.
        from Orchestrator.tasks import is_cancel_requested
        if getattr(session, "stop_requested", False) or is_cancel_requested(task_id):
            task.status = TaskStatus.CANCELLED
            if isinstance(task.result_data, dict):
                task.result_data["cancelled"] = True
            task.progress = 0
            task_db.save_task(task)
            print(f"[GEMINI CU] Task {task_id} cancelled — CANCELLED, skipping auto-snapshot")
            return

        task.status = TaskStatus.COMPLETED
        task.result_data.update({
            "result_text": final_text,
            "screenshots": screenshots,
            "final_screenshot": screenshots[-1] if screenshots else None,
            "steps": session.current_step,
            "tokens": session.total_tokens,
        })
        if screenshots:
            task.result_url = screenshots[-1]
        task.progress = 100
        task_db.save_task(task)
        print(f"[GEMINI CU] Task {task_id} completed: {session.current_step} steps, {len(screenshots)} screenshots")

        # Auto-snapshot the result into BlackBox memory
        try:
            await _snapshot_cu_result(task_id, operator, device_id, prompt,
                                      final_text, screenshots, session.current_step)
        except Exception as snap_err:
            print(f"[GEMINI CU] Snapshot failed (non-fatal): {snap_err}")
    except Exception as e:
        import traceback
        error_msg = str(e) or f"{type(e).__name__}: {repr(e)}"
        task.status = TaskStatus.FAILED
        task.result_data["error"] = error_msg
        task_db.save_task(task)
        print(f"[GEMINI CU] Task {task_id} failed: {error_msg}")
        traceback.print_exc()
    finally:
        # Release the per-launch display claim on every exit (invariant 4).
        # Idempotent + a no-op for android (never claimed). The driver runs inline
        # in this task, so this task's completion IS the driver's lifecycle.
        release_claim(_claim_id)


@router.post("/stream")
async def stream_gemini_cu(body: GeminiCURequest):
    """Stream Gemini CU events via SSE."""
    registry = get_registry()
    device = registry.get_device(body.device_id)
    if not device:
        raise HTTPException(status_code=404, detail=f"Device not found: {body.device_id}")

    if device.protocol == DeviceProtocol.ADB:
        environment = "android"
        from Orchestrator.adb import get_adb_manager
        result = await get_adb_manager().ensure_connected(body.device_id)
        if not result["success"]:
            raise HTTPException(status_code=400,
                                detail=f"Cannot connect: {result.get('error')}")
    else:
        environment = "browser"

    session = get_or_create_session(body.operator, body.device_id, environment)

    # ── Per-launch display claim (M1-T6). The driver runs INLINE in this
    #    generator (not a detached background task), so the generator's finally IS
    #    the driver's lifecycle — no Hole-3 disconnect window here. Android never
    #    claims. Claim + release live inside the generator so a stream that is
    #    never consumed cannot leak a claim. ──
    from Orchestrator.browser.display_arbiter import (
        try_claim, release_claim, is_local_environment,
    )
    _claims_display = is_local_environment(environment)
    _claim_id = f"gemini-route-stream:{uuid.uuid4()}"

    async def event_stream():
        try:
            if _claims_display:
                owner = try_claim("gemini-task", body.operator, _claim_id,
                                  session_id=session.session_id)
                if owner is not None:
                    yield f"data: {json.dumps({'type': 'error', 'data': owner.describe()})}\n\n"
                    return
            async for event in run_gemini_cu_loop(
                session, body.prompt, body.model, body.system_prompt, body.url
            ):
                yield f"data: {json.dumps(event)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            release_claim(_claim_id)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/session/{operator}")
async def get_session_info(operator: str):
    session = get_session(operator)
    if not session:
        return {"active": False}
    return {
        "active": True,
        "session_id": session.session_id,
        "device_id": session.device_id,
        "environment": session.environment,
        "status": session.status,
        "current_step": session.current_step,
        "screenshot_count": session.screenshot_count,
        "tokens": session.total_tokens,
    }


@router.delete("/session/{operator}")
async def end_session(operator: str):
    destroy_session(operator)
    return {"status": "destroyed"}
