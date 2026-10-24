"""REST API routes for Gemini Computer Use."""
import asyncio
import json
import uuid
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List

from Orchestrator.gemini_cu import get_session, destroy_session, run_gemini_cu_loop
from Orchestrator.gemini_cu.config import DEFAULT_CU_MODEL
from Orchestrator.gemini_cu.session_manager import (
    create_task_session, destroy_task_session,
)
from Orchestrator.device_registry import get_registry, DeviceProtocol
from Orchestrator.utils.operators import InvalidOperatorName, validate_operator

router = APIRouter(prefix="/gemini-cu", tags=["gemini-cu"])

# Strong references to the background drivers start_gemini_cu_task launches: the
# event loop holds only a weak reference to a task, so a bare create_task can be
# garbage-collected mid-run (audit O13-computer-use-15, O18-toolvault-modules-10).
_BACKGROUND_TASKS: set = set()


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
    from Orchestrator.config import orchestrator_url
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            orchestrator_url("/chat/save"),
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


def _require_safe_operator(operator: str) -> None:
    """400 for an operator unsafe to put in a path or the ledger. The Gemini
    session refuses one too, but only once the background task is running."""
    try:
        validate_operator(operator)
    except InvalidOperatorName as e:
        raise HTTPException(status_code=400, detail=f"Invalid operator: {e}")


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
    _require_safe_operator(body.operator)
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

    start_gemini_cu_task(
        task.task_id, body.operator, body.device_id, environment,
        body.prompt, body.model, body.system_prompt, body.url)

    return {
        "task_id": task.task_id,
        "status": "pending",
        "device_id": body.device_id,
        "environment": environment
    }


def start_gemini_cu_task(task_id, operator, device_id, environment, prompt,
                         model=DEFAULT_CU_MODEL, system_prompt=None, url=None):
    """Launch _run_task in the background for an already-created GEMINI_CU task row.

    The public entry point for every caller (this router's /run and the
    control_android_device tool). The driver is kept in _BACKGROUND_TASKS until it
    finishes, and an exception that escapes the runner is logged and written to
    the task row instead of leaving it on "running" forever.

    The driver always runs on the server's long-lived loop. The non-stream chat
    path (tasks.process_chat_task -> call_anthropic / call_gemini) runs a tool
    inside asyncio.run on a worker thread, and a task created on THAT loop was
    cancelled the moment the tool returned, so the row stayed PENDING or
    PROCESSING forever (audit O18-toolvault-modules-10). From another loop or
    thread this returns a concurrent.futures.Future; on the server loop itself,
    an asyncio.Task."""
    coro = _run_task(task_id, operator, device_id, environment, prompt, model,
                     system_prompt, url)
    from Orchestrator.notifications.bridge import get_main_loop
    server_loop = get_main_loop()
    try:
        here = asyncio.get_running_loop()
    except RuntimeError:
        here = None
    if server_loop is not None and server_loop is not here:
        bg = asyncio.run_coroutine_threadsafe(coro, server_loop)
    elif here is not None:
        bg = here.create_task(coro, name=f"gemini-cu:{task_id}")
    else:
        coro.close()
        _record_task_failure(task_id, "No event loop is available to run the Gemini CU task")
        raise RuntimeError("no event loop to run the Gemini CU task on")
    _BACKGROUND_TASKS.add(bg)

    def _done(t):
        _BACKGROUND_TASKS.discard(t)
        if t.cancelled():
            # Cancelled with its loop (shutdown, or a throwaway loop with no
            # server loop captured). A cancel by the operator already marked the
            # row CANCELLED, which is sticky; anything else must not stay running.
            _record_task_failure(
                task_id, "Gemini CU task was interrupted before it finished "
                         "(its event loop shut down)", only_if_running=True)
            return
        exc = t.exception()
        if exc is None:
            return
        print(f"[GEMINI CU] Task {task_id} crashed: {type(exc).__name__}: {exc}")
        _record_task_failure(task_id, str(exc) or type(exc).__name__)

    bg.add_done_callback(_done)
    return bg


def _record_task_failure(task_id: str, message: str, only_if_running: bool = False) -> None:
    """Best-effort FAILED write for a driver that cannot report its own end."""
    try:
        from Orchestrator.tasks import task_db, update_task
        from Orchestrator.models import TaskStatus
        if only_if_running:
            row = task_db.get_task(task_id)
            if row is None or row.status not in (TaskStatus.PENDING, TaskStatus.PROCESSING):
                return
        update_task(task_id, status=TaskStatus.FAILED, error_message=message)
    except Exception as e:  # pragma: no cover - best effort bookkeeping
        print(f"[GEMINI CU] could not record the end of task {task_id}: {e}")


