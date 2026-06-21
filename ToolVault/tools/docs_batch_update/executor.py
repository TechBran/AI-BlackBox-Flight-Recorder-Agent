"""Executor for docs_batch_update — raw Google Docs batchUpdate passthrough."""
import json

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    from Orchestrator.gmail.service import workspace_connected
    from Orchestrator.google_workspace import docs

    operator = params.get("operator") or ctx.operator or "system"
    if not workspace_connected(operator):
        return ToolResult(False, f"Google Workspace not connected for {operator} — connect in onboarding")
    document_id = params.get("document_id", "")
    if not document_id:
        return ToolResult(False, "document_id is required")
    requests = params.get("requests")
    if not isinstance(requests, list):
        return ToolResult(False, "requests must be a list of Google Docs API request objects")
    result = docs.docs_batch_update(operator, document_id, requests)
    ok = "error" not in result
    return ToolResult(ok, json.dumps(result))
