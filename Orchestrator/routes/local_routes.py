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

from fastapi import Request
from fastapi.responses import JSONResponse

from Orchestrator.checkpoint import app
from Orchestrator.local_provider import get_local_registry
from Orchestrator.tools.blackbox_tools import execute_tool
from Orchestrator.toolvault import meta_tool

# Valid autonomy modes for the on-device agent loop (YOLO = act without asking,
# permission = ask before each actuator call).
_AUTONOMY_MODES = ("yolo", "permission")


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
        search = meta_tool.execute("search", query=query)
        matches = (search.data or {}).get("matches", []) if search.success else []

        tools = []
        for m in matches[:k]:
            name = m.get("name")
            if not name:
                continue
            # The search result only carries {name, score}; pull the full schema
            # (and description) per hit via the meta-tool's read action.
            spec = meta_tool.execute("read", tool_name=name)
            # Fault isolation: a stale/renamed tool (read failure) is skipped, not
            # 500'd for the whole batch nor appended as an empty-schema garbage entry.
            if not spec.success:
                continue
            data = spec.data or {}
            tools.append({
                "name": name,
                "description": data.get("description", ""),
                # meta_tool calls it "schema"; expose as "parameters" for tool-def consumers
                "parameters": data.get("schema", {}),
            })

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
# POST /local/device/attest — register an operator's verified on-device model
# ---------------------------------------------------------------------------
@app.post("/local/device/attest")
async def local_device_attest(request: Request):
    """Record (upsert) which Gemma model an operator's device has verified.

    Body: {"operator": str, "device_id": str, "model_slug"?: str, "version"?: str,
           "sha256"?: str, "delegate"?: str, "autonomy_mode"? = "permission"}
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
        )
        return {"success": True, "device": device}
    except Exception as e:
        print(f"[LOCAL PROVIDER] attest failed: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# GET /local/device/status — availability + attested models for an operator
# ---------------------------------------------------------------------------
@app.get("/local/device/status")
async def local_device_status(operator: str = None):
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
