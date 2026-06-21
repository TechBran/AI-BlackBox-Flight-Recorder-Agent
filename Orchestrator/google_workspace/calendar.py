"""Google Calendar helpers — per-operator create / list / update / delete events.

Each helper takes `(operator, ...)`, builds an authenticated Calendar v3 service
via `Orchestrator.gmail.service.get_calendar_service`, wraps the Google call in
try/except `googleapiclient.errors.HttpError`, and returns a plain dict (errors
under the "error" key) so the tool executors stay thin. The list helpers return
the response's bare `"items"` list.

Event times accept either an RFC3339 datetime (e.g. "2026-06-20T09:00:00Z") for a
timed event or a plain calendar date (e.g. "2026-06-20") for an all-day event.
The rule is intentionally simple — a value containing "T" is treated as a
dateTime, otherwise it's an all-day date — and is shared by create + update via
`_time_field`. The model passes RFC3339 strings.

A 403 (insufficient scopes) is special-cased into a customer-facing "reconnect"
message because it happens until the user re-consents to the Calendar scope.
"""

from googleapiclient.errors import HttpError

from Orchestrator.gmail.service import get_calendar_service


def _err(msg):
    return {"error": msg}


def _http_err(e):
    """Map an HttpError to a customer-facing error dict.

    403 = the operator's stored token predates the Calendar scope; the only fix
    is to re-run the Google sign-in so the new scope is granted.
    """
    status = getattr(e, "status_code", None)
    if status is None:
        resp = getattr(e, "resp", None)
        status = getattr(resp, "status", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if status == 403:
        return _err(
            "Google Workspace needs reconnect to grant Calendar access "
            "(re-run the Google sign-in in onboarding)"
        )
    reason = getattr(e, "reason", None) or str(e)
    return _err(f"Google Calendar API error: {reason} (status {status})")


def _time_field(value):
    """Translate a start/end value into a Calendar API time field.

    A value containing "T" is an RFC3339 datetime -> {"dateTime": value}; any
    other value is treated as an all-day calendar date -> {"date": value}.
    """
    if "T" in value:
        return {"dateTime": value}
    return {"date": value}


def create_event(operator, summary, start, end, calendar_id="primary",
                 description=None, attendees=None, location=None):
    """Create a calendar event and return the created event dict.

    `start`/`end` accept an RFC3339 datetime (timed event) or a plain date
    (all-day) — see `_time_field`. `attendees` is a list of email strings.
    description/location are included only when set. Returns the created event
    {id, htmlLink, status, summary, start, end} or {"error": ...}.
    """
    svc = get_calendar_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        body = {
            "summary": summary,
            "start": _time_field(start),
            "end": _time_field(end),
        }
        if description is not None:
            body["description"] = description
        if location is not None:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": e} for e in attendees]
        created = svc.events().insert(
            calendarId=calendar_id, body=body
        ).execute()
        return created
    except HttpError as e:
        return _http_err(e)


def list_events(operator, time_min, time_max, calendar_id="primary"):
    """List events in [time_min, time_max) for a calendar (RFC3339 bounds).

    Uses singleEvents=True + orderBy="startTime" so recurring events are
    expanded into individual instances in start order. Returns the list of event
    dicts (the response's "items") or {"error": ...}.
    """
    svc = get_calendar_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        resp = svc.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return resp.get("items", [])
    except HttpError as e:
        return _http_err(e)


def update_event(operator, event_id, calendar_id="primary", **fields):
    """Patch only the provided fields of an event and return the updated event.

    Recognised fields: summary, description, location (passed through when
    present) and start/end/attendees (translated the same way as create_event).
    Returns the updated event dict or {"error": ...}.
    """
    svc = get_calendar_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        body = {}
        for key in ("summary", "description", "location"):
            if key in fields and fields[key] is not None:
                body[key] = fields[key]
        for key in ("start", "end"):
            if key in fields and fields[key] is not None:
                body[key] = _time_field(fields[key])
        if fields.get("attendees"):
            body["attendees"] = [{"email": e} for e in fields["attendees"]]
        updated = svc.events().patch(
            calendarId=calendar_id, eventId=event_id, body=body
        ).execute()
        return updated
    except HttpError as e:
        return _http_err(e)


def delete_event(operator, event_id, calendar_id="primary"):
    """Delete an event by id. Returns {"deleted": True, "event_id": ...}."""
    svc = get_calendar_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        svc.events().delete(
            calendarId=calendar_id, eventId=event_id
        ).execute()
        return {"deleted": True, "event_id": event_id}
    except HttpError as e:
        return _http_err(e)


def list_calendars(operator):
    """List the operator's calendars.

    Returns a list of calendar dicts (the response's "items"; each with id,
    summary, primary, accessRole) or {"error": ...}.
    """
    svc = get_calendar_service(operator)
    if svc is None:
        return _err("Google Workspace not connected")
    try:
        resp = svc.calendarList().list().execute()
        return resp.get("items", [])
    except HttpError as e:
        return _http_err(e)
