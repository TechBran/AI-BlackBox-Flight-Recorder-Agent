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
from Orchestrator.tools.blackbox_tools import execute_tool
from Orchestrator.toolvault import meta_tool


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
            data = spec.data or {}
            tools.append({
                "name": name,
                "description": data.get("description", ""),
                "parameters": data.get("schema", {}),
            })

        return {"success": True, "tools": tools}
    except Exception as e:
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
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
