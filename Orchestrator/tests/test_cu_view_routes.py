"""M9: /cu/view — the viewer HTML carries the session's /cu/view/{id}/ws proxy
path (the ws port itself is resolved server-side at connect time, never embedded
in the page); an unknown session degrades gracefully; and the WS proxy accepts
then closes 1008 for an unknown/dead session."""
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
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
    """M2 promoted the inline HTML to the served Portal/cu-view asset set: the
    page now loads the client module; the WS path is constructed CLIENT-SIDE in
    cu-view.js from location + /cu/view/{sid}/ws (asserted below on the JS)."""
    _fake_handle(monkeypatch)
    r = TestClient(app).get("/cu/view/sess-1")
    assert r.status_code == 200
    assert "/ui/cu-view/cu-view.js" in r.text        # the served Splashtop client


def _cu_view_js() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[2] / "Portal" / "cu-view" / "cu-view.js").read_text()


def test_view_client_is_interactive_by_default():
    """D1 (2026-07-23 design): the client constructs RFB WITHOUT viewOnly —
    input flows over RFB into this session's own Xvfb. A ?viewonly=1 query
    param opts back into watch-only, evaluated client-side. These behaviors
    moved into cu-view.js when M2 promoted the inline HTML to served assets."""
    js = _cu_view_js()
    assert "viewOnly = true" not in js               # the old D11 hard gate is gone
    assert 'get("viewonly")' in js                    # the query opt-out is wired
    assert "/ws" in js and "/cu/view/" in js          # WS proxy path construction
    assert "resizeSession" in js                      # invariant handled client-side
    assert "resizeSession = true" not in js           # never resize the agent's screen


def test_view_client_carries_the_switcher_rail():
    """N2 (main-desktop switcher): the served client builds the
    [Main desktop]+sessions rail from /cu/sessions via the PURE switcher.js
    module (node-tested in switcher.test.mjs) and swaps streams in place with
    a history.replaceState URL rewrite so deep links keep working."""
    js = _cu_view_js()
    assert "./switcher.js" in js                      # pure state module wired
    assert "history.replaceState" in js               # in-place URL rewrite
    assert "/cu/sessions" in js                       # ONE payload feeds the rail
    from pathlib import Path
    html = (Path(__file__).resolve().parents[2]
            / "Portal" / "cu-view" / "index.html").read_text()
    assert "cuvSwitcher" in html                      # the rail container exists


def test_novnc_vendored_mount_serves_rfb_core():
    """D3: /cu/novnc serves the PINNED vendored noVNC (Portal/vendor/novnc,
    v1.5.0) — the RFB entry module and its pako dependency closure must both
    resolve, or the viewer's ES-module import chain 404s at runtime."""
    client = TestClient(app)
    rfb = client.get("/cu/novnc/core/rfb.js")
    assert rfb.status_code == 200
    assert "RFB" in rfb.text
    # core/inflator.js imports ../vendor/pako/lib/zlib/inflate.js — the only
    # dependency outside core/; a missing pako tree breaks the import chain.
    pako = client.get("/cu/novnc/vendor/pako/lib/zlib/inflate.js")
    assert pako.status_code == 200


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


def test_view_diag_beacon_wiring():
    """Android-WebView black-screen hunt (2026-07-23): the served page carries
    an INLINE classic-script beacon (runs even when the ES-module chain fails)
    and POST /cu/view/diag accepts its payload log-only — it must never error
    back at the page, even on garbage bodies."""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[2]
            / "Portal" / "cu-view" / "index.html").read_text()
    assert "/cu/view/diag" in html                     # inline beacon wired
    assert 'diag("page-load"' in html                  # fires before modules
    client = TestClient(app)
    r = client.post("/cu/view/diag",
                    json={"event": "page-load", "sid": "s1", "detail": "ua"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    r = client.post("/cu/view/diag", content=b"not-json")   # beacon blobs vary
    assert r.status_code == 200


def test_ws_proxy_rejects_unknown_session(monkeypatch):
    """The load-bearing reverse-proxy: for an unknown/dead session the proxy
    accepts the handshake (so the client sees a real WS) then closes 1008 — it
    never dials a loopback ws port for a session the allocator doesn't know."""
    _fake_handle(monkeypatch)  # only "sess-1" resolves; "nope" -> None
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/cu/view/nope/ws") as ws:
            ws.receive_text()  # server accepted, then closes with 1008
    assert excinfo.value.code == 1008
