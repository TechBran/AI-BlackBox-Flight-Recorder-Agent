"""Executor for read_presentation."""
import json

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    from Orchestrator.gmail.service import workspace_connected
    from Orchestrator.google_workspace import slides

    operator = params.get("operator") or ctx.operator or "system"
    if not workspace_connected(operator):
        return ToolResult(False, f"Google Workspace not connected for {operator} — connect in onboarding")
    presentation_id = params.get("presentation_id", "")
    if not presentation_id:
        return ToolResult(False, "presentation_id is required")
    result = slides.read_presentation(operator, presentation_id)
    ok = "error" not in result
    return ToolResult(ok, json.dumps(result))
