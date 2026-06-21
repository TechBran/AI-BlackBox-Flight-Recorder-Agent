"""Executor for create_event."""
import json

from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    from Orchestrator.gmail.service import workspace_connected
    from Orchestrator.google_workspace import calendar

    operator = params.get("operator") or ctx.operator or "system"
    if not workspace_connected(operator):
        return ToolResult(False, f"Google Workspace not connected for {operator} — connect in onboarding")
    summary = params.get("summary", "")
    if not summary:
        return ToolResult(False, "summary is required")
    start = params.get("start", "")
    if not start:
        return ToolResult(False, "start is required")
    end = params.get("end", "")
    if not end:
        return ToolResult(False, "end is required")
    calendar_id = params.get("calendar_id", "primary")
    description = params.get("description")
    location = params.get("location")
    attendees = params.get("attendees")
    result = calendar.create_event(
        operator,
        summary,
        start,
        end,
        calendar_id=calendar_id,
        description=description,
        attendees=attendees,
        location=location,
    )
    ok = "error" not in result
    return ToolResult(ok, json.dumps(result))
