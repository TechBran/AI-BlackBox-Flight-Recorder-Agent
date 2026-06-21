"""Executor for update_sheet_values."""
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
    range_a1 = params.get("range", "")
    if not range_a1:
        return ToolResult(False, "range is required")
    values = params.get("values")
    if not isinstance(values, list):
        return ToolResult(False, "values must be a list of rows (a 2D array)")
    result = sheets.update_sheet_values(operator, spreadsheet_id, range_a1, values)
    ok = "error" not in result
    return ToolResult(ok, json.dumps(result))
