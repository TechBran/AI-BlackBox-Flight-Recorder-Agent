"""M9: /cu/view — viewer HTML resolves the session's ws port; unknown session
degrades gracefully; the WS proxy rejects an unknown session."""
from starlette.testclient import TestClient
import Orchestrator.app  # noqa: F401 — registers the /cu/view routes onto the shared app
from Orchestrator.checkpoint import app
from Orchestrator.browser import display as disp


def _fake_handle(monkeypatch, session_id="sess-1", ws_port=6101, live=True):
    h = disp.DisplayHandle(session_id=session_id, slot=0, backend="anthropic",
                           operator="op", width=1280, height=720, display_num=100,
                           vnc_port=5901, ws_port=ws_port, live_view=live)
    monkeypatch.setattr(disp.DisplayAllocator, "get",
                        lambda self, sid: h if sid == session_id else None)


def test_view_page_renders_for_known_session(monkeypatch):
    _fake_handle(monkeypatch)
    r = TestClient(app).get("/cu/view/sess-1")
    assert r.status_code == 200
    assert "/cu/view/sess-1/ws" in r.text          # socket path injected
    assert "/cu/novnc/core/rfb.js" in r.text        # noVNC module referenced


def test_view_page_unknown_session_is_friendly(monkeypatch):
    _fake_handle(monkeypatch)
    r = TestClient(app).get("/cu/view/nope")
    assert r.status_code == 404
    assert "No active" in r.text


def test_view_page_live_view_unavailable_notice(monkeypatch):
    _fake_handle(monkeypatch, live=False)
    r = TestClient(app).get("/cu/view/sess-1")
    assert r.status_code == 200
    assert "novnc" in r.text.lower()  # install-novnc notice
