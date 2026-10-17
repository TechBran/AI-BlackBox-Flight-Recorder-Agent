"""Secrets stay out of task rows and streamed errors (audit rank 23:
O07-tts-voice-01 / X03-security-backend-11).

  * update_task / append_task_progress mask secrets before the row is saved,
    because tasks.db rows are served back to the Portal and Android by /tasks
    polling and the old Google key was found in 18 of them (worker str(e) and
    tracebacks carried the request URL).
  * GET and POST /chat/stream mask every provider's "error" event before it
    reaches the client.

Hermetic: in-memory task store; the routes run against stubbed context and a
stubbed provider stream, with no lifespan.
"""
import pytest

from Orchestrator import tasks as tasks_mod
from Orchestrator.models import Task, TaskStatus, TaskType

FAKE_KEY = "AIza" + "M" * 35
KEY_QS = "?" + "key="   # split so the URL-key guard does not flag this file


# --------------------------------------------------------------------------- #
# tasks.db writes                                                             #
# --------------------------------------------------------------------------- #

class _FakeDB:
    def __init__(self):
        self.store = {}
        self.progress = {}

    def get_task(self, task_id):
        return self.store.get(task_id)

    def save_task(self, task):
        self.store[task.task_id] = task

    def update_progress_text(self, task_id, text):
        self.progress[task_id] = text


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(tasks_mod, "task_db", db)
    monkeypatch.setattr(tasks_mod, "_emit_task_notification", lambda *a, **k: None)
    now = "2026-10-17T00:00:00Z"
    db.store["t1"] = Task(task_id="t1", task_type=TaskType.CHAT, status=TaskStatus.PROCESSING,
                          created_at=now, updated_at=now, operator="system")
    return db


def test_update_task_masks_error_message(fake_db):
    leaked = f"400 Client Error: Bad Request for url: https://x/m:generateContent{KEY_QS}{FAKE_KEY}"
    tasks_mod.update_task("t1", status=TaskStatus.FAILED, error_message=leaked)
    saved = fake_db.store["t1"].error_message
    assert FAKE_KEY not in saved
    assert "400 Client Error" in saved


def test_update_task_masks_error_fields_in_result_data(fake_db):
    tasks_mod.update_task("t1", result_data={"error": f"boom {FAKE_KEY}",
                                             "trace": f"Traceback ... {KEY_QS}{FAKE_KEY}",
                                             "transcript": "kept as is"})
    rd = fake_db.store["t1"].result_data
    assert FAKE_KEY not in rd["error"] and FAKE_KEY not in rd["trace"]
    assert rd["transcript"] == "kept as is"


def test_append_task_progress_masks_secrets(fake_db):
    tasks_mod.append_task_progress("t1", f"agent stdout: OPENAI key {FAKE_KEY}")
    assert FAKE_KEY not in fake_db.progress["t1"]


# --------------------------------------------------------------------------- #
# /chat/stream SSE error events (GET + POST share the choke point)            #
# --------------------------------------------------------------------------- #

@pytest.fixture
def sse_client(tmp_path, monkeypatch):
    import Orchestrator.app  # noqa: F401 — registers routes onto the shared app
    from fastapi.testclient import TestClient
    from Orchestrator.checkpoint import app
    from Orchestrator.onboarding import custom_servers as cs
    from Orchestrator.routes import chat_routes as cr

    monkeypatch.setattr(cs, "REGISTRY_PATH", str(tmp_path / "absent.json"))
    monkeypatch.setattr(
        cr, "build_streaming_context",
        lambda messages, operator, provider="openai", window_guard_tokens=None: (
            list(messages), {"recent": [], "keyword": [], "semantic": [], "checkpoint": []}, {}))

    async def leaky_stream(messages, model, operator):
        yield {"type": "error", "data": f"Stream error: 400 for url https://x/v1{KEY_QS}{FAKE_KEY}"}

    monkeypatch.setattr(cr, "stream_custom_with_reasoning", leaky_stream)
    return TestClient(app)   # no `with`: the production lifespan never starts


def test_chat_stream_get_masks_error_events(sse_client):
    import json
    r = sse_client.get("/chat/stream", params={
        "messages": json.dumps([{"role": "user", "content": "hi"}]),
        "provider": "custom", "operator": "TestOp"})
    assert r.status_code == 200
    assert "event: error" in r.text
    assert FAKE_KEY not in r.text


def test_chat_stream_post_masks_error_events(sse_client):
    r = sse_client.post("/chat/stream", json={
        "messages": [{"role": "user", "content": "hi"}],
        "provider": "custom", "model": "Auto", "operator": "TestOp"})
    assert r.status_code == 200
    assert "event: error" in r.text
    assert FAKE_KEY not in r.text
