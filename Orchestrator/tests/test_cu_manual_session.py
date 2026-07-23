"""Desktop-first CU (2026-07-23): POST /cu/session/open ensures-or-creates a
live VIRTUAL session with NO agent loop, POST /cu/session/{sid}/close ends it.

The two CRITICAL invariants pinned here:

1. ATTACH — a subsequent agent task for the same operator (the headless path
   calls session_manager.get_or_create_session(operator, device_id=...)) picks
   up the manually opened session OBJECT, not a fresh one.
2. NOT IMMORTAL — idle expiry still reaps manual sessions: an expired manual
   session is (a) swept by cleanup_inactive_sessions and (b) never reused by
   get_or_create_session.

No Xvfb/Chrome is spawned: ensure_browser / is_alive / destroy are patched at
the class so the real session store + reuse logic run for real.
"""
import pytest
from starlette.testclient import TestClient

import Orchestrator.app  # noqa: F401 — registers /cu/session routes onto the shared app
from Orchestrator.checkpoint import app
from Orchestrator.browser import session_manager as sm


@pytest.fixture
def clean_store(monkeypatch):
    """Isolated session store — never touch (or leak into) the module dicts."""
    monkeypatch.setattr(sm, "_sessions", {})
    monkeypatch.setattr(sm, "_operator_sessions", {})


@pytest.fixture
def fake_browser(monkeypatch):
    """Sessions whose browser 'starts' without spawning anything.

    ensure_browser flips a flag; is_alive mirrors it (the real predicate is
    chrome.is_running()); destroy records the torn-down ids instead of killing
    processes. Returns the destroyed-ids list for assertions.
    """
    destroyed = []

    async def _ensure(self, url="about:blank", backend="anthropic"):
        self._fake_started = True
        return True

    monkeypatch.setattr(sm.ComputerUseSession, "ensure_browser", _ensure)
    monkeypatch.setattr(sm.ComputerUseSession, "is_alive",
                        lambda self: getattr(self, "_fake_started", False))
    monkeypatch.setattr(sm.ComputerUseSession, "destroy",
                        lambda self: destroyed.append(self.session_id))
    return destroyed


def _open(client, operator="Brandon"):
    r = client.post("/cu/session/open", json={"operator": operator})
    assert r.status_code == 200, r.text
    return r.json()


# ── /cu/session/open ─────────────────────────────────────────────────────────

def test_open_creates_a_session_without_agent_loop(clean_store, fake_browser):
    client = TestClient(app)
    data = _open(client)
    sid = data["session_id"]
    assert data["view_url"] == f"/cu/view/{sid}"
    assert data["reused"] is False
    session = sm._sessions[sid]
    assert session.operator == "Brandon"
    assert session.agent_task is None          # no agent loop was started
    assert session.status == "idle"
    assert session.native_mode is False        # manual desktop is always virtual


def test_open_twice_reuses_the_live_session(clean_store, fake_browser):
    client = TestClient(app)
    first = _open(client)
    second = _open(client)
    assert second["session_id"] == first["session_id"]
    assert second["reused"] is True
    assert len(sm._sessions) == 1


def test_open_defaults_to_system_operator(clean_store, fake_browser):
    client = TestClient(app)
    r = client.post("/cu/session/open")
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    assert sm._sessions[sid].operator == "system"


def test_open_browser_start_failure_is_502_and_leaves_no_session(clean_store, monkeypatch):
    async def _fail(self, url="about:blank", backend="anthropic"):
        return False
    monkeypatch.setattr(sm.ComputerUseSession, "ensure_browser", _fail)
    monkeypatch.setattr(sm.ComputerUseSession, "destroy", lambda self: None)
    client = TestClient(app)
    r = client.post("/cu/session/open", json={"operator": "Brandon"})
    assert r.status_code == 502
    assert sm._sessions == {}                  # nothing half-created left pinned


# ── ATTACH: manual-open then agent-task reuse (the load-bearing semantics) ───

def test_manual_open_then_task_attach_reuses_the_session_object(clean_store, fake_browser):
    """A later agent task for the same operator resolves its session through
    get_or_create_session(operator, device_id=...) — exactly what
    browser/headless.run_cu_task does — and must get THE manual session back."""
    client = TestClient(app)
    sid = _open(client, operator="Brandon")["session_id"]

    attached = sm.get_or_create_session("Brandon", device_id="blackbox")
    assert attached.session_id == sid
    assert attached is sm._sessions[sid]       # same object, not a lookalike
    assert len(sm._sessions) == 1

    # A DIFFERENT operator does not attach to it.
    other = sm.get_or_create_session("Alice", device_id="blackbox")
    assert other.session_id != sid


# ── NOT IMMORTAL: idle expiry still reaps manual sessions ────────────────────

def test_idle_expiry_reaps_manual_sessions(clean_store, fake_browser):
    client = TestClient(app)
    sid = _open(client)["session_id"]

    # Fresh session survives a sweep…
    sm.cleanup_inactive_sessions(timeout=600)
    assert sid in sm._sessions

    # …but an idle-expired one is reaped (destroy called, both dicts cleared).
    sm._sessions[sid].last_activity -= 10_000
    sm.cleanup_inactive_sessions(timeout=600)
    assert sid not in sm._sessions
    assert sm._operator_sessions == {}
    assert sid in fake_browser                 # destroy() actually ran


def test_expired_manual_session_is_not_reused_on_next_attach(clean_store, fake_browser):
    client = TestClient(app)
    sid = _open(client)["session_id"]
    sm._sessions[sid].last_activity -= 10 * sm.SESSION_TIMEOUT

    fresh = sm.get_or_create_session("Brandon", device_id="blackbox")
    assert fresh.session_id != sid             # expired → recreated, not reused
    assert sid not in sm._sessions
    assert sid in fake_browser


# ── /cu/session/{sid}/close ──────────────────────────────────────────────────

def test_close_ends_the_session_and_404s_when_unknown(clean_store, fake_browser):
    client = TestClient(app)
    sid = _open(client)["session_id"]

    r = client.post(f"/cu/session/{sid}/close")
    assert r.status_code == 200
    assert r.json() == {"success": True, "session_id": sid}
    assert sid not in sm._sessions
    assert sid in fake_browser                 # existing cleanup path ran

    # Closing again — or any unknown id — is a 404.
    assert client.post(f"/cu/session/{sid}/close").status_code == 404
    assert client.post("/cu/session/never-existed/close").status_code == 404
