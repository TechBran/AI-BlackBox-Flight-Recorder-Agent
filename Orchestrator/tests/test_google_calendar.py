"""Tests for the Google Calendar helper module + the Calendar tools.

Task 6 of the Google Workspace integration — mirrors test_google_drive.py.
These tests monkeypatch `Orchestrator.google_workspace.calendar.get_calendar_service`
with a fake `events()` / `calendarList()` resource so no live API call is made,
and assert the helpers:
  - create_event builds the event body (summary; start/end as {dateTime} for an
    RFC3339 value containing "T" else {date}; attendees -> [{"email": ...}];
    description/location only when set) and returns the created event.
  - list_events calls events().list with singleEvents=True, orderBy="startTime",
    the time window + calendar_id and returns the items list (incl. the empty case).
  - update_event calls events().patch with only the provided fields (start/end
    translated the same way) and returns the updated event.
  - delete_event calls events().delete and returns {"deleted": True, "event_id": ...}.
  - list_calendars returns the items list.
  - return {"error": ...} when the service is unavailable.
And that the tool executors return the "not connected" message when
`workspace_connected` is False, plus the bare-list `success=True` contract
(including the empty-list case) for list_events / list_calendars.
"""

import asyncio
import json

import pytest

from Orchestrator.google_workspace import calendar
from Orchestrator.toolvault.context import ToolContext


# --- fake Google Calendar service -----------------------------------------

CANNED_EVENTS = [
    {
        "id": "e1",
        "status": "confirmed",
        "summary": "Standup",
        "htmlLink": "https://calendar.google.com/event?eid=e1",
        "start": {"dateTime": "2026-06-20T09:00:00Z"},
        "end": {"dateTime": "2026-06-20T09:30:00Z"},
    },
    {
        "id": "e2",
        "status": "confirmed",
        "summary": "Holiday",
        "htmlLink": "https://calendar.google.com/event?eid=e2",
        "start": {"date": "2026-07-04"},
        "end": {"date": "2026-07-05"},
    },
]

CANNED_CALENDARS = [
    {"id": "primary", "summary": "Me", "primary": True, "accessRole": "owner"},
    {"id": "team@x.com", "summary": "Team", "accessRole": "reader"},
]


class FakeRequest:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeEvents:
    def __init__(self, recorder):
        self._recorder = recorder

    def insert(self, calendarId=None, body=None):
        self._recorder["insert_calendar_id"] = calendarId
        self._recorder["insert_body"] = body
        created = {
            "id": "newevent",
            "status": "confirmed",
            "htmlLink": "https://calendar.google.com/event?eid=newevent",
        }
        created.update(body or {})
        return FakeRequest(created)

    def list(self, calendarId=None, timeMin=None, timeMax=None,
             singleEvents=None, orderBy=None):
        self._recorder["list_calendar_id"] = calendarId
        self._recorder["list_time_min"] = timeMin
        self._recorder["list_time_max"] = timeMax
        self._recorder["list_single_events"] = singleEvents
        self._recorder["list_order_by"] = orderBy
        items = [] if self._recorder.get("empty_list") else list(CANNED_EVENTS)
        return FakeRequest({"items": items})

    def patch(self, calendarId=None, eventId=None, body=None):
        self._recorder["patch_calendar_id"] = calendarId
        self._recorder["patch_event_id"] = eventId
        self._recorder["patch_body"] = body
        updated = {"id": eventId, "status": "confirmed"}
        updated.update(body or {})
        return FakeRequest(updated)

    def delete(self, calendarId=None, eventId=None):
        self._recorder["delete_calendar_id"] = calendarId
        self._recorder["delete_event_id"] = eventId
        return FakeRequest("")


class FakeCalendarList:
    def __init__(self, recorder):
        self._recorder = recorder

    def list(self):
        self._recorder["calendar_list_called"] = True
        items = [] if self._recorder.get("empty_calendars") else list(CANNED_CALENDARS)
        return FakeRequest({"items": items})


class FakeService:
    def __init__(self, recorder):
        self._events = FakeEvents(recorder)
        self._calendar_list = FakeCalendarList(recorder)

    def events(self):
        return self._events

    def calendarList(self):
        return self._calendar_list


@pytest.fixture
def recorder(monkeypatch):
    rec = {}
    monkeypatch.setattr(
        calendar, "get_calendar_service", lambda operator: FakeService(rec)
    )
    return rec


# --- create_event ----------------------------------------------------------

