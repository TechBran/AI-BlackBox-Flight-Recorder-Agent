"""Executor for control_phone — a frontier model delegates a device task to the
on-device Gemma running on the user's OWN phone, over Tailscale.

Inversion of the usual flow: instead of the phone calling back into the BlackBox,
the BlackBox reaches OUT to the phone. We resolve the originating operator's
reachable phone (mesh.resolve_origin), POST the task to the phone's tailnet HTTP
listener, then BLOCK — polling /status until the on-device run reaches a terminal
phase — and return the result. The frontier model should pre-announce ("Waking
Gemma on your phone — I'll report back…") before calling, since model load + run
can take a minute. Only SAFE device actions run remotely; the phone enforces an
allowlist.

Structured errors (data["error_kind"]) let the frontier model decide to retry or
stop. Resolution (mesh.resolve_device): invalid_target / origin_mismatch /
no_primary_device / no_device. Execution: refused / wake_failed / bad_response /
lost_contact / remote_error / timeout.
"""
import asyncio
import time

import aiohttp

from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.local_provider import mesh

# Defaults for reaching the phone's RemoteControlServer (Task 5). Tailscale already
# encrypts the transport (WireGuard), so plain HTTP over the tailnet is fine. All
# three are overridable via the [control_phone] section of config.ini; the defaults
# live here (mirrors the [context] local_* knob pattern).
REMOTE_CONTROL_PORT = 8765
POLL_INTERVAL_SECS = 2.0
TOTAL_TIMEOUT_SECS = 300.0          # ~5 min: model load (10-75s) + execution
# Tolerate transient /status drops: the phone's listener can briefly stop responding
# while it cold-loads the model on the GPU (heavy + memory-pressure). Only declare
# lost_contact after /status has failed continuously for this long.
LOST_CONTACT_GRACE_SECS = 60.0
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Terminal phases reported by the phone's /status (see RemoteTaskRunner, Task 7).
_PHASE_DONE = "done"
_PHASE_ERROR = "error"


def _control_port() -> int:
    """Phone listener port — [control_phone] port, default REMOTE_CONTROL_PORT."""
    try:
        from Orchestrator.config import CFG
        return CFG.getint("control_phone", "port", fallback=REMOTE_CONTROL_PORT)
    except Exception:
        return REMOTE_CONTROL_PORT


def _poll_interval_secs() -> float:
    """Status poll cadence — [control_phone] poll_interval_secs, default 2.0s."""
    try:
        from Orchestrator.config import CFG
        return CFG.getfloat("control_phone", "poll_interval_secs",
                            fallback=POLL_INTERVAL_SECS)
    except Exception:
        return POLL_INTERVAL_SECS


def _total_timeout_secs() -> float:
    """Overall blocking budget — [control_phone] timeout_secs, default 300s."""
    try:
        from Orchestrator.config import CFG
        return CFG.getfloat("control_phone", "timeout_secs", fallback=TOTAL_TIMEOUT_SECS)
    except Exception:
        return TOTAL_TIMEOUT_SECS


def _phone_base_url(node: mesh.Node) -> str:
    """Build the phone listener's base URL from its tailnet address."""
    host = node.dns_name or node.ip
    return f"http://{host}:{_control_port()}"


def _clip(value, limit: int = 300) -> str:
    """Clip a string so a huge __str__ can't flood model context (caps at `limit`)."""
    s = str(value)
    return s if len(s) <= limit else s[:limit - 1] + "…"


async def _post_task(base_url: str, payload: dict) -> dict:
    """POST the task to the phone's listener; return the JSON body. Test seam."""
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{base_url}/task", json=payload,
                                timeout=_HTTP_TIMEOUT) as resp:
            resp.raise_for_status()
            return await resp.json()


