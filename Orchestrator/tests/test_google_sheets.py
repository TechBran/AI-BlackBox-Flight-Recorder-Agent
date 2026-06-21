"""Tests for the Google Sheets helper module + the spreadsheet tools.

Task 3 of the Google Workspace integration — mirrors test_google_docs.py.
These tests monkeypatch `Orchestrator.google_workspace.sheets.get_sheets_service`
with a fake `spreadsheets()` resource so no live API call is made, and assert
the helpers:
  - create_spreadsheet returns spreadsheet_id + url.
  - read_sheet WITH a range returns the cell values.
  - read_sheet WITHOUT a range returns the sheet metadata (titles + grid dims)
    so the model knows what ranges exist.
  - update_sheet_values calls values().update with USER_ENTERED + the values.
  - sheets_batch_update forwards the `requests` array VERBATIM into the API body.
  - return {"error": ...} when the service is unavailable.
And that the tool executors return the "not connected" message when
`workspace_connected` is False.
"""

import asyncio
import json

import pytest

from Orchestrator.google_workspace import sheets
from Orchestrator.toolvault.context import ToolContext


# --- fake Google Sheets service -------------------------------------------

# A canned spreadsheets().get() body: two sheets with grid dimensions so the
# metadata path can surface titles + rows/cols + sheet_id.
CANNED_META = {
    "spreadsheetId": "S1",
    "properties": {"title": "Canned Sheet"},
    "sheets": [
        {
            "properties": {
                "sheetId": 0,
                "title": "Sheet1",
                "gridProperties": {"rowCount": 100, "columnCount": 26},
            }
        },
        {
            "properties": {
                "sheetId": 7,
                "title": "Data",
                "gridProperties": {"rowCount": 500, "columnCount": 12},
            }
        },
    ],
}

# A canned values().get() response.
CANNED_VALUES = {
    "range": "Sheet1!A1:B2",
    "majorDimension": "ROWS",
    "values": [["a", "b"], ["c", "d"]],
}


class FakeRequest:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeValues:
    def __init__(self, recorder):
        self._recorder = recorder

    def get(self, spreadsheetId=None, range=None):
        self._recorder["values_get"] = {"id": spreadsheetId, "range": range}
        return FakeRequest(CANNED_VALUES)

    def update(self, spreadsheetId=None, range=None, valueInputOption=None, body=None):
        self._recorder["values_update"] = {
            "id": spreadsheetId,
            "range": range,
            "valueInputOption": valueInputOption,
            "body": body,
        }
        return FakeRequest({
            "spreadsheetId": spreadsheetId,
            "updatedRange": range,
            "updatedCells": 4,
        })


class FakeSpreadsheets:
    def __init__(self, recorder):
        self._recorder = recorder
        self._values = FakeValues(recorder)

    def create(self, body=None):
        self._recorder["create_body"] = body
        return FakeRequest({
            "spreadsheetId": "S1",
            "properties": (body or {}).get("properties", {}),
        })

    def get(self, spreadsheetId=None):
        self._recorder["get_id"] = spreadsheetId
        return FakeRequest(CANNED_META)

    def batchUpdate(self, spreadsheetId=None, body=None):
        self._recorder["batch_id"] = spreadsheetId
        self._recorder.setdefault("batch_bodies", []).append(body)
        return FakeRequest({"spreadsheetId": spreadsheetId, "replies": []})

    def values(self):
        return self._values


class FakeService:
    def __init__(self, recorder):
        self._spreadsheets = FakeSpreadsheets(recorder)

    def spreadsheets(self):
        return self._spreadsheets


@pytest.fixture
def recorder(monkeypatch):
    rec = {}
    monkeypatch.setattr(sheets, "get_sheets_service", lambda operator: FakeService(rec))
    return rec


# --- create_spreadsheet ---------------------------------------------------

def test_create_spreadsheet_returns_id_and_url(recorder):
    result = sheets.create_spreadsheet("op", "My Sheet")
    assert result["spreadsheet_id"] == "S1"
    assert result["title"] == "My Sheet"
    assert result["url"] == "https://docs.google.com/spreadsheets/d/S1/edit"
    assert recorder["create_body"] == {"properties": {"title": "My Sheet"}}


# --- read_sheet -----------------------------------------------------------

def test_read_sheet_with_range_returns_values(recorder):
    result = sheets.read_sheet("op", "S1", range="Sheet1!A1:B2")
    assert recorder["values_get"] == {"id": "S1", "range": "Sheet1!A1:B2"}
    assert "error" not in result
    assert result["range"] == "Sheet1!A1:B2"
    assert result["values"] == [["a", "b"], ["c", "d"]]


def test_read_sheet_without_range_returns_metadata(recorder):
    result = sheets.read_sheet("op", "S1")
    assert recorder["get_id"] == "S1"
    assert "error" not in result
    sheets_meta = result["sheets"]
    assert isinstance(sheets_meta, list) and len(sheets_meta) == 2
    first = sheets_meta[0]
    assert first["title"] == "Sheet1"
    assert first["rows"] == 100
    assert first["cols"] == 26
    assert first["sheet_id"] == 0
    second = sheets_meta[1]
    assert second["title"] == "Data"
    assert second["sheet_id"] == 7


