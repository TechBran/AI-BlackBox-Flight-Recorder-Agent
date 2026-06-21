"""Tests for the Google Slides helper module + the presentation tools.

Task 4 of the Google Workspace integration — mirrors test_google_sheets.py.
These tests monkeypatch `Orchestrator.google_workspace.slides.get_slides_service`
with a fake `presentations()` resource so no live API call is made, and assert
the helpers:
  - create_presentation returns presentation_id + url.
  - read_presentation returns the slides with their objectIds + each slide's
    pageElements (objectId, type, and any text) so the model can target edits.
  - slides_batch_update forwards the `requests` array VERBATIM into the API body.
  - return {"error": ...} when the service is unavailable.
And that the tool executors return the "not connected" message when
`workspace_connected` is False, and reject non-list `requests`.
"""

import asyncio
import json

import pytest

from Orchestrator.google_workspace import slides
from Orchestrator.toolvault.context import ToolContext


# --- fake Google Slides service -------------------------------------------

# A canned presentations().get() body: two slides, each with a couple of
# pageElements so the structured read can surface objectIds + types + text.
CANNED_PRESENTATION = {
    "presentationId": "P1",
    "title": "Canned Deck",
    "slides": [
        {
            "objectId": "slide_1",
            "pageElements": [
                {
                    "objectId": "title_1",
                    "shape": {
                        "shapeType": "TEXT_BOX",
                        "text": {
                            "textElements": [
                                {"textRun": {"content": "Hello "}},
                                {"textRun": {"content": "World"}},
                            ]
                        },
                    },
                },
                {
                    "objectId": "img_1",
                    "image": {"contentUrl": "https://example.com/x.png"},
                },
            ],
        },
        {
            "objectId": "slide_2",
            "pageElements": [
                {
                    "objectId": "body_2",
                    "shape": {
                        "shapeType": "TEXT_BOX",
                        "text": {
                            "textElements": [
                                {"textRun": {"content": "Second slide"}},
                            ]
                        },
                    },
                },
            ],
        },
    ],
}


class FakeRequest:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakePresentations:
    def __init__(self, recorder):
        self._recorder = recorder

    def create(self, body=None):
        self._recorder["create_body"] = body
        return FakeRequest({
            "presentationId": "P1",
            "title": (body or {}).get("title", ""),
        })

    def get(self, presentationId=None):
        self._recorder["get_id"] = presentationId
        return FakeRequest(CANNED_PRESENTATION)

    def batchUpdate(self, presentationId=None, body=None):
        self._recorder["batch_id"] = presentationId
        self._recorder.setdefault("batch_bodies", []).append(body)
        return FakeRequest({"presentationId": presentationId, "replies": []})


class FakeService:
    def __init__(self, recorder):
        self._presentations = FakePresentations(recorder)

    def presentations(self):
        return self._presentations


@pytest.fixture
def recorder(monkeypatch):
    rec = {}
    monkeypatch.setattr(slides, "get_slides_service", lambda operator: FakeService(rec))
    return rec


# --- create_presentation --------------------------------------------------

def test_create_presentation_returns_id_and_url(recorder):
    result = slides.create_presentation("op", "My Deck")
    assert result["presentation_id"] == "P1"
    assert result["title"] == "My Deck"
    assert result["url"] == "https://docs.google.com/presentation/d/P1/edit"
    assert recorder["create_body"] == {"title": "My Deck"}


# --- read_presentation ----------------------------------------------------

def test_read_presentation_returns_slides_with_object_ids(recorder):
    result = slides.read_presentation("op", "P1")
    assert recorder["get_id"] == "P1"
    assert "error" not in result
    assert result["presentation_id"] == "P1"
    assert result["title"] == "Canned Deck"

    slides_list = result["slides"]
    assert isinstance(slides_list, list) and len(slides_list) == 2

    first = slides_list[0]
    assert first["object_id"] == "slide_1"
    elements = first["page_elements"]
    assert isinstance(elements, list) and len(elements) == 2

    title_el = elements[0]
    assert title_el["object_id"] == "title_1"
    assert title_el["type"] == "shape"
    assert title_el["text"] == "Hello World"

    img_el = elements[1]
    assert img_el["object_id"] == "img_1"
    assert img_el["type"] == "image"

    second = slides_list[1]
    assert second["object_id"] == "slide_2"
    assert second["page_elements"][0]["object_id"] == "body_2"
    assert second["page_elements"][0]["text"] == "Second slide"


