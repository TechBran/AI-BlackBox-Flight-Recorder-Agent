"""Executor for stop_device_control — the M8.2 remote incident kill switch.

Reaches OUT to the operator's device (phone/tablet/XR) over Tailscale and halts in-flight
device-control: it resolves the device with the SAME origin-aware rule control_device uses
(mesh.resolve_device), then POSTs to the phone's RemoteControlServer kill endpoint —
``POST /kill-all`` (kill every in-flight task for this operator) or ``POST /kill/{task_id}``
(kill one). On the device, ``RemoteSessionBus.stopAll(operator)`` / ``stop(taskId)`` records
the task killed, so every subsequent ``/action`` + ``/stream`` frame for it is refused and the
consent banner drops. Operator-scoped + tailnet-gated exactly like ``/action`` (the phone's
authorize() rejects a foreign operator with 403). Never actuates the device — it only cancels.

Structured errors (data["error_kind"]): resolution (invalid_target / origin_mismatch /
no_primary_device / no_device) + delivery (refused / bad_response / lost_contact).
"""
import aiohttp

from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.local_provider import mesh

REMOTE_CONTROL_PORT = 8765
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)


def _control_port() -> int:
    """Phone listener port — [control_phone] port (shared), default 8765."""
    try:
        from Orchestrator.config import CFG
        return CFG.getint("control_phone", "port", fallback=REMOTE_CONTROL_PORT)
    except Exception:
        return REMOTE_CONTROL_PORT


def _phone_base_url(node: mesh.Node) -> str:
    """Build the device listener's base URL from its tailnet address (dns_name preferred)."""
    host = node.dns_name or node.ip
    return f"http://{host}:{_control_port()}"


def _clip(value, limit: int = 300) -> str:
    s = str(value)
    return s if len(s) <= limit else s[:limit - 1] + "…"


async def _post_kill(base_url: str, path: str, operator: str) -> dict:
    """POST the kill to the phone's listener; return the JSON body ({ok, killed_count}).
    Test seam — monkeypatched in unit tests so no socket is touched."""
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{base_url}{path}", json={"operator": operator},
                                timeout=_HTTP_TIMEOUT) as resp:
            resp.raise_for_status()
            return await resp.json()


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    device = (params.get("device") or "").strip()
    task_id = (params.get("task_id") or "").strip()

    # M3 origin-aware routing — identical rule to control_device: explicit device → any tailnet
    # node; else the origin device (must belong to this operator — never silent-retarget); else
    # the operator's PRIMARY device; else an error.
    try:
        node = mesh.resolve_device(
            operator=ctx.operator,
            origin_device_id=ctx.origin_device_id,
            target_device_id=device or None,
        )
    except mesh.DeviceResolutionError as e:
        data = {"error_kind": e.kind}
        data.update(e.detail)
        return ToolResult(False, e.message, data=data)

    base_url = _phone_base_url(node)
    device_name = node.dns_name or node.ip
    # A specific task → /kill/{id}; else the operator-wide kill-all (the common "stop" case).
    path = f"/kill/{task_id}" if task_id else "/kill-all"

    try:
        body = await _post_kill(base_url, path, ctx.operator)
    except aiohttp.ClientResponseError as e:
        # The device was reached but refused (e.g. 403 from the phone's operator-scope auth).
        if 400 <= e.status < 500:
            return ToolResult(
                False,
                f"The device refused the stop request (HTTP {e.status}) — it may not be "
                f"authorized for this operator.",
                data={"error_kind": "refused", "device": device_name, "http_status": e.status})
        return ToolResult(
            False,
            f"The device errored handling the stop request (HTTP {e.status}).",
            data={"error_kind": "bad_response", "device": device_name, "http_status": e.status})
    except Exception as e:  # connection refused / DNS / timeout — CancelledError is BaseException
        return ToolResult(
            False,
            f"Could not reach the device ({device_name}) to stop control: {_clip(e)}",
            data={"error_kind": "lost_contact", "device": device_name})

    killed = int(body.get("killed_count") or 0) if isinstance(body, dict) else 0
    scope = f"task {task_id}" if task_id else "all sessions"
    if killed > 0:
        msg = f"Stopped device control on {device_name} ({scope}) — {killed} in-flight task(s) halted."
    else:
        msg = (f"No in-flight device control was running on {device_name} for {scope} — "
               "nothing to stop (any future frame for a killed task is already refused).")
    return ToolResult(True, msg, data={"killed_count": killed, "device": device_name})
