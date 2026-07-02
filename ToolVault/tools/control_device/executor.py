"""Executor for control_device — the provider-agnostic frontier device-control tool (M2/M7).

Unlike control_phone (which delegates to the on-device Gemma), this keeps the smart model in
the CLOUD: it resolves the operator's reachable device over the tailnet mesh, then runs a
server-side ReAct loop (Orchestrator.frontier_agent_loop.run_frontier_loop) that drives the
phone's M1 endpoints (GET /stream observations + POST /action) with hybrid tree+screenshot
grounding — the phone is only the hands. No on-device inference, so it starts fast.

Provider selection (M7 — the frontier brain is now one of several):
  1. an explicit ``provider`` param (gemini / claude / openai / gemma) — highest precedence;
  2. else the TARGET device's ``default_provider`` from the M3 device registry (this is what
     makes the persisted-but-unconsumed M3 field LIVE);
  3. else the box default ([computer_use] frontier_provider, CU_FRONTIER_PROVIDER).
``gemma`` routes to the ON-DEVICE Gemma path (delegates to control_phone / the phone's
``/task``) instead of the cloud loop — so Gemma is one provider among Gemini/Claude/OpenAI
behind the SAME seam. gemini/claude/openai run the cloud loop; the loop builds the matching
driver + coordinate adapter (frontier_agent_loop._make_driver / frontier_grounding).

Device resolution is origin-aware (M3): mesh.resolve_device(operator, origin_device_id,
target_device_id) implements the firm routing rule — an explicit `device` targets ANY tailnet
node; else the ORIGIN device (ctx.origin_device_id) it came from — but only if that device
belongs to this operator (never silently retarget); else the operator's PRIMARY device from the
registry; else an error. Safety gates live ON THE PHONE (M1/M4).

Structured errors (data["error_kind"]) let the frontier model decide to retry or stop:
no_device / no_primary_device / invalid_target / origin_mismatch / invalid_argument (bad
provider) / stopped / accessibility_off / lost_contact / timeout / model_error / config_error /
max_steps / loop_error. The loop SHORT-CIRCUITS on terminal device states instead of burning
model calls (F2).
"""
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.local_provider import mesh
from Orchestrator import frontier_agent_loop

REMOTE_CONTROL_PORT = 8765

# The providers control_device understands. Cloud loop: gemini/claude/openai (+ their
# aliases google/anthropic/gpt). On-device opt-in: gemma (→ control_phone). Kept in sync with
# the device_registry VALID_DEFAULT_PROVIDERS (gemma/gemini/claude/openai) plus loop aliases.
_CLOUD_PROVIDERS = frozenset({"gemini", "google", "claude", "anthropic", "openai", "gpt"})
_GEMMA_PROVIDER = "gemma"
_KNOWN_PROVIDERS = _CLOUD_PROVIDERS | {_GEMMA_PROVIDER}


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


def _config_default_provider() -> str:
    """The box default frontier provider ([computer_use] frontier_provider)."""
    try:
        from Orchestrator.config import CU_FRONTIER_PROVIDER
        return (CU_FRONTIER_PROVIDER or "gemini").strip().lower() or "gemini"
    except Exception:
        return "gemini"


def _resolve_provider(params: dict, node: mesh.Node) -> str:
    """Pick the frontier provider (M7 7.5): explicit param → device default_provider → config
    default. Lower-cased; validation of an explicit value happens in execute()."""
    explicit = (params.get("provider") or "").strip().lower()
    if explicit:
        return explicit
    device_default = mesh.default_provider_for_node(node)
    if device_default:
        return device_default.strip().lower()
    return _config_default_provider()


async def _run_gemma(params: dict, ctx: ToolContext) -> ToolResult:
    """Route to the on-device Gemma path — the SAME actuation, brain on the phone (M7 7.4).
    Delegates to the control_phone executor (resolves the device + drives the phone's /task).
    """
    from Orchestrator.toolvault import registry
    control_phone = registry.get_executor("control_phone")
    if control_phone is None:
        return ToolResult(
            False,
            "The on-device (gemma) path is unavailable — the control_phone tool did not load.",
            data={"error_kind": "config_error", "provider": "gemma"})
    # control_phone reads the same params (task, device) + ctx (operator, origin_device_id).
    result = await control_phone(params, ctx)
    if getattr(result, "data", None) is not None:
        result.data.setdefault("provider", "gemma")
    return result


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    task = (params.get("task") or "").strip()
    if not task:
        return ToolResult(False, "task is required (what to do on the device).",
                          data={"error_kind": "invalid_argument"})

    # Validate an explicit provider up front (before touching the tailnet) so a typo is a clean
    # invalid_argument, not a confusing downstream failure.
    explicit_provider = (params.get("provider") or "").strip().lower()
    if explicit_provider and explicit_provider not in _KNOWN_PROVIDERS:
        return ToolResult(
            False,
            f"Unknown provider '{explicit_provider}'. Expected one of: gemini, claude, openai, gemma.",
            data={"error_kind": "invalid_argument", "provider": explicit_provider})

    device = (params.get("device") or "").strip()
    # M3 origin-aware routing: explicit device → any tailnet node; else the origin device
    # (must belong to this operator — never silent retarget); else primary.
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

    # M7 provider selection: explicit param → device default_provider (M3) → config default.
    provider = _resolve_provider(params, node)

    # M7-M4: validate the RESOLVED provider uniformly (belt-and-suspenders). The explicit param
    # is already checked above; this additionally catches a bad device default_provider (the M3
    # registry sanitizes it, but a hypothetical bad value now reads as invalid_argument here
    # instead of surfacing as a confusing downstream config_error from the loop's driver factory).
    if provider not in _KNOWN_PROVIDERS:
        return ToolResult(
            False,
            f"Unknown provider '{provider}'. Expected one of: gemini, claude, openai, gemma.",
            data={"error_kind": "invalid_argument", "provider": provider})

    # gemma = the on-device opt-in path behind the same seam (M7 7.4).
    if provider == _GEMMA_PROVIDER:
        return await _run_gemma(params, ctx)

    base_url = _phone_base_url(node)
    device_name = node.dns_name or node.ip
    model = (params.get("model") or "").strip() or None

    # Run the server-side frontier ReAct loop. capability=None → the loop starts from phone
    # defaults and adopts the device's authoritative device_capability from the first
    # observation. The loop builds the driver + coordinate adapter for `provider`; model
    # defaults to that provider's configured frontier model.
    try:
        result = await frontier_agent_loop.run_frontier_loop(
            device_base_url=base_url,
            task=task,
            operator=ctx.operator,
            model=model,
            capability=None,
            provider=provider,
        )
    except Exception as e:  # never let an unexpected error escape the tool boundary
        return ToolResult(
            False,
            f"The device-control loop failed unexpectedly: {frontier_agent_loop._clip(e)}",
            data={"error_kind": "loop_error", "device": device_name, "provider": provider})

    data = result.to_data()
    # Report the friendly tailnet device NAME (the loop's internal `device` is the base URL).
    data["device"] = device_name
    data["provider"] = provider
    return ToolResult(result.success, result.message, data=data)
