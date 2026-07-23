"""D8 (2026-07-23 CU live-view design): /browser/status in VIRTUAL mode must
report cu_resolution. Before the fix the virtual-shape payload lacked the field,
so interactive viewers (cu-interact.js) silently kept their 1280x720 default —
wrong for a 1440x900 Gemini session. The field is additive: every pre-existing
virtual-shape key is retained."""
import Orchestrator.app  # noqa: F401 — registers /browser/status onto the shared app
from starlette.testclient import TestClient
from Orchestrator.checkpoint import app
from Orchestrator.browser import display as disp
import Orchestrator.browser.config as bcfg


def _virtual(monkeypatch, sessions):
    monkeypatch.setattr(bcfg, "NATIVE_MODE", False)
    monkeypatch.setattr(disp.DisplayAllocator, "active_sessions",
                        lambda self: sessions)


def _gemini_session():
    return {"session_id": "s-gem", "operator": "op", "backend": "gemini",
            "width": 1440, "height": 900, "display": ":100", "live_view": True,
            "view_url": "/cu/view/s-gem", "started_at": 1.0}


def test_virtual_status_reports_session_resolution(monkeypatch):
    _virtual(monkeypatch, [_gemini_session()])
    body = TestClient(app).get("/browser/status").json()
    assert body["native_mode"] is False
    assert body["cu_resolution"] == "1440x900"


def test_virtual_status_idle_falls_back_to_cu_default(monkeypatch):
    _virtual(monkeypatch, [])
    body = TestClient(app).get("/browser/status").json()
    assert body["cu_resolution"] == f"{bcfg.DISPLAY_WIDTH}x{bcfg.DISPLAY_HEIGHT}"


def test_virtual_status_shape_is_additive(monkeypatch):
    """Pre-existing virtual-shape keys survive the D8 fix (contract additive-only)."""
    _virtual(monkeypatch, [_gemini_session()])
    body = TestClient(app).get("/browser/status").json()
    for key in ("display_running", "native_mode", "virtual_sessions", "cap", "sessions"):
        assert key in body
    assert body["display_running"] is True
    assert body["virtual_sessions"] == 1
    assert body["sessions"][0]["session_id"] == "s-gem"


def test_virtual_status_first_session_wins(monkeypatch):
    """Multiple sessions: report sessions[0]'s WxH — matching the viewer's
    hard-open-sessions[0] behavior."""
    anth = {"session_id": "s-a", "operator": "op", "backend": "anthropic",
            "width": 1280, "height": 720, "display": ":101", "live_view": True,
            "view_url": "/cu/view/s-a", "started_at": 2.0}
    _virtual(monkeypatch, [_gemini_session(), anth])
    body = TestClient(app).get("/browser/status").json()
    assert body["cu_resolution"] == "1440x900"
