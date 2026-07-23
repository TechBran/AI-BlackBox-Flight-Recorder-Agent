"""Main-desktop streaming (Brandon 2026-07-23: "if we have no task available,
we should go directly to the main desktop").

Covers: the availability probe (logged-in X session + xauth), the refcounted
spawn/reap lifecycle (fake procs, short grace), the pure /cu/view/auto
resolver matrix, and main-vs-session route dispatch (/cu/main/status,
/cu/sessions additive "main" key, /cu/view/{main,auto}, WS refusal)."""
import time

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import Orchestrator.app  # noqa: F401 — registers the /cu routes onto the shared app
from Orchestrator.checkpoint import app
from Orchestrator.browser import display as disp
from Orchestrator.browser import native_stream as ns


AVAILABLE = {"available": True, "display": ":0", "resolution": "1920x1080",
             "xauthority": "/run/user/1000/.mutter-Xwaylandauth.TEST"}
UNAVAILABLE = {"available": False, "reason": "log into the desktop session"}


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    ns._probe_cache["ts"], ns._probe_cache["payload"] = 0.0, None
    yield
    ns._probe_cache["ts"], ns._probe_cache["payload"] = 0.0, None


class FakeProc:
    """Quacks like the Popen surface _terminate_proc/_alive_locked touch."""
    def __init__(self):
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


# ── Availability probe ────────────────────────────────────────────────────


def test_probe_unavailable_when_no_session(monkeypatch):
    monkeypatch.setattr(ns, "_detect_session", lambda: None)
    out = ns.probe_main_desktop(force=True)
    assert out == {"available": False, "reason": "log into the desktop session"}


def test_probe_unavailable_when_xauth_unresolvable(monkeypatch):
    monkeypatch.setattr(ns, "_detect_session", lambda: (0, ""))
    monkeypatch.setattr(ns, "_detect_xauthority", lambda: "")
    out = ns.probe_main_desktop(force=True)
    assert out["available"] is False
    assert out["reason"] == "log into the desktop session"


def test_probe_available_reports_display_and_resolution(monkeypatch, tmp_path):
    """The SESSION's own XAUTHORITY (e.g. GDM's /run/user/<uid>/gdm/Xauthority,
    harvested from gnome-shell's environ) wins — resolvable == file exists."""
    xauth = tmp_path / "gdm-Xauthority"
    xauth.write_bytes(b"")
    monkeypatch.setattr(ns, "_detect_session", lambda: (0, str(xauth)))
    monkeypatch.setattr(ns, "_detect_resolution", lambda d, x: "2560x1440")
    out = ns.probe_main_desktop(force=True)
    assert out["available"] is True
    assert out["display"] == ":0"
    assert out["resolution"] == "2560x1440"
    assert out["xauthority"] == str(xauth)  # internal plumbing for spawn


def test_probe_falls_back_to_config_xauth_patterns(monkeypatch, tmp_path):
    """When the session env names a stale/absent xauth file, the probe falls
    back to config._detect_xauthority's Mutter/classic patterns."""
    fallback = tmp_path / "Xauthority"
    fallback.write_bytes(b"")
    monkeypatch.setattr(ns, "_detect_session", lambda: (0, "/nonexistent/xauth"))
    monkeypatch.setattr(ns, "_detect_xauthority", lambda: str(fallback))
    monkeypatch.setattr(ns, "_detect_resolution", lambda d, x: None)
    out = ns.probe_main_desktop(force=True)
    assert out["available"] is True
    assert out["xauthority"] == str(fallback)


def test_public_status_strips_xauthority(monkeypatch):
    monkeypatch.setattr(ns, "probe_main_desktop",
                        lambda force=False: dict(AVAILABLE))
    pub = ns.public_main_status()
    assert "xauthority" not in pub
    assert pub == {"available": True, "display": ":0", "resolution": "1920x1080"}


def test_probe_cache_reused_within_ttl(monkeypatch):
    calls = {"n": 0}

    def fake_detect():
        calls["n"] += 1
        return None

    monkeypatch.setattr(ns, "_detect_session", fake_detect)
    ns.probe_main_desktop()
    ns.probe_main_desktop()  # cached — /cu/sessions polling stays cheap
    assert calls["n"] == 1
    ns.probe_main_desktop(force=True)
    assert calls["n"] == 2


