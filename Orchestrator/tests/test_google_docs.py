"""Tests for the Google Docs helper module + the create_doc/read_doc/docs_batch_update tools.

Task 2 of the Google Workspace integration. These tests monkeypatch
`Orchestrator.google_workspace.docs.get_docs_service` with a fake `documents()`
resource so no live API call is made, and assert the helpers:
  - create_doc returns document_id + url; inserts text via batchUpdate when given.
  - read_doc returns plain text + per-element index info for targeting edits.
  - docs_batch_update forwards the `requests` array VERBATIM into the API body.
  - return {"error": ...} when the service is unavailable.
And that the tool executors return the "not connected" message when
`workspace_connected` is False.
"""

import asyncio
import json

import pytest

from Orchestrator.google_workspace import docs
from Orchestrator.toolvault.context import ToolContext


# --- fake Google Docs service ---------------------------------------------

# A canned documents().get() body: one paragraph "Hello world\n" and one
# heading paragraph, exercising both startIndex/endIndex and a textRun.
CANNED_DOC = {
    "documentId": "D1",
    "title": "Canned Doc",
    "body": {
        "content": [
            {
                "startIndex": 1,
                "endIndex": 13,
                "paragraph": {
                    "elements": [
                        {
                            "startIndex": 1,
                            "endIndex": 13,
                            "textRun": {"content": "Hello world\n"},
                        }
                    ]
                },
            },
            {
                "startIndex": 13,
                "endIndex": 20,
                "paragraph": {
                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                    "elements": [
                        {
                            "startIndex": 13,
                            "endIndex": 20,
                            "textRun": {"content": "Title\n"},
                        }
                    ],
                },
            },
        ]
    },
    "inlineObjects": {"kix.abc123": {}},
}


class FakeRequest:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeDocuments:
    def __init__(self, recorder):
        self._recorder = recorder

    def create(self, body=None):
        self._recorder["create_body"] = body
        return FakeRequest({"documentId": "D1"})

    def get(self, documentId=None):
        self._recorder["get_id"] = documentId
        return FakeRequest(CANNED_DOC)

    def batchUpdate(self, documentId=None, body=None):
        self._recorder["batch_id"] = documentId
        self._recorder.setdefault("batch_bodies", []).append(body)
        return FakeRequest({"documentId": documentId, "replies": []})


class FakeService:
    def __init__(self, recorder):
        self._documents = FakeDocuments(recorder)

    def documents(self):
        return self._documents


@pytest.fixture
def recorder(monkeypatch):
    rec = {}
    monkeypatch.setattr(docs, "get_docs_service", lambda operator: FakeService(rec))
    return rec


# --- create_doc -----------------------------------------------------------

def test_create_doc_returns_id_and_url(recorder):
    result = docs.create_doc("op", "My Doc")
    assert result["document_id"] == "D1"
    assert result["title"] == "My Doc"
    assert result["url"] == "https://docs.google.com/document/d/D1/edit"
    assert recorder["create_body"] == {"title": "My Doc"}
    # No text -> no batchUpdate.
    assert "batch_bodies" not in recorder


def test_create_doc_with_text_inserts_via_batchupdate(recorder):
    result = docs.create_doc("op", "My Doc", text="Hello there")
    assert result["document_id"] == "D1"
    assert recorder["batch_id"] == "D1"
    bodies = recorder["batch_bodies"]
    assert len(bodies) == 1
    reqs = bodies[0]["requests"]
    assert reqs == [{"insertText": {"location": {"index": 1}, "text": "Hello there"}}]


# --- read_doc -------------------------------------------------------------

def test_read_doc_returns_text_and_element_index_info(recorder):
    result = docs.read_doc("op", "D1")
    assert recorder["get_id"] == "D1"
    assert "error" not in result
    assert result["document_id"] == "D1"
    assert result["title"] == "Canned Doc"
    # Plain text concatenated from the textRuns.
    assert "Hello world" in result["text"]
    assert "Title" in result["text"]
    # Structured element list with start/end indices for targeting edits.
    elements = result["elements"]
    assert isinstance(elements, list) and len(elements) >= 1
    first = elements[0]
    assert first["startIndex"] == 1
    assert first["endIndex"] == 13
    assert "Hello world" in first["text"]
    # Object IDs (inline objects) surfaced so the model can target them.
    assert "kix.abc123" in result["inline_object_ids"]


# --- docs_batch_update ----------------------------------------------------

def test_docs_batch_update_forwards_requests_verbatim(recorder):
    requests = [
        {"insertText": {"location": {"index": 1}, "text": "X"}},
        {"updateTextStyle": {
            "range": {"startIndex": 1, "endIndex": 2},
            "textStyle": {"bold": True},
            "fields": "bold",
        }},
    ]
    result = docs.docs_batch_update("op", "D1", requests)
    assert "error" not in result
    assert recorder["batch_id"] == "D1"
    body = recorder["batch_bodies"][-1]
    # Same list object, forwarded verbatim under "requests".
    assert body["requests"] is requests


# --- service None ---------------------------------------------------------

def test_create_doc_service_none(monkeypatch):
    monkeypatch.setattr(docs, "get_docs_service", lambda operator: None)
    result = docs.create_doc("op", "X")
    assert "error" in result


def test_read_doc_service_none(monkeypatch):
    monkeypatch.setattr(docs, "get_docs_service", lambda operator: None)
    result = docs.read_doc("op", "D1")
    assert "error" in result


def test_docs_batch_update_service_none(monkeypatch):
    monkeypatch.setattr(docs, "get_docs_service", lambda operator: None)
    result = docs.docs_batch_update("op", "D1", [])
    assert "error" in result


# --- HttpError handling ---------------------------------------------------

def test_create_doc_403_message_says_reconnect(monkeypatch):
    from googleapiclient.errors import HttpError

    class FakeResp:
        status = 403
        reason = "insufficientPermissions"

    def boom(*a, **k):
        raise HttpError(FakeResp(), b"forbidden")

    class BoomDocuments:
        def create(self, body=None):
            boom()

    class BoomService:
        def documents(self):
            return BoomDocuments()

    monkeypatch.setattr(docs, "get_docs_service", lambda operator: BoomService())
    result = docs.create_doc("op", "X")
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


def test_create_doc_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("create_doc")
    res = _run(mod.execute({"title": "X", "operator": "op"}, ToolContext(operator="op")))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_read_doc_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("read_doc")
    res = _run(mod.execute({"document_id": "D1", "operator": "op"}, ToolContext(operator="op")))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_docs_batch_update_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("docs_batch_update")
    res = _run(mod.execute(
        {"document_id": "D1", "requests": [], "operator": "op"},
        ToolContext(operator="op"),
    ))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_docs_batch_update_executor_requires_list(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: True)
    mod = _load_executor("docs_batch_update")
    res = _run(mod.execute(
        {"document_id": "D1", "requests": "notalist", "operator": "op"},
        ToolContext(operator="op"),
    ))
    assert res.success is False
    assert "requests must be a list" in res.result.lower()


def test_create_doc_executor_success(monkeypatch, recorder):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: True)
    mod = _load_executor("create_doc")
    res = _run(mod.execute({"title": "Doc!", "operator": "op"}, ToolContext(operator="op")))
    assert res.success is True
    payload = json.loads(res.result)
    assert payload["document_id"] == "D1"