async def _get_status(base_url: str, task_id: str) -> dict:
    """GET the phone task's status; return the JSON body. Test seam."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base_url}/status/{task_id}",
                               timeout=_HTTP_TIMEOUT) as resp:
            resp.raise_for_status()
            return await resp.json()


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    task = (params.get("task") or "").strip()
    if not task:
        return ToolResult(False, "task is required (what to do on the phone).")

    # M3 origin-aware routing. control_phone drives the operator's OWN phone, so it
    # normally has no explicit target; resolve_device defaults to the origin device
    # (ctx.origin_device_id, once the Android app stamps it — 3.6 Android half) and,
    # when the origin is a non-device surface with no primary set, falls back to the
    # legacy single-attested-device path (resolve_origin) it always used. The optional
    # `device` param (if ever supplied) still targets any tailnet node.
    try:
        node = mesh.resolve_device(
            operator=ctx.operator,
            origin_device_id=ctx.origin_device_id,
            target_device_id=(params.get("device") or "").strip() or None,
        )
    except mesh.DeviceResolutionError as e:
        data = {"error_kind": e.kind}
        data.update(e.detail)
        return ToolResult(False, e.message, data=data)

    base_url = _phone_base_url(node)
    device = node.dns_name or node.ip

    # Wake + start the task on the phone.
    try:
        started = await _post_task(base_url, {"task": task, "operator": ctx.operator})
    except aiohttp.ClientResponseError as e:
        # The phone WAS reached but refused/errored. A 4xx (esp. 403 from the phone's
        # source/operator auth) is a deliberate refusal — distinct from "couldn't reach"
        # so the frontier model stops instead of pointlessly retrying a wake.
        if 400 <= e.status < 500:
            return ToolResult(
                False,
                f"The phone refused the task (HTTP {e.status}) — it may not be authorized "
                f"for this operator, or the listener rejected the caller.",
                data={"error_kind": "refused", "device": device, "http_status": e.status},
            )
        return ToolResult(
            False,
            f"The phone errored starting the task (HTTP {e.status}).",
            data={"error_kind": "wake_failed", "device": device, "http_status": e.status},
        )
    except Exception as e:  # connection refused, DNS failure, timeout, etc.
        return ToolResult(
            False,
            f"Could not reach the phone ({device}) to start the task: {_clip(e)}",
            data={"error_kind": "wake_failed", "device": device},
        )

    task_id = started.get("task_id") or started.get("id")
    if not task_id:
        return ToolResult(
            False,
            f"The phone did not return a task id (got: {_clip(started)}).",
            data={"error_kind": "bad_response", "device": device},
        )

    # Block, polling /status until the on-device run reaches a terminal phase.
    # CANCELLATION-SAFE: asyncio.CancelledError is a BaseException (Py3.8+), so the
    # `except Exception` clauses below do NOT swallow it — a cancelled turn aborts
    # the poll cleanly instead of being misreported as lost_contact/wake_failed.
    poll_interval = _poll_interval_secs()
    total_timeout = _total_timeout_secs()
    deadline = time.monotonic() + total_timeout
    last_phase = "waking"
    first_status_failure_at = None  # start of the current run of consecutive /status failures
    while True:
        try:
            status = await _get_status(base_url, task_id)
            first_status_failure_at = None  # responded → reset the failure run
        except Exception as e:
            # Transient drop — the listener can briefly stop responding during a heavy
            # GPU cold-load. Tolerate it for LOST_CONTACT_GRACE_SECS before giving up;
            # the overall deadline still caps the total wait.
            now = time.monotonic()
            if first_status_failure_at is None:
                first_status_failure_at = now
            if now - first_status_failure_at >= LOST_CONTACT_GRACE_SECS:
                return ToolResult(
                    False,
                    f"Lost contact with the phone ({device}) while it was "
                    f"'{last_phase}': {_clip(e)}",
                    data={"error_kind": "lost_contact", "device": device, "phase": last_phase},
                )
            if now >= deadline:
                return ToolResult(
                    False,
                    f"Timed out after {int(total_timeout)}s while the phone was "
                    f"'{last_phase}'. The model may still be loading — you can try again.",
                    data={"error_kind": "timeout", "device": device, "phase": last_phase},
                )
            await asyncio.sleep(poll_interval)
            continue

        phase = status.get("phase") or status.get("status") or "working"
        last_phase = phase

        if phase == _PHASE_DONE:
            return ToolResult(
                True,
                status.get("result") or "Done.",
                data={"phase": _PHASE_DONE, "device": device, "task_id": task_id},
            )
        if phase == _PHASE_ERROR:
            return ToolResult(
                False,
                status.get("error") or "The on-device task failed.",
                data={"error_kind": "remote_error", "device": device, "task_id": task_id},
            )
        if time.monotonic() >= deadline:
            return ToolResult(
                False,
                f"Timed out after {int(total_timeout)}s while the phone was "
                f"'{phase}'. The model may still be loading — you can try again.",
                data={"error_kind": "timeout", "device": device, "phase": phase},
            )
        await asyncio.sleep(poll_interval)
