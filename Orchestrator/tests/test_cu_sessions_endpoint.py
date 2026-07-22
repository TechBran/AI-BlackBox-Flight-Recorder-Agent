"""M9: /cu/sessions surfaces active virtual CU sessions for the D14 active-sessions badge/list."""
import Orchestrator.app  # noqa: F401 — registers the /cu/sessions route onto the shared app
from starlette.testclient import TestClient
from Orchestrator.checkpoint import app
from Orchestrator.browser import display as disp


def test_sessions_empty(monkeypatch):
    monkeypatch.setattr(disp.DisplayAllocator, "active_sessions", lambda self: [])
    r = TestClient(app).get("/cu/sessions")
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is False and body["count"] == 0 and body["sessions"] == []


def test_sessions_reports_active(monkeypatch):
    fake = [{"session_id": "s1", "operator": "Brandon", "backend": "anthropic",
             "width": 1280, "height": 720, "display": ":100", "live_view": True,
             "view_url": "/cu/view/s1", "started_at": 1.0}]
    monkeypatch.setattr(disp.DisplayAllocator, "active_sessions", lambda self: fake)
    r = TestClient(app).get("/cu/sessions")
    body = r.json()
    assert body["active"] is True and body["count"] == 1
    assert body["sessions"][0]["operator"] == "Brandon"
    assert body["sessions"][0]["view_url"] == "/cu/view/s1"