def test_create_event_builds_datetime_body_and_returns_event(recorder):
    result = calendar.create_event(
        "op",
        "Lunch",
        "2026-06-20T12:00:00Z",
        "2026-06-20T13:00:00Z",
    )
    assert "error" not in result
    assert recorder["insert_calendar_id"] == "primary"
    body = recorder["insert_body"]
    assert body["summary"] == "Lunch"
    # RFC3339 value (contains "T") -> {dateTime}
    assert body["start"] == {"dateTime": "2026-06-20T12:00:00Z"}
    assert body["end"] == {"dateTime": "2026-06-20T13:00:00Z"}
    # optional fields omitted when not set
    assert "description" not in body
    assert "location" not in body
    assert "attendees" not in body
    # returns the created event
    assert result["id"] == "newevent"


def test_create_event_all_day_uses_date(recorder):
    result = calendar.create_event(
        "op", "Holiday", "2026-07-04", "2026-07-05"
    )
    assert "error" not in result
    body = recorder["insert_body"]
    # no "T" -> all-day {date}
    assert body["start"] == {"date": "2026-07-04"}
    assert body["end"] == {"date": "2026-07-05"}


def test_create_event_attendees_and_optional_fields(recorder):
    result = calendar.create_event(
        "op",
        "Sync",
        "2026-06-20T15:00:00Z",
        "2026-06-20T16:00:00Z",
        calendar_id="team@x.com",
        description="Quarterly sync",
        location="Room 4",
        attendees=["a@x.com", "b@x.com"],
    )
    assert "error" not in result
    assert recorder["insert_calendar_id"] == "team@x.com"
    body = recorder["insert_body"]
    assert body["description"] == "Quarterly sync"
    assert body["location"] == "Room 4"
    assert body["attendees"] == [{"email": "a@x.com"}, {"email": "b@x.com"}]


# --- list_events -----------------------------------------------------------

def test_list_events_calls_list_with_window_and_returns_items(recorder):
    result = calendar.list_events(
        "op", "2026-06-20T00:00:00Z", "2026-06-21T00:00:00Z"
    )
    assert recorder["list_calendar_id"] == "primary"
    assert recorder["list_time_min"] == "2026-06-20T00:00:00Z"
    assert recorder["list_time_max"] == "2026-06-21T00:00:00Z"
    assert recorder["list_single_events"] is True
    assert recorder["list_order_by"] == "startTime"
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["id"] == "e1"


def test_list_events_custom_calendar_id(recorder):
    calendar.list_events(
        "op", "2026-06-20T00:00:00Z", "2026-06-21T00:00:00Z",
        calendar_id="team@x.com",
    )
    assert recorder["list_calendar_id"] == "team@x.com"


def test_list_events_empty_returns_empty_list(recorder):
    recorder["empty_list"] = True
    result = calendar.list_events(
        "op", "2026-06-20T00:00:00Z", "2026-06-21T00:00:00Z"
    )
    assert result == []


# --- update_event ----------------------------------------------------------

def test_update_event_patches_only_provided_fields(recorder):
    result = calendar.update_event(
        "op", "e1", summary="Renamed", location="Room 9"
    )
    assert "error" not in result
    assert recorder["patch_calendar_id"] == "primary"
    assert recorder["patch_event_id"] == "e1"
    body = recorder["patch_body"]
    assert body == {"summary": "Renamed", "location": "Room 9"}
    assert result["id"] == "e1"


def test_update_event_translates_start_end(recorder):
    calendar.update_event(
        "op", "e1",
        start="2026-06-21T10:00:00Z",
        end="2026-06-22",
    )
    body = recorder["patch_body"]
    assert body["start"] == {"dateTime": "2026-06-21T10:00:00Z"}
    # no "T" -> all-day date
    assert body["end"] == {"date": "2026-06-22"}


def test_update_event_translates_attendees(recorder):
    calendar.update_event("op", "e1", attendees=["c@x.com"])
    body = recorder["patch_body"]
    assert body["attendees"] == [{"email": "c@x.com"}]


# --- delete_event ----------------------------------------------------------

def test_delete_event_returns_deleted(recorder):
    result = calendar.delete_event("op", "e1")
    assert recorder["delete_calendar_id"] == "primary"
    assert recorder["delete_event_id"] == "e1"
    assert result == {"deleted": True, "event_id": "e1"}


# --- list_calendars --------------------------------------------------------

def test_list_calendars_returns_items(recorder):
    result = calendar.list_calendars("op")
    assert recorder["calendar_list_called"] is True
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["id"] == "primary"


# --- service None ----------------------------------------------------------

def test_create_event_service_none(monkeypatch):
    monkeypatch.setattr(calendar, "get_calendar_service", lambda operator: None)
    result = calendar.create_event("op", "x", "2026-06-20T12:00:00Z", "2026-06-20T13:00:00Z")
    assert isinstance(result, dict)
    assert "error" in result


