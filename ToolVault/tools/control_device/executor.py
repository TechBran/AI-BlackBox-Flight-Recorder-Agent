"""Executor for control_device — the frontier-driven device-control tool (M2, the MVP).

Unlike control_phone (which delegates to the on-device Gemma), this keeps the smart model
in the CLOUD: it resolves the operator's reachable device over the tailnet mesh, then runs a
server-side Gemini ReAct loop (Orchestrator.frontier_agent_loop.run_frontier_loop) that drives
the phone's M1 endpoints (GET /stream observations + POST /action) with hybrid tree+screenshot
grounding — the phone is only the hands. No on-device inference, so it starts fast.

Device resolution is origin-aware (M3): mesh.resolve_device(operator, origin_device_id,
target_device_id) implements the firm routing rule — an explicit `device` targets ANY tailnet
node; else the ORIGIN device (ctx.origin_device_id) it came from — but only if that device
belongs to this operator (never silently retarget); else the operator's PRIMARY device from the
registry; else an error. Safety gates live ON THE PHONE (M1/M4).

Structured errors (data["error_kind"]) let the frontier model decide to retry or stop:
no_device / no_primary_device / invalid_target / origin_mismatch / stopped / accessibility_off /
lost_contact / timeout / model_error / config_error / invalid_argument / max_steps / loop_error.
The loop SHORT-CIRCUITS on terminal device states (stopped = the user hit STOP;
accessibility_off = the a11y service is off; no_device from a not_wired result) instead of
burning model calls re-planning (F2).
"""
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.local_provider import mesh
from Orchestrator import frontier_agent_loop

REMOTE_CONTROL_PORT = 8765


def _control_port() -> int:
    """Phone listener port — [control_phone] port (shared with control_phone), default 8765."""
    try:
        from Orchestrator.config import CFG
        return CFG.getint("control_phone", "port", fallback=REMOTE_CONTROL_PORT)
    except Exception:
        return REMOTE_CONTROL_PORT


def _phone_base_url(node: mesh.Node) -> str:
    """Build the device listener's base URL from its tailnet address (dns_name preferred)."""
    host = node.dns_name or node.ip
    return f"http://{host}:{_control_port()}"


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    task = (params.get("task") or "").strip()
    if not task:
        return ToolResult(False, "task is required (what to do on the device).",
                          data={"error_kind": "invalid_argument"})

    device = (params.get("device") or "").strip()
    # M3 origin-aware routing: explicit device → any tailnet node; else the origin
    # device (must belong to this operator — never silent retarget); else primary.
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

    # Run the server-side frontier ReAct loop. capability=None → the loop starts from phone
    # defaults and adopts the device's authoritative device_capability from the first
    # observation. Model/provider come from config ([computer_use] frontier_*), not hardcoded.
    try:
        result = await frontier_agent_loop.run_frontier_loop(
            device_base_url=base_url,
            task=task,
            operator=ctx.operator,
            model=None,
            capability=None,
        )
    except Exception as e:  # never let an unexpected error escape the tool boundary
        return ToolResult(
            False,
            f"The device-control loop failed unexpectedly: {frontier_agent_loop._clip(e)}",
            data={"error_kind": "loop_error", "device": device_name})

    data = result.to_data()
    # Report the friendly tailnet device NAME (the loop's internal `device` is the base URL).
    data["device"] = device_name
    return ToolResult(result.success, result.message, data=data)
