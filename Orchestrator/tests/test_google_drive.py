"""Tests for the Google Drive helper module + the Drive tools.

Task 5 of the Google Workspace integration — mirrors test_google_slides.py.
These tests monkeypatch `Orchestrator.google_workspace.drive.get_drive_service`
with a fake `files()` resource so no live API call is made, and assert
the helpers:
  - search_drive_files calls files().list with the right q/pageSize/fields and
    returns the files list.
  - get_drive_file returns metadata; for a google-apps doc it exports text; for
    a plain-text binary it decodes the content; for a non-UTF8 binary it returns
    metadata + a note (no crash).
  - create_drive_file uploads media when content is given, metadata-only otherwise.
  - delete_drive_file calls files().delete and returns {"deleted": True, ...}.
  - return {"error": ...} when the service is unavailable.
And that the tool executors return the "not connected" message when
`workspace_connected` is False.
"""

import asyncio
import json

import pytest

from Orchestrator.google_workspace import drive
from Orchestrator.toolvault.context import ToolContext


# --- fake Google Drive service --------------------------------------------

CANNED_FILES = [
    {
        "id": "f1",
        "name": "Report.txt",
        "mimeType": "text/plain",
        "modifiedTime": "2026-06-20T00:00:00Z",
        "size": "12",
        "webViewLink": "https://drive.google.com/file/d/f1/view",
    },
    {
        "id": "f2",
        "name": "Plan",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-06-19T00:00:00Z",
        "webViewLink": "https://docs.google.com/document/d/f2/edit",
    },
]

# Metadata bodies keyed by file id (what files().get returns).
META = {
    "doc1": {
        "id": "doc1",
        "name": "My Doc",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-06-20T00:00:00Z",
        "webViewLink": "https://docs.google.com/document/d/doc1/edit",
    },
    "txt1": {
        "id": "txt1",
        "name": "notes.txt",
        "mimeType": "text/plain",
        "size": "5",
        "modifiedTime": "2026-06-20T00:00:00Z",
        "webViewLink": "https://drive.google.com/file/d/txt1/view",
    },
    "bin1": {
        "id": "bin1",
        "name": "photo.png",
        "mimeType": "image/png",
        "size": "4",
        "modifiedTime": "2026-06-20T00:00:00Z",
        "webViewLink": "https://drive.google.com/file/d/bin1/view",
    },
}


class FakeRequest:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeFiles:
    def __init__(self, recorder):
        self._recorder = recorder

    def list(self, q=None, pageSize=None, fields=None):
        self._recorder["list_q"] = q
        self._recorder["list_page_size"] = pageSize
        self._recorder["list_fields"] = fields
        return FakeRequest({"files": list(CANNED_FILES)})

    def get(self, fileId=None, fields=None):
        self._recorder.setdefault("get_ids", []).append(fileId)
        self._recorder["get_fields"] = fields
        return FakeRequest(dict(META[fileId]))

    def get_media(self, fileId=None):
        self._recorder["get_media_id"] = fileId
        return FakeRequest(self._recorder["media_bytes"])

    def export(self, fileId=None, mimeType=None):
        self._recorder["export_id"] = fileId
        self._recorder["export_mime"] = mimeType
        return FakeRequest(b"exported text")

    def create(self, body=None, media_body=None, fields=None):
        self._recorder["create_body"] = body
        self._recorder["create_media_body"] = media_body
        self._recorder["create_fields"] = fields
        return FakeRequest({
            "id": "newid",
            "name": (body or {}).get("name", ""),
            "mimeType": (body or {}).get("mimeType", ""),
            "webViewLink": "https://drive.google.com/file/d/newid/view",
        })

    def delete(self, fileId=None):
        self._recorder["delete_id"] = fileId
        return FakeRequest("")


class FakeService:
    def __init__(self, recorder):
        self._files = FakeFiles(recorder)

    def files(self):
        return self._files


@pytest.fixture
def recorder(monkeypatch):
    rec = {}
    monkeypatch.setattr(drive, "get_drive_service", lambda operator: FakeService(rec))
    return rec


# --- search_drive_files ----------------------------------------------------

def test_search_drive_files_calls_list_with_q_and_returns_files(recorder):
    result = drive.search_drive_files("op", query="name contains 'Report'", page_size=10)
    assert recorder["list_q"] == "name contains 'Report'"
    assert recorder["list_page_size"] == 10
    assert recorder["list_fields"] == (
        "files(id,name,mimeType,modifiedTime,size,webViewLink)"
    )
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["id"] == "f1"


def test_search_drive_files_default_page_size(recorder):
    drive.search_drive_files("op")
    assert recorder["list_q"] is None
    assert recorder["list_page_size"] == 20


# --- get_drive_file --------------------------------------------------------

