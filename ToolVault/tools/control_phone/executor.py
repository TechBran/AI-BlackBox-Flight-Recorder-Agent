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
stop: no_device / wake_failed / bad_response / lost_contact / remote_error / timeout.
"""
import asyncio
import time

import aiohttp

from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.local_provider import mesh

# Port the phone's RemoteControlServer (Task 5) listens on. Tailscale already
# encrypts the transport (WireGuard), so plain HTTP over the tailnet is fine.
REMOTE_CONTROL_PORT = 8765
# Poll cadence + overall budget. Refined + made config-tunable in Task 4.
POLL_INTERVAL_SECS = 2.0
TOTAL_TIMEOUT_SECS = 300.0          # ~5 min: model load (10-75s) + execution
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Terminal phases reported by the phone's /status (see RemoteTaskRunner, Task 7).
_PHASE_DONE = "done"
_PHASE_ERROR = "error"


def _phone_base_url(node: mesh.Node) -> str:
    """Build the phone listener's base URL from its tailnet address."""
    host = node.dns_name or node.ip
    return f"http://{host}:{REMOTE_CONTROL_PORT}"


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

    node = mesh.resolve_origin(ctx.operator)
    if node is None:
        return ToolResult(
            False,
            "No reachable on-device Gemma for this operator — the phone may be "
            "offline, off the tailnet, or has not attested a model. Cannot run "
            "the task remotely.",
            data={"error_kind": "no_device"},
        )

    base_url = _phone_base_url(node)
    device = node.dns_name or node.ip

    # Wake + start the task on the phone.
    try:
        started = await _post_task(base_url, {"task": task, "operator": ctx.operator})
    except Exception as e:  # connection refused, DNS failure, timeout, etc.
        return ToolResult(
            False,
            f"Could not reach the phone ({device}) to start the task: {e}",
            data={"error_kind": "wake_failed", "device": device},
        )

    task_id = started.get("task_id") or started.get("id")
    if not task_id:
        return ToolResult(
            False,
            f"The phone did not return a task id (got: {started}).",
            data={"error_kind": "bad_response", "device": device},
        )

    # Block, polling /status until the on-device run reaches a terminal phase.
    deadline = time.monotonic() + TOTAL_TIMEOUT_SECS
    last_phase = "waking"
    while True:
        try:
            status = await _get_status(base_url, task_id)
        except Exception as e:
            return ToolResult(
                False,
                f"Lost contact with the phone ({device}) while it was "
                f"'{last_phase}': {e}",
                data={"error_kind": "lost_contact", "device": device, "phase": last_phase},
            )

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
                f"Timed out after {int(TOTAL_TIMEOUT_SECS)}s while the phone was "
                f"'{phase}'. The model may still be loading — you can try again.",
                data={"error_kind": "timeout", "device": device, "phase": phase},
            )
        await asyncio.sleep(POLL_INTERVAL_SECS)
