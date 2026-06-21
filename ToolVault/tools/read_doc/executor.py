"""Executor for read_doc."""
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
    result = docs.read_doc(operator, document_id)
    ok = "error" not in result
    return ToolResult(ok, json.dumps(result))