def test_get_drive_file_google_doc_exports_text(recorder):
    result = drive.get_drive_file("op", "doc1")
    assert "error" not in result
    # metadata always present
    assert result["id"] == "doc1"
    assert result["name"] == "My Doc"
    # google-apps doc -> export with a text mime type
    assert recorder["export_id"] == "doc1"
    assert recorder["export_mime"] == "text/plain"
    assert result["content"] == "exported text"
    # metadata get used the right fields list
    assert recorder["get_fields"] == "id,name,mimeType,size,modifiedTime,webViewLink"


def test_get_drive_file_plain_text_binary_decodes(recorder):
    recorder["media_bytes"] = b"hello"
    result = drive.get_drive_file("op", "txt1")
    assert "error" not in result
    assert result["id"] == "txt1"
    assert recorder["get_media_id"] == "txt1"
    assert result["content"] == "hello"


def test_get_drive_file_non_utf8_binary_returns_note(recorder):
    # 0xff 0xfe is not valid UTF-8 -> must NOT crash, returns metadata + note.
    recorder["media_bytes"] = b"\xff\xfe\x00\x01"
    result = drive.get_drive_file("op", "bin1")
    assert "error" not in result
    assert result["id"] == "bin1"
    assert "content" not in result
    assert "note" in result


# --- create_drive_file -----------------------------------------------------

def test_create_drive_file_with_content_uploads_media(recorder):
    result = drive.create_drive_file(
        "op", "new.txt", "text/plain", content="payload"
    )
    assert "error" not in result
    assert result["id"] == "newid"
    assert recorder["create_body"] == {"name": "new.txt", "mimeType": "text/plain"}
    assert recorder["create_media_body"] is not None
    assert recorder["create_fields"] == "id,name,mimeType,webViewLink"


def test_create_drive_file_without_content_metadata_only(recorder):
    result = drive.create_drive_file("op", "folderish", "text/plain")
    assert "error" not in result
    assert result["id"] == "newid"
    assert recorder["create_body"] == {"name": "folderish", "mimeType": "text/plain"}
    assert recorder["create_media_body"] is None


# --- delete_drive_file -----------------------------------------------------

def test_delete_drive_file_returns_deleted(recorder):
    result = drive.delete_drive_file("op", "f1")
    assert recorder["delete_id"] == "f1"
    assert result == {"deleted": True, "file_id": "f1"}


# --- service None ----------------------------------------------------------

def test_search_drive_files_service_none(monkeypatch):
    monkeypatch.setattr(drive, "get_drive_service", lambda operator: None)
    result = drive.search_drive_files("op")
    assert isinstance(result, dict)
    assert "error" in result


def test_get_drive_file_service_none(monkeypatch):
    monkeypatch.setattr(drive, "get_drive_service", lambda operator: None)
    result = drive.get_drive_file("op", "f1")
    assert "error" in result


def test_create_drive_file_service_none(monkeypatch):
    monkeypatch.setattr(drive, "get_drive_service", lambda operator: None)
    result = drive.create_drive_file("op", "x", "text/plain")
    assert "error" in result


def test_delete_drive_file_service_none(monkeypatch):
    monkeypatch.setattr(drive, "get_drive_service", lambda operator: None)
    result = drive.delete_drive_file("op", "f1")
    assert "error" in result


# --- HttpError handling ----------------------------------------------------

def test_search_drive_files_403_message_says_reconnect(monkeypatch):
    from googleapiclient.errors import HttpError

    class FakeResp:
        status = 403
        reason = "insufficientPermissions"

    def boom(*a, **k):
        raise HttpError(FakeResp(), b"forbidden")

    class BoomFiles:
        def list(self, q=None, pageSize=None, fields=None):
            boom()

    class BoomService:
        def files(self):
            return BoomFiles()

    monkeypatch.setattr(drive, "get_drive_service", lambda operator: BoomService())
    result = drive.search_drive_files("op")
    assert isinstance(result, dict)
    assert "error" in result
    assert "reconnect" in result["error"].lower()
    assert "drive" in result["error"].lower()


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


def test_search_drive_files_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("search_drive_files")
    res = _run(mod.execute({"operator": "op"}, ToolContext(operator="op")))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_get_drive_file_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("get_drive_file")
    res = _run(mod.execute({"file_id": "f1", "operator": "op"}, ToolContext(operator="op")))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_create_drive_file_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("create_drive_file")
    res = _run(mod.execute(
        {"name": "x", "mime_type": "text/plain", "operator": "op"},
        ToolContext(operator="op"),
    ))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_delete_drive_file_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("delete_drive_file")
    res = _run(mod.execute({"file_id": "f1", "operator": "op"}, ToolContext(operator="op")))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_search_drive_files_executor_success(monkeypatch, recorder):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: True)
    mod = _load_executor("search_drive_files")
    res = _run(mod.execute({"query": "name contains 'x'", "operator": "op"}, ToolContext(operator="op")))
    assert res.success is True
    payload = json.loads(res.result)
    assert isinstance(payload, list)
    assert payload[0]["id"] == "f1"
