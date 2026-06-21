"""Executor for list_events."""
import json

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    from Orchestrator.gmail.service import workspace_connected
    from Orchestrator.google_workspace import calendar

    operator = params.get("operator") or ctx.operator or "system"
    if not workspace_connected(operator):
        return ToolResult(False, f"Google Workspace not connected for {operator} — connect in onboarding")
    time_min = params.get("time_min", "")
    if not time_min:
        return ToolResult(False, "time_min is required")
    time_max = params.get("time_max", "")
    if not time_max:
        return ToolResult(False, "time_max is required")
    calendar_id = params.get("calendar_id", "primary")
    result = calendar.list_events(operator, time_min, time_max, calendar_id=calendar_id)
    ok = not (isinstance(result, dict) and "error" in result)
    return ToolResult(ok, json.dumps(result))
