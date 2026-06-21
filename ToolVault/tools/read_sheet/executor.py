"""Executor for read_sheet."""
import json

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    from Orchestrator.gmail.service import workspace_connected
    from Orchestrator.google_workspace import sheets

    operator = params.get("operator") or ctx.operator or "system"
    if not workspace_connected(operator):
        return ToolResult(False, f"Google Workspace not connected for {operator} — connect in onboarding")
    spreadsheet_id = params.get("spreadsheet_id", "")
    if not spreadsheet_id:
        return ToolResult(False, "spreadsheet_id is required")
    result = sheets.read_sheet(operator, spreadsheet_id, params.get("range"))
    ok = "error" not in result
    return ToolResult(ok, json.dumps(result))
