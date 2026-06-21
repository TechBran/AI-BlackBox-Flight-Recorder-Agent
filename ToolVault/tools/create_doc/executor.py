"""Executor for create_doc."""
import json

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    from Orchestrator.gmail.service import workspace_connected
    from Orchestrator.google_workspace import docs

    operator = params.get("operator") or ctx.operator or "system"
    if not workspace_connected(operator):
        return ToolResult(False, f"Google Workspace not connected for {operator} — connect in onboarding")
    title = params.get("title", "")
    if not title:
        return ToolResult(False, "title is required")
    result = docs.create_doc(operator, title, params.get("text"))
    ok = "error" not in result
    return ToolResult(ok, json.dumps(result))
