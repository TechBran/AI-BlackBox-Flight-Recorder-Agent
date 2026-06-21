"""Executor for delete_drive_file."""
import json

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    from Orchestrator.gmail.service import workspace_connected
    from Orchestrator.google_workspace import drive

    operator = params.get("operator") or ctx.operator or "system"
    if not workspace_connected(operator):
        return ToolResult(False, f"Google Workspace not connected for {operator} — connect in onboarding")
    file_id = params.get("file_id", "")
    if not file_id:
        return ToolResult(False, "file_id is required")
    result = drive.delete_drive_file(operator, file_id)
    ok = "error" not in result
    return ToolResult(ok, json.dumps(result))
