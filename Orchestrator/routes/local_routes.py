#!/usr/bin/env python3
"""
local_routes.py — Tool-bridge endpoints for the on-device (local Gemma) provider.

The on-device Gemma model runs its agent loop ON the phone and keeps only a
handful of phone actuators + a `search_tools` meta-tool resident. Everything
else in ToolVault is pulled on demand through these two HTTP endpoints:

  POST /local/tools/search   — semantic tool discovery (returns ≤ k schemas)
  POST /local/tools/execute  — execute a ToolVault tool by name

Operator is caller-asserted (consistent with the Tailscale-perimeter trust
model already used by /gmail/execute).

NOTE: ``execute_tool`` and ``meta_tool`` are imported at MODULE TOP (not inside
the handlers) so tests can monkeypatch them on this module.
"""

import hashlib
import uuid
from typing import Optional

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse

from Orchestrator.behavioral_core import get_behavioral_core
from Orchestrator.checkpoint import app
from Orchestrator.config import CFG
from Orchestrator.context_builder import PROVIDER_CAPS, build_fossil_context
from Orchestrator.local_provider import get_local_registry
from Orchestrator.local_provider import mirror
from Orchestrator.local_provider.tool_injection import build_injected_tools
from Orchestrator.tools.blackbox_tools import execute_tool
from Orchestrator.toolvault import meta_tool

# Valid autonomy modes for the on-device agent loop (YOLO = act without asking,
# permission = ask before each actuator call).
_AUTONOMY_MODES = ("yolo", "permission")

# ---------------------------------------------------------------------------
# `local` model catalog — descriptive entries ONLY.
#
# These two Gemma models run ON the phone; the Orchestrator has NO server-side
# inference path for them. This catalog exists solely so the Android picker can
# render them (uniformly with the cloud providers served by /models/{provider}).
# They are surfaced ONLY for an operator with a verified on-device attestation
# (see the Task 0.1 registry); for everyone else /models/local returns an empty
# list + a reason, so the picker hides/disables the provider.
#
# Shape mirrors the cloud catalog entries (id/name + provider) plus an
# `on_device: true` marker. There is deliberately NO server-inference flag —
# any consumer keying on one will (correctly) find none.
# ---------------------------------------------------------------------------
LOCAL_MODELS = [
    {"id": "gemma-4-e2b", "name": "Gemma 4 E2B (on-device)", "provider": "local", "on_device": True},
    {"id": "gemma-4-e4b", "name": "Gemma 4 E4B (on-device)", "provider": "local", "on_device": True},
]

# Reason string returned when the provider is not available for the operator —
# the picker shows this when there is no verified device binding.
_LOCAL_UNAVAILABLE_REASON = "no verified on-device model"


def build_local_models_response(operator: Optional[str]) -> dict:
    """Build the /models/local payload, conditional on a verified device.

    Returns the two descriptive Gemma entries ONLY when the operator has a
    verified on-device attestation (``get_local_registry().status(op)["available"]``).
    Otherwise returns an empty list plus an availability signal so the picker
    can hide/disable the provider.

    Available:    {"provider": "local", "models": [..2..], "available": True}
    Unavailable:  {"provider": "local", "models": [], "available": False,
                   "reason": "no verified on-device model"}

    A missing/blank operator is treated as unavailable (a catalog read for
    nobody is a legitimately-empty result, not a malformed request — so unlike
    the device-status endpoint we do NOT 400 here; the picker just renders the
    empty/disabled state).

    NOTE: the response envelope deliberately diverges from the generic
    ``_wrap()`` contract (no ``source``/``default_id``/``fetched_iso``) because
    local has no upstream fetch/cache/server-default and is availability-gated.
    """
    if not isinstance(operator, str) or not operator.strip():
        return {"provider": "local", "models": [], "available": False,
                "reason": _LOCAL_UNAVAILABLE_REASON}

    available = bool(get_local_registry().status(operator).get("available"))
    if not available:
        return {"provider": "local", "models": [], "available": False,
                "reason": _LOCAL_UNAVAILABLE_REASON}

    # shallow-copy each entry (entries are flat) so callers can't mutate the
    # module-level catalog list/entries.
    return {"provider": "local", "models": [dict(m) for m in LOCAL_MODELS],
            "available": True}