# --- slides_batch_update --------------------------------------------------

def test_slides_batch_update_forwards_requests_verbatim(recorder):
    requests = [
        {"createSlide": {"objectId": "new_slide"}},
        {"insertText": {
            "objectId": "title_1",
            "text": "Updated title",
            "insertionIndex": 0,
        }},
    ]
    result = slides.slides_batch_update("op", "P1", requests)
    assert "error" not in result
    assert recorder["batch_id"] == "P1"
    body = recorder["batch_bodies"][-1]
    # Same list object, forwarded verbatim under "requests".
    assert body["requests"] is requests
    assert result["presentation_id"] == "P1"
    assert result["applied"] == 2


# --- service None ---------------------------------------------------------

def test_create_presentation_service_none(monkeypatch):
    monkeypatch.setattr(slides, "get_slides_service", lambda operator: None)
    result = slides.create_presentation("op", "X")
    assert "error" in result


def test_read_presentation_service_none(monkeypatch):
    monkeypatch.setattr(slides, "get_slides_service", lambda operator: None)
    result = slides.read_presentation("op", "P1")
    assert "error" in result


def test_slides_batch_update_service_none(monkeypatch):
    monkeypatch.setattr(slides, "get_slides_service", lambda operator: None)
    result = slides.slides_batch_update("op", "P1", [])
    assert "error" in result


# --- HttpError handling ---------------------------------------------------

def test_create_presentation_403_message_says_reconnect(monkeypatch):
    from googleapiclient.errors import HttpError

    class FakeResp:
        status = 403
        reason = "insufficientPermissions"

    def boom(*a, **k):
        raise HttpError(FakeResp(), b"forbidden")

    class BoomPresentations:
        def create(self, body=None):
            boom()

    class BoomService:
        def presentations(self):
            return BoomPresentations()

    monkeypatch.setattr(slides, "get_slides_service", lambda operator: BoomService())
    result = slides.create_presentation("op", "X")
    assert "error" in result
    assert "reconnect" in result["error"].lower()


# --- tool executors -------------------------------------------------------

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


def test_create_presentation_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("create_presentation")
    res = _run(mod.execute({"title": "X", "operator": "op"}, ToolContext(operator="op")))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_read_presentation_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("read_presentation")
    res = _run(mod.execute({"presentation_id": "P1", "operator": "op"}, ToolContext(operator="op")))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_slides_batch_update_executor_not_connected(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: False)
    mod = _load_executor("slides_batch_update")
    res = _run(mod.execute(
        {"presentation_id": "P1", "requests": [], "operator": "op"},
        ToolContext(operator="op"),
    ))
    assert res.success is False
    assert "not connected" in res.result.lower()


def test_slides_batch_update_executor_requires_list(monkeypatch):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: True)
    mod = _load_executor("slides_batch_update")
    res = _run(mod.execute(
        {"presentation_id": "P1", "requests": "notalist", "operator": "op"},
        ToolContext(operator="op"),
    ))
    assert res.success is False
    assert "requests must be a list" in res.result.lower()


def test_create_presentation_executor_success(monkeypatch, recorder):
    import Orchestrator.gmail.service as svc
    monkeypatch.setattr(svc, "workspace_connected", lambda op: True)
    mod = _load_executor("create_presentation")
    res = _run(mod.execute({"title": "Deck!", "operator": "op"}, ToolContext(operator="op")))
    assert res.success is True
    payload = json.loads(res.result)
    assert payload["presentation_id"] == "P1"