# --- update_sheet_values --------------------------------------------------

def test_update_sheet_values_uses_user_entered_and_values(recorder):
    values = [["x", "y"], ["1", "2"]]
    result = sheets.update_sheet_values("op", "S1", "Sheet1!A1:B2", values)
    assert "error" not in result
    upd = recorder["values_update"]
    assert upd["id"] == "S1"
    assert upd["range"] == "Sheet1!A1:B2"
    assert upd["valueInputOption"] == "USER_ENTERED"
    assert upd["body"] == {"values": values}
    assert result["updated_range"] == "Sheet1!A1:B2"
    assert result["updated_cells"] == 4


# --- sheets_batch_update --------------------------------------------------

def test_sheets_batch_update_forwards_requests_verbatim(recorder):
    requests = [
        {"addSheet": {"properties": {"title": "New"}}},
        {"repeatCell": {
            "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold",
        }},
    ]
    result = sheets.sheets_batch_update("op", "S1", requests)
    assert "error" not in result
    assert recorder["batch_id"] == "S1"
    body = recorder["batch_bodies"][-1]
    # Same list object, forwarded verbatim under "requests".
    assert body["requests"] is requests
    assert result["spreadsheet_id"] == "S1"
    assert result["applied"] == 2


# --- service None ---------------------------------------------------------

def test_create_spreadsheet_service_none(monkeypatch):
    monkeypatch.setattr(sheets, "get_sheets_service", lambda operator: None)
    result = sheets.create_spreadsheet("op", "X")
    assert "error" in result


def test_read_sheet_service_none(monkeypatch):
    monkeypatch.setattr(sheets, "get_sheets_service", lambda operator: None)
    result = sheets.read_sheet("op", "S1")
    assert "error" in result


def test_update_sheet_values_service_none(monkeypatch):
    monkeypatch.setattr(sheets, "get_sheets_service", lambda operator: None)
    result = sheets.update_sheet_values("op", "S1", "A1", [["x"]])
    assert "error" in result


def test_sheets_batch_update_service_none(monkeypatch):
    monkeypatch.setattr(sheets, "get_sheets_service", lambda operator: None)
    result = sheets.sheets_batch_update("op", "S1", [])
    assert "error" in result


# --- HttpError handling ---------------------------------------------------

def test_create_spreadsheet_403_message_says_reconnect(monkeypatch):
    from googleapiclient.errors import HttpError

    class FakeResp:
        status = 403
        reason = "insufficientPermissions"

    def boom(*a, **k):
        raise HttpError(FakeResp(), b"forbidden")

    class BoomSpreadsheets:
        def create(self, body=None):
            boom()

    class BoomService:
        def spreadsheets(self):
            return BoomSpreadsheets()

    monkeypatch.setattr(sheets, "get_sheets_service", lambda operator: BoomService())
    result = sheets.create_spreadsheet("op", "X")
    assert "error" in result
    assert "reconnect" in result["error"].lower()


# --- tool executors -------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


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


def test_create_spreadsheet_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("create_spreadsheet")
    res = _run(mod.execute({"title": "X", "operator": "op"}, ToolContext(operator="op")))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_read_sheet_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("read_sheet")
    res = _run(mod.execute({"spreadsheet_id": "S1", "operator": "op"}, ToolContext(operator="op")))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_update_sheet_values_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("update_sheet_values")
    res = _run(mod.execute(
        {"spreadsheet_id": "S1", "range": "A1", "values": [["x"]], "operator": "op"},
        ToolContext(operator="op"),
    ))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_sheets_batch_update_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("sheets_batch_update")
    res = _run(mod.execute(
        {"spreadsheet_id": "S1", "requests": [], "operator": "op"},
        ToolContext(operator="op"),
    ))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_sheets_batch_update_executor_requires_list(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: True)
    mod = _load_executor("sheets_batch_update")
    res = _run(mod.execute(
        {"spreadsheet_id": "S1", "requests": "notalist", "operator": "op"},
        ToolContext(operator="op"),
    ))
    assert res.success is False
    assert "requests must be a list" in res.result.lower()


def test_update_sheet_values_executor_requires_values_list(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: True)
    mod = _load_executor("update_sheet_values")
    res = _run(mod.execute(
        {"spreadsheet_id": "S1", "range": "A1", "values": "notalist", "operator": "op"},
        ToolContext(operator="op"),
    ))
    assert res.success is False
    assert "values must be a list" in res.result.lower()


def test_create_spreadsheet_executor_success(monkeypatch, recorder):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: True)
    mod = _load_executor("create_spreadsheet")
    res = _run(mod.execute({"title": "Sheet!", "operator": "op"}, ToolContext(operator="op")))
    assert res.success is True
    payload = json.loads(res.result)
    assert payload["spreadsheet_id"] == "S1"