# ---------------------------------------------------------------------------
# GET /local/models/catalog — server-side model MIRROR catalog (download metadata)
#
# The hub mirrors the Gemma LiteRT `.litertlm` bundles so phones download them
# from the hub over Tailscale. This endpoint lists WHAT is downloadable + its
# metadata (hf_repo/filename/size/sha/min_ram/guidance). This is DISTINCT from
# /models/local (the picker descriptors): same slugs, different fields. The
# actual ranged download is Task 1.2 — this is catalog only, no fetch here.
# ---------------------------------------------------------------------------
@app.get("/local/models/catalog")
async def local_models_catalog():
    """List the downloadable on-device model bundles (mirror metadata).

    Returns: {"bundles": [{slug, display_name, hf_repo, filename, size_bytes,
              sha256, min_ram_gb, recommended_for}, ...]}.
    """
    try:
        return {"bundles": mirror.list_bundles()}
    except Exception as e:
        print(f"[LOCAL PROVIDER] catalog failed: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# GET /local/models/download/{slug} — fetch-once + ranged/resumable download
#
# The hub fetches the .litertlm bytes from Hugging Face exactly ONCE
# (mirror.ensure_present), then phones stream them from here over Tailscale.
# Bundles are multi-GB, so we hand the file to Starlette's FileResponse, which
# streams it from disk (never loading the whole file into RAM — important on a
# host with documented memory pressure) and natively handles HTTP Range/resume:
# a Range header gets a 206 Partial Content + Content-Range, no Range header gets
# a 200 + the whole file, malformed ranges get 400, un-satisfiable ranges get
# 416, and Accept-Ranges: bytes is always advertised — all RFC 7233-correct.
# ---------------------------------------------------------------------------
@app.get("/local/models/download/{slug}")
async def local_models_download(slug: str):
    """Stream a mirrored on-device model bundle, with HTTP Range/resume support.

    Unknown slug → 404. Otherwise fetch-once via ``mirror.ensure_present`` and
    hand the path to ``FileResponse``, which streams the bytes from disk and
    handles Range natively (206 + ``Content-Range`` for a satisfiable range, 200
    + whole file otherwise, 400 for a malformed range, 416 for an un-satisfiable
    one; ``Accept-Ranges: bytes`` always set).
    """
    if mirror.get_bundle(slug) is None:
        return JSONResponse(
            {"success": False, "error": f"unknown bundle: {slug}"}, status_code=404
        )

    try:
        path = mirror.ensure_present(slug)
    except Exception as e:
        # A fetch failure should still 500 (not crash); the actual serving is
        # delegated to FileResponse below and streams from disk.
        print(f"[LOCAL PROVIDER] download failed: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    return FileResponse(path, media_type="application/octet-stream")


# ---------------------------------------------------------------------------
# POST /local/tools/search — semantic tool discovery
# ---------------------------------------------------------------------------
@app.post("/local/tools/search")
async def local_tools_search(request: Request):
    """Find tools by natural language query for the on-device model.

    Body: {"query": str, "operator"?: str, "k"?: int = 5}
    Returns: {"success": True, "tools": [{"name", "description", "parameters"}, ...]}
    (≤ k items). Empty/missing query → 400.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "error": "body must be a JSON object"}, status_code=400)

    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        return JSONResponse({"success": False, "error": "query required"}, status_code=400)

    k = body.get("k", 5)
    if not isinstance(k, int) or k < 1:
        k = 5

    # NOTE: body may carry "operator" for symmetry with /local/tools/execute, but
    # tool discovery is global/un-scoped — every operator searches the same vault.

    try:
        # Shared discovery->specs helper (also used by /local/turn/prepare). It
        # is total (never raises) and applies the same fault isolation: stale/
        # renamed hits whose `read` fails are skipped, not 500'd for the batch.
        tools = build_injected_tools(query, k)
        return {"success": True, "tools": tools}
    except Exception as e:
        print(f"[LOCAL PROVIDER] search failed: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# POST /local/tools/execute — execute a ToolVault tool
# ---------------------------------------------------------------------------
@app.post("/local/tools/execute")
async def local_tools_execute(request: Request):
    """Execute a ToolVault tool on behalf of the on-device model.

    Body: {"tool": str, "params"?: object, "operator"?: str = "system"}
    Returns: {"success": bool, "result": <tool result>}. Missing/blank tool → 400.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "error": "body must be a JSON object"}, status_code=400)

    tool = body.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        return JSONResponse({"success": False, "error": "tool required"}, status_code=400)

    operator = body.get("operator") or "system"
    params = body.get("params")
    if not isinstance(params, dict):
        params = {}
    params = dict(params)
    params["operator"] = operator

    try:
        result = await execute_tool(tool, params, operator)
        return {"success": bool(result.success), "result": result.result}
    except Exception as e:
        print(f"[LOCAL PROVIDER] execute failed: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# POST /local/turn/prepare — per-turn context assembly (server-bracketed turn)
#
# The first leg of the server-bracketed on-device turn: the phone POSTs the user
# prompt + operator; the BlackBox assembles a LEAN per-turn context package
# (persona + operator-scoped fossils + semantically-injected tools) and returns
# it; the phone then runs the on-device Gemma model locally on that package.
#
# Per-operator scoping is server-authoritative: the operator comes from the
# request body and is passed straight to build_fossil_context (consistent with
# the Tailscale-perimeter trust model). The lean LOCAL profile keeps the package
# inside the phone's small window: semantic_k=3 + checkpoint_count=1, NO recent/
# keyword blocks (the on-device agent loop needs the ~12K-token remainder).
# ---------------------------------------------------------------------------
@app.post("/local/turn/prepare")
async def local_turn_prepare(request: Request):
    """Assemble a lean per-turn context package for the on-device model.

    Body: {"prompt": str, "operator": str}. Blank prompt is allowed (assembly
    still returns checkpoint + persona); blank/missing operator -> 400.

    Returns: {"success": True, "turn_id", "system_prompt", "tools": [...],
              "provenance": {"semantic": [...], "checkpoint": [...]},
              "budget": {"package_chars", "cap_chars"}}.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "error": "body must be a JSON object"}, status_code=400)

    operator = body.get("operator")
    if not isinstance(operator, str) or not operator.strip():
        return JSONResponse({"success": False, "error": "operator required"}, status_code=400)

    # Blank/missing prompt is allowed — assembly still returns checkpoint +
    # persona. Pass it through to build_fossil_context as-is (it skips keyword/
    # semantic retrieval for an empty user_text).
    prompt = body.get("prompt") or ""

    try:
        # Lean LOCAL profile: a few semantic snapshots (the teleport) + the most
        # recent checkpoint, NO recent/keyword — kept small so the package leaves
        # ~12K of the phone's 16K window for the agent loop. Defaults (3/1/5) are
        # the Task-5-validated budget (persona ~2941 + ~1330/snapshot ≈ 8-10K <
        # the 16000-char local cap); exposed via [context] config so they can be
        # tuned on the live box (post-device timing) WITHOUT an APK/backend rebuild.
        fossil, prov = build_fossil_context(
            prompt,
            operator=operator,
            provider="local",
            semantic_k=CFG.getint("context", "local_semantic_k", fallback=3),
            checkpoint_count=CFG.getint("context", "local_checkpoint_count", fallback=1),
            include_recent=False,
            include_keyword=False,
        )
        tools = build_injected_tools(prompt, k=CFG.getint("context", "local_injected_tools_k", fallback=5))
        persona = get_behavioral_core("chat")
        # Don't leave a trailing blank fossil block when there are no fossils.
        system_prompt = persona + ("\n\n" + fossil if fossil else "")
        turn_id = uuid.uuid4().hex
        return {
            "success": True,
            "turn_id": turn_id,
            "system_prompt": system_prompt,
            "tools": tools,
            "provenance": {
                "semantic": prov.get("semantic", []),
                "checkpoint": prov.get("checkpoint", []),
            },
            "budget": {
                "package_chars": len(system_prompt),
                "cap_chars": PROVIDER_CAPS["local"],
            },
        }
    except Exception as e:
        print(f"[LOCAL PROVIDER] turn/prepare failed: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# POST /local/turn/complete - server-composed mint of a completed on-device turn
#
# The second leg of the server-bracketed on-device turn: after the phone runs
# the on-device Gemma model locally (on the package from /local/turn/prepare),
# it POSTs the completed turn here. The BlackBox composes the snapshot body
# SERVER-SIDE (the 4B never authors snapshot content), persists it, and
# AUTO-MINTS it (inline embedding) so the turn is instantly recallable - and
# the existing checkpoint cadence may fire. The persist+mint sequence is reused
# from chat_routes (lazy-imported below to avoid an import cycle).
# ---------------------------------------------------------------------------
@app.post("/local/turn/complete")
async def local_turn_complete(request: Request):
    """Persist + auto-mint a completed on-device turn (server-composed body).

    Body: {"turn_id": str, "operator": str, "prompt": str,
           "final_response": str, "tool_transcript"?: [{name,args,result}],
           "provenance"?: {}}.

    Blank operator -> 400; missing/blank final_response -> 400.
    Returns: {"success": True, "snap_id": str|None, "checkpoint_triggered": bool}.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "error": "body must be a JSON object"}, status_code=400)

    operator = body.get("operator")
    if not isinstance(operator, str) or not operator.strip():
        return JSONResponse({"success": False, "error": "operator required"}, status_code=400)

    final_response = body.get("final_response")
    if not isinstance(final_response, str) or not final_response.strip():
        return JSONResponse({"success": False, "error": "final_response required"}, status_code=400)

    prompt = body.get("prompt") or ""

    # Lazy-import to avoid an import cycle: chat_routes pulls in a large pile of
    # state/mint/task deps; importing it at module top here would create a cycle.
    from Orchestrator.routes.chat_routes import persist_local_turn_and_mint

    result = await persist_local_turn_and_mint(
        operator, prompt, final_response,
        tool_transcript=body.get("tool_transcript"),
        provenance=body.get("provenance"),
    )
    if result.get("error"):
        return JSONResponse({"success": False, "error": result["error"]}, status_code=500)
    return {
        "success": True,
        "snap_id": result["snap_id"],
        "checkpoint_triggered": result["checkpoint_triggered"],
    }


