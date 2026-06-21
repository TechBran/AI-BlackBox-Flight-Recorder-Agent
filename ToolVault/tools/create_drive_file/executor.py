"""Executor for create_drive_file."""
import json

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    from Orchestrator.gmail.service import workspace_connected
    from Orchestrator.google_workspace import drive

    operator = params.get("operator") or ctx.operator or "system"
    if not workspace_connected(operator):
        return ToolResult(False, f"Google Workspace not connected for {operator} — connect in onboarding")
    name = params.get("name", "")
    if not name:
        return ToolResult(False, "name is required")
    mime_type = params.get("mime_type", "")
    if not mime_type:
        return ToolResult(False, "mime_type is required")
    content = params.get("content")
    result = drive.create_drive_file(operator, name, mime_type, content=content)
    ok = "error" not in result
    return ToolResult(ok, json.dumps(result))
