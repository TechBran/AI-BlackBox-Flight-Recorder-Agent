"""Executor for list_events."""
import json

from Orchestrator.toolvault.context import ToolContext, ToolResult

# M4: Google protocol plumbing that costs tokens and carries no meaning for a reader.
#
# This is a DENY-list, deliberately not a whitelist: every human-meaningful field
# Google returns (location, description, attendees, organizer, created/updated,
# reminders, colorId, conferenceData, hangoutLink, attachments, visibility, ...)
# passes through untouched, so no future question about an event can be broken by
# this trim. The four below are inert here by inspection:
#   kind     - the constant "calendar#event"
#   etag     - an opaque concurrency token; update_event uses events().patch by id
#              and never sends If-Match, so nothing in this system reads it
#   iCalUID  - iCal interop id; no tool accepts it (update/delete take the event id)
#   sequence - iCal revision counter, same story
# They are also the worst per-token offenders: etag/iCalUID are high-entropy random
# strings a tokenizer shreds into noise, once per event, on a list a morning-briefing
# prompt fetches for a whole day.
_NOISE_KEYS = ("kind", "etag", "iCalUID", "sequence")


def _slim(event):
    """Drop protocol-only keys from one event dict; anything else passes through."""
    if not isinstance(event, dict):
        return event
    return {k: v for k, v in event.items() if k not in _NOISE_KEYS}


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
    # Trim only on the success path — an error dict goes back verbatim.
    if ok and isinstance(result, list):
        result = [_slim(e) for e in result]
    return ToolResult(ok, json.dumps(result))