def test_list_events_service_none(monkeypatch):
    monkeypatch.setattr(calendar, "get_calendar_service", lambda operator: None)
    result = calendar.list_events("op", "a", "b")
    assert isinstance(result, dict)
    assert "error" in result


def test_update_event_service_none(monkeypatch):
    monkeypatch.setattr(calendar, "get_calendar_service", lambda operator: None)
    result = calendar.update_event("op", "e1", summary="x")
    assert "error" in result


def test_delete_event_service_none(monkeypatch):
    monkeypatch.setattr(calendar, "get_calendar_service", lambda operator: None)
    result = calendar.delete_event("op", "e1")
    assert "error" in result


def test_list_calendars_service_none(monkeypatch):
    monkeypatch.setattr(calendar, "get_calendar_service", lambda operator: None)
    result = calendar.list_calendars("op")
    assert isinstance(result, dict)
    assert "error" in result


# --- HttpError handling ----------------------------------------------------

def test_create_event_403_message_says_reconnect_and_calendar(monkeypatch):
    from googleapiclient.errors import HttpError

    class FakeResp:
        status = 403
        reason = "insufficientPermissions"

    def boom(*a, **k):
        raise HttpError(FakeResp(), b"forbidden")

    class BoomEvents:
        def insert(self, calendarId=None, body=None):
            boom()

    class BoomService:
        def events(self):
            return BoomEvents()

    monkeypatch.setattr(
        calendar, "get_calendar_service", lambda operator: BoomService()
    )
    result = calendar.create_event(
        "op", "x", "2026-06-20T12:00:00Z", "2026-06-20T13:00:00Z"
    )
    assert isinstance(result, dict)
    assert "error" in result
    assert "reconnect" in result["error"].lower()
    assert "calendar" in result["error"].lower()


# --- tool executors --------------------------------------------------------

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _load_executor(tool_name):
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "ToolVault" / "tools" / tool_name / "executor.py"
    )
    spec = importlib.util.spec_from_file_location(f"_exec_{tool_name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_create_event_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("create_event")
    res = _run(mod.execute(
        {
            "summary": "x",
            "start": "2026-06-20T12:00:00Z",
            "end": "2026-06-20T13:00:00Z",
            "operator": "op",
        },
        ToolContext(operator="op"),
    ))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_list_events_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("list_events")
    res = _run(mod.execute(
        {"time_min": "a", "time_max": "b", "operator": "op"},
        ToolContext(operator="op"),
    ))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_update_event_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("update_event")
    res = _run(mod.execute(
        {"event_id": "e1", "summary": "x", "operator": "op"},
        ToolContext(operator="op"),
    ))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_delete_event_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("delete_event")
    res = _run(mod.execute(
        {"event_id": "e1", "operator": "op"},
        ToolContext(operator="op"),
    ))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_list_calendars_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("list_calendars")
    res = _run(mod.execute({"operator": "op"}, ToolContext(operator="op")))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_list_events_executor_success(monkeypatch, recorder):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: True)
    mod = _load_executor("list_events")
    res = _run(mod.execute(
        {
            "time_min": "2026-06-20T00:00:00Z",
            "time_max": "2026-06-21T00:00:00Z",
            "operator": "op",
        },
        ToolContext(operator="op"),
    ))
    assert res.success is True
    payload = json.loads(res.result)
    assert isinstance(payload, list)
    assert payload[0]["id"] == "e1"


def test_list_events_executor_empty_list_is_success(monkeypatch, recorder):
    # An empty result list is a SUCCESS, not a failure. This locks the bare-list
    # executor contract (ok = not (isinstance(result, dict) and "error" in result))
    # against a future refactor back to the sibling `"error" not in result`, which
    # would misread an empty list.
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: True)
    recorder["empty_list"] = True
    mod = _load_executor("list_events")
    res = _run(mod.execute(
        {
            "time_min": "2026-06-20T00:00:00Z",
            "time_max": "2026-06-21T00:00:00Z",
            "operator": "op",
        },
        ToolContext(operator="op"),
    ))
    assert res.success is True
    assert json.loads(res.result) == []


def test_list_calendars_executor_success(monkeypatch, recorder):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: True)
    mod = _load_executor("list_calendars")
    res = _run(mod.execute({"operator": "op"}, ToolContext(operator="op")))
    assert res.success is True
    payload = json.loads(res.result)
    assert isinstance(payload, list)
    assert payload[0]["id"] == "primary"


def test_list_calendars_executor_empty_list_is_success(monkeypatch, recorder):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: True)
    recorder["empty_calendars"] = True
    mod = _load_executor("list_calendars")
    res = _run(mod.execute({"operator": "op"}, ToolContext(operator="op")))
    assert res.success is True
    assert json.loads(res.result) == []
