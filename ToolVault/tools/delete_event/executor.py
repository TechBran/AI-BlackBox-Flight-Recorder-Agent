"""Executor for delete_event."""
import json

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    from Orchestrator.gmail.service import workspace_connected
    from Orchestrator.google_workspace import calendar

    operator = params.get("operator") or ctx.operator or "system"
    if not workspace_connected(operator):
        return ToolResult(False, f"Google Workspace not connected for {operator} — connect in onboarding")
    event_id = params.get("event_id", "")
    if not event_id:
        return ToolResult(False, "event_id is required")
    calendar_id = params.get("calendar_id", "primary")
    result = calendar.delete_event(operator, event_id, calendar_id=calendar_id)
    ok = "error" not in result
    return ToolResult(ok, json.dumps(result))