# ── Refcounted spawn/reap ─────────────────────────────────────────────────


def _stream(monkeypatch, grace_s=0.05):
    spawned = []

    def fake_spawn(display, xauth):
        procs = {"x11vnc": FakeProc(), "websockify": FakeProc()}
        spawned.append(procs)
        return procs

    monkeypatch.setattr(ns, "probe_main_desktop",
                        lambda force=False: dict(AVAILABLE))
    monkeypatch.setattr(ns, "_live_view_available", lambda: True)
    monkeypatch.setattr(ns, "_spawn_stream_procs", fake_spawn)
    return ns.NativeDesktopStream(grace_s=grace_s), spawned


def test_spawns_on_first_connect_only(monkeypatch):
    mgr, spawned = _stream(monkeypatch)
    assert mgr.acquire() == ns.MAIN_WS_PORT
    assert mgr.acquire() == ns.MAIN_WS_PORT   # second viewer reuses the pair
    assert len(spawned) == 1
    assert mgr.viewers == 2


def test_no_reap_while_viewers_remain(monkeypatch):
    mgr, spawned = _stream(monkeypatch)
    mgr.acquire()
    mgr.acquire()
    mgr.release()                              # one viewer left
    time.sleep(0.2)
    assert not spawned[0]["x11vnc"].terminated
    assert mgr.viewers == 1


def test_reaps_after_last_disconnect_plus_grace(monkeypatch):
    mgr, spawned = _stream(monkeypatch)
    mgr.acquire()
    mgr.release()
    assert not spawned[0]["x11vnc"].terminated  # grace window: still alive
    time.sleep(0.3)
    assert spawned[0]["x11vnc"].terminated
    assert spawned[0]["websockify"].terminated


def test_reconnect_within_grace_cancels_reap(monkeypatch):
    mgr, spawned = _stream(monkeypatch)
    mgr.acquire()
    mgr.release()
    mgr.acquire()                              # tab reload inside the grace
    time.sleep(0.3)
    assert not spawned[0]["x11vnc"].terminated
    assert len(spawned) == 1                   # never respawned
    mgr.shutdown()


def test_respawns_after_reap(monkeypatch):
    mgr, spawned = _stream(monkeypatch)
    mgr.acquire()
    mgr.release()
    time.sleep(0.3)                            # reaped
    mgr.acquire()
    assert len(spawned) == 2
    mgr.shutdown()


def test_acquire_refuses_when_desktop_unavailable(monkeypatch):
    monkeypatch.setattr(ns, "probe_main_desktop",
                        lambda force=False: dict(UNAVAILABLE))
    mgr = ns.NativeDesktopStream(grace_s=0.05)
    with pytest.raises(RuntimeError, match="log into the desktop session"):
        mgr.acquire()
    assert mgr.viewers == 0


def test_acquire_refuses_without_live_view_stack(monkeypatch):
    monkeypatch.setattr(ns, "probe_main_desktop",
                        lambda force=False: dict(AVAILABLE))
    monkeypatch.setattr(ns, "_live_view_available", lambda: False)
    mgr = ns.NativeDesktopStream(grace_s=0.05)
    with pytest.raises(RuntimeError, match="websockify"):
        mgr.acquire()


# ── Pure auto-view resolver matrix ────────────────────────────────────────


SESSIONS = [{"session_id": "s1", "view_url": "/cu/view/s1"},
            {"session_id": "s2", "view_url": "/cu/view/s2"}]


def test_auto_prefers_live_session_over_main():
    out = ns.resolve_auto_view(SESSIONS, dict(AVAILABLE))
    assert out["kind"] == "session" and out["url"] == "/cu/view/s1"


def test_auto_falls_back_to_main_when_no_sessions():
    out = ns.resolve_auto_view([], dict(AVAILABLE))
    assert out == {"kind": "main", "url": "/cu/view/main"}


def test_auto_nothing_to_show_names_reason():
    out = ns.resolve_auto_view([], dict(UNAVAILABLE))
    assert out["kind"] == "none"
    assert out["reason"] == "log into the desktop session"


def test_auto_session_wins_even_when_main_unavailable():
    out = ns.resolve_auto_view(SESSIONS[1:], dict(UNAVAILABLE))
    assert out["kind"] == "session" and out["url"] == "/cu/view/s2"


