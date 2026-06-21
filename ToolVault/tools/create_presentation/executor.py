"""Executor for create_presentation."""
import json

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    from Orchestrator.gmail.service import workspace_connected
    from Orchestrator.google_workspace import slides

    operator = params.get("operator") or ctx.operator or "system"
    if not workspace_connected(operator):
        return ToolResult(False, f"Google Workspace not connected for {operator} — connect in onboarding")
    title = params.get("title", "")
    if not title:
        return ToolResult(False, "title is required")
    result = slides.create_presentation(operator, title)
    ok = "error" not in result
    return ToolResult(ok, json.dumps(result))
