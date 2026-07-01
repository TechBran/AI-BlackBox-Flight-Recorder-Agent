"""Executor for control_device — the frontier-driven device-control tool (M2, the MVP).

Unlike control_phone (which delegates to the on-device Gemma), this keeps the smart model
in the CLOUD: it resolves the operator's reachable device over the tailnet mesh, then runs a
server-side Gemini ReAct loop (Orchestrator.frontier_agent_loop.run_frontier_loop) that drives
the phone's M1 endpoints (GET /stream observations + POST /action) with hybrid tree+screenshot
grounding — the phone is only the hands. No on-device inference, so it starts fast.

Device resolution reuses control_phone's mesh resolution for M2 (resolve_origin, plus a
best-effort match when an explicit `device` is given). M3 replaces this with origin-aware
mesh.resolve_device + the device registry. Safety gates live ON THE PHONE (M1/M4).

Structured errors (data["error_kind"]) let the frontier model decide to retry or stop:
no_device / stopped / accessibility_off / lost_contact / timeout / model_error / config_error /
invalid_argument / invalid_target / max_steps / loop_error. The loop SHORT-CIRCUITS on the
terminal device states (stopped = the user hit STOP; accessibility_off = the a11y service is
off; no_device from a not_wired result) instead of burning model calls re-planning (F2).
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


def _resolve_device_node(operator: str, device: str):
    """Resolve the target tailnet node (M2 — reuses control_phone's mesh resolution).

    Explicit `device` → best-effort name match among the operator's reachable devices
    (None if it doesn't match a reachable node → an honest invalid_target). No `device` →
    the originating operator's reachable device (resolve_origin). M3 replaces this with the
    origin-aware mesh.resolve_device + registry (explicit → origin → primary → error).
    """
    device = (device or "").strip()
    if device:
        for rec in mesh.reachable_devices(operator=operator):
            node = mesh.Node(**rec["node"])
            if mesh._name_matches(device, node):
                return node, None
        return None, "invalid_target"
    node = mesh.resolve_origin(operator)
    return node, (None if node else "no_device")


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    task = (params.get("task") or "").strip()
    if not task:
        return ToolResult(False, "task is required (what to do on the device).",
                          data={"error_kind": "invalid_argument"})

    device = (params.get("device") or "").strip()
    node, err = _resolve_device_node(ctx.operator, device)
    if node is None:
        if err == "invalid_target":
            return ToolResult(
                False,
                f"No reachable device named '{device}' for this operator. It may be offline, "
                "off the tailnet, or not attested. Check the name or omit it to use the "
                "originating device.",
                data={"error_kind": "invalid_target", "requested": device})
        return ToolResult(
            False,
            "No reachable device for this operator — the phone may be offline, off the "
            "tailnet, or has not attested. Cannot drive it remotely.",
            data={"error_kind": "no_device"})

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