async def _run_task(task_id, operator, device_id, environment,
                    prompt, model, system_prompt, url):
    """Background task that runs the Gemini CU loop and updates the task.

    The task drives its OWN session (create_task_session), built with exactly the
    requested device/environment and an empty history, and torn down on exit. The
    operator-cached chat session used to be borrowed here: a cache hit came back
    as-is, so a session made for an ADB phone kept driving the phone when the next
    task targeted the desktop (audit O13-computer-use-15), and the task inherited
    the chat's history and a previous task's stop flag."""
    from Orchestrator.tasks import (
        task_db, is_cancel_requested, register_cancel_handle, unregister_cancel_handle,
        clear_cancel_request,
    )
    from Orchestrator.models import TaskStatus
    task = task_db.get_task(task_id)
    if not task:
        return
    if task.status == TaskStatus.CANCELLED or is_cancel_requested(task_id):
        # Cancelled before the driver started: there is no session to stop.
        clear_cancel_request(task_id)
        return

    try:
        session = await asyncio.to_thread(create_task_session, operator, device_id, environment)
    except Exception as e:
        task.status = TaskStatus.FAILED
        task.result_data["error"] = f"Cannot start Gemini CU: {e}"
        task_db.save_task(task)
        return
    # The row learns its session (live view + cancel find THIS desktop), and a
    # cancel resolves this session by id rather than the operator's chat session.
    task.session_id = session.session_id
    if isinstance(task.result_data, dict):
        task.result_data["session_id"] = session.session_id
    register_cancel_handle(task_id, "cu", operator=operator,
                           session_id=session.session_id, model=model or "")
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
            unregister_cancel_handle(task_id)
            await asyncio.to_thread(destroy_task_session, session)  # teardown (Chrome + display) off the loop
            return

    try:
        task.status = TaskStatus.PROCESSING
        task_db.save_task(task)
        async for event in run_gemini_cu_loop(
            session, prompt, model, system_prompt, url
        ):
            # A cancel stops the driver at its next step even when the cu-stop
            # lookup could not reach this session.
            if is_cancel_requested(task_id) and not session.stop_requested:
                stop = getattr(session, "request_stop", None)
                if callable(stop):
                    stop()
                else:
                    session.stop_requested = True
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
        # this point. It reflects only THIS task's cancel because the session is
        # this task's own (created un-stopped above). The background worker never
        # sees a GEMINI_CU row (tasks._ROUTE_OWNED_TASK_TYPES), so the cancel
        # handle and the is_cancel_requested flag live until this task's own
        # `finally`. is_cancel_requested is OR'd in for symmetry with the
        # USE_COMPUTER guard + a flag-only cancel.
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
        # in this task, so this task's completion IS the driver's lifecycle — and
        # so is this task's own session.
        release_claim(_claim_id)
        unregister_cancel_handle(task_id)
        clear_cancel_request(task_id)
        await asyncio.to_thread(destroy_task_session, session)  # teardown (Chrome + display) off the loop
@router.post("/stream")
async def stream_gemini_cu(body: GeminiCURequest):
    """Stream Gemini CU events via SSE."""
    _require_safe_operator(body.operator)
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

    # ── Per-launch display claim (M1-T6). The driver runs INLINE in this
    #    generator (not a detached background task), so the generator's finally IS
    #    the driver's lifecycle — no Hole-3 disconnect window here. Android never
    #    claims. Claim + release live inside the generator so a stream that is
    #    never consumed cannot leak a claim — and so does the stream's own session
    #    (create_task_session: exactly the requested target, empty history; see
    #    _run_task). ──
    from Orchestrator.browser.display_arbiter import (
        try_claim, release_claim, is_local_environment,
    )
    _claims_display = is_local_environment(environment)
    _claim_id = f"gemini-route-stream:{uuid.uuid4()}"

    async def event_stream():
        session = await asyncio.to_thread(create_task_session, body.operator, body.device_id, environment)
        try:
            # The stream's own session is reachable only by id (live-view stop,
            # /chat/cu-stop?session_id=), so the client is told it first.
            yield f"data: {json.dumps({'type': 'session', 'data': {'session_id': session.session_id}})}\n\n"
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
            await asyncio.to_thread(destroy_task_session, session)  # teardown (Chrome + display) off the loop
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
    """End the operator's chat session and stop their running /run and /stream tasks
    (each drives its own task session, registered by id only; it tears itself down)."""
    await asyncio.to_thread(destroy_session, operator)
    from Orchestrator.gemini_cu.session_manager import list_task_sessions
    stopped = 0
    for task_session in list_task_sessions(operator):
        task_session.request_stop()
        stopped += 1
    return {"status": "destroyed", "stopped_tasks": stopped}