# ---------------------------------------------------------------------------
# POST /local/device/attest — register an operator's verified on-device model
# ---------------------------------------------------------------------------
@app.post("/local/device/attest")
async def local_device_attest(request: Request):
    """Record (upsert) which Gemma model an operator's device has verified.

    Body: {"operator": str, "device_id": str, "model_slug"?: str, "version"?: str,
           "sha256"?: str, "delegate"?: str, "autonomy_mode"? = "permission",
           "tailnet_name"?: str}
    Returns: {"success": True, "device": <record>}. Missing operator/device_id → 400.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "error": "body must be a JSON object"}, status_code=400)

    operator = body.get("operator")
    if not isinstance(operator, str) or not operator.strip():
        return JSONResponse({"success": False, "error": "operator required"}, status_code=400)

    device_id = body.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        return JSONResponse({"success": False, "error": "device_id required"}, status_code=400)

    autonomy_mode = body.get("autonomy_mode") or "permission"

    try:
        device = get_local_registry().attest(
            operator=operator,
            device_id=device_id,
            model_slug=body.get("model_slug"),
            version=body.get("version"),
            sha256=body.get("sha256"),
            delegate=body.get("delegate"),
            autonomy_mode=autonomy_mode,
            tailnet_name=body.get("tailnet_name"),
        )
        return {"success": True, "device": device}
    except Exception as e:
        print(f"[LOCAL PROVIDER] attest failed: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# GET /local/device/status — availability + attested models for an operator
# ---------------------------------------------------------------------------
@app.get("/local/device/status")
async def local_device_status(operator: Optional[str] = None):
    """Report whether the local provider is available for an operator.

    Query: ?operator=<str>
    Returns: {"available": bool, "models": [...]}. Missing operator → 400.
    """
    if not isinstance(operator, str) or not operator.strip():
        return JSONResponse({"success": False, "error": "operator required"}, status_code=400)

    try:
        return get_local_registry().status(operator)
    except Exception as e:
        print(f"[LOCAL PROVIDER] status failed: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# POST /local/device/autonomy — flip an attested device's autonomy mode
# ---------------------------------------------------------------------------
@app.post("/local/device/autonomy")
async def local_device_autonomy(request: Request):
    """Switch an attested device between YOLO and Permission autonomy modes.

    Body: {"operator": str, "device_id": str, "mode": "yolo"|"permission"}
    Returns: {"success": True, "device": <record>} on success;
             {"success": False, "error": "device not found"} (404) if unknown.
    Invalid mode → 400.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"success": False, "error": "body must be a JSON object"}, status_code=400)

    operator = body.get("operator")
    if not isinstance(operator, str) or not operator.strip():
        return JSONResponse({"success": False, "error": "operator required"}, status_code=400)

    device_id = body.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        return JSONResponse({"success": False, "error": "device_id required"}, status_code=400)

    mode = body.get("mode")
    if mode not in _AUTONOMY_MODES:
        return JSONResponse(
            {"success": False, "error": f"mode must be one of {list(_AUTONOMY_MODES)}"},
            status_code=400,
        )

    try:
        device = get_local_registry().set_autonomy(operator, device_id, mode)
        if device is None:
            return JSONResponse({"success": False, "error": "device not found"}, status_code=404)
        return {"success": True, "device": device}
    except Exception as e:
        print(f"[LOCAL PROVIDER] autonomy failed: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# GET /local/system-prompt — persona (behavioral core) for on-device parity
# ---------------------------------------------------------------------------
@app.get("/local/system-prompt")
async def local_system_prompt(operator: Optional[str] = None):
    """Return the BlackBox persona/tone/anti-sycophancy text for the on-device model.

    The on-device Gemma agent loop runs ON the phone and never hits the cloud
    /chat path that prepends the behavioral core, so it fetches that SAME text
    here once and caches it (works offline after first fetch).

    Sourced from ``behavioral_core.get_behavioral_core("chat")`` — the exact
    constant (``BEHAVIORAL_CORE_CHAT``) the chat path prepends — so persona is
    identical across providers; this endpoint does NOT re-author it.

    The persona is operator-independent (operator-specific context like memory
    and snapshots is functional content, not persona), so ``operator`` is
    accepted for symmetry but does not change the output.

    Query: ?operator=<str> (optional, ignored)
    Returns: {"prompt": <str>, "version": <stable 12-char sha256 hex of prompt>}.
    """
    try:
        prompt = get_behavioral_core("chat")
        version = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        return {"prompt": prompt, "version": version}
    except Exception as e:
        print(f"[LOCAL PROVIDER] system-prompt failed: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