# ── Route dispatch ────────────────────────────────────────────────────────


def test_main_status_route(monkeypatch):
    monkeypatch.setattr(ns, "probe_main_desktop",
                        lambda force=False: dict(AVAILABLE))
    body = TestClient(app).get("/cu/main/status").json()
    assert body == {"available": True, "display": ":0",
                    "resolution": "1920x1080"}


def test_main_status_route_unavailable(monkeypatch):
    monkeypatch.setattr(ns, "probe_main_desktop",
                        lambda force=False: dict(UNAVAILABLE))
    body = TestClient(app).get("/cu/main/status").json()
    assert body == {"available": False, "reason": "log into the desktop session"}


def test_cu_sessions_gains_additive_main_key(monkeypatch):
    monkeypatch.setattr(disp.DisplayAllocator, "active_sessions", lambda self: [])
    monkeypatch.setattr(ns, "probe_main_desktop",
                        lambda force=False: dict(AVAILABLE))
    body = TestClient(app).get("/cu/sessions").json()
    # Pre-existing shape intact (additive change only)
    assert body["active"] is False and body["count"] == 0 and body["sessions"] == []
    assert body["main"] == {"available": True, "display": ":0",
                            "resolution": "1920x1080"}


def test_view_main_serves_client_page(monkeypatch):
    monkeypatch.setattr(ns, "probe_main_desktop",
                        lambda force=False: dict(AVAILABLE))
    monkeypatch.setattr(disp, "_live_view_available", lambda: True)
    r = TestClient(app).get("/cu/view/main")
    assert r.status_code == 200
    assert "/ui/cu-view/cu-view.js" in r.text  # the SAME Splashtop client asset


def test_view_main_unavailable_names_reason(monkeypatch):
    monkeypatch.setattr(ns, "probe_main_desktop",
                        lambda force=False: dict(UNAVAILABLE))
    r = TestClient(app).get("/cu/view/main")
    assert r.status_code == 503
    assert "log into the desktop session" in r.text


def test_view_main_never_hits_session_lookup(monkeypatch):
    """'main' is a reserved id — the allocator must never be consulted for it."""
    monkeypatch.setattr(ns, "probe_main_desktop",
                        lambda force=False: dict(UNAVAILABLE))

    def boom(self, sid):
        raise AssertionError("allocator lookup for reserved id 'main'")

    monkeypatch.setattr(disp.DisplayAllocator, "get", boom)
    r = TestClient(app).get("/cu/view/main")
    assert r.status_code == 503  # dispatched to the main path, not a 404 lookup


def test_view_auto_redirects_to_live_session(monkeypatch):
    monkeypatch.setattr(disp.DisplayAllocator, "active_sessions",
                        lambda self: list(SESSIONS))
    monkeypatch.setattr(ns, "probe_main_desktop",
                        lambda force=False: dict(AVAILABLE))
    r = TestClient(app).get("/cu/view/auto", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/cu/view/s1"


def test_view_auto_redirects_to_main_when_idle(monkeypatch):
    monkeypatch.setattr(disp.DisplayAllocator, "active_sessions", lambda self: [])
    monkeypatch.setattr(ns, "probe_main_desktop",
                        lambda force=False: dict(AVAILABLE))
    r = TestClient(app).get("/cu/view/auto", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/cu/view/main"


def test_view_auto_nothing_to_show_page(monkeypatch):
    monkeypatch.setattr(disp.DisplayAllocator, "active_sessions", lambda self: [])
    monkeypatch.setattr(ns, "probe_main_desktop",
                        lambda force=False: dict(UNAVAILABLE))
    r = TestClient(app).get("/cu/view/auto", follow_redirects=False)
    assert r.status_code == 200
    assert "Nothing to show" in r.text
    assert "log into the desktop session" in r.text


def test_ws_main_refused_when_unavailable(monkeypatch):
    """WS dispatch: /cu/view/main/ws goes to the native manager (accept then
    close 1008 with the probe's reason), never a session lookup."""
    class FakeMgr:
        viewers = 0

        def acquire(self):
            raise RuntimeError("log into the desktop session")

        def release(self):
            pass

    monkeypatch.setattr(ns, "get_native_stream", lambda: FakeMgr())
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/cu/view/main/ws") as ws:
            ws.receive_text()
    assert excinfo.value.code == 1008
