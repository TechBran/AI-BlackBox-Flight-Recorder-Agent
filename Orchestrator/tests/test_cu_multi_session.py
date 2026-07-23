"""Multi-desktop CU session semantics (design 2026-07-23, D1/D2 locked).

Brandon's model: starting a CU agent while the current one is BUSY appends a
brand-new desktop; previous agents keep working in the background and are
returned to only by selecting them. The old world hard-wired one session per
operator (_operator_sessions 1:1), queued new prompts into the busy agent,
and DESTROYED other operators' idle sessions on every fresh create.
"""
import pytest

from Orchestrator.browser import session_manager as bsm
from Orchestrator.browser.session_manager import ComputerUseSession


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(ComputerUseSession, "is_alive", lambda self: True)
    monkeypatch.setattr(ComputerUseSession, "destroy", lambda self: None)
    bsm._sessions.clear()
    bsm._operator_sessions.clear()
    yield
    bsm._sessions.clear()
    bsm._operator_sessions.clear()


def test_force_new_appends_a_session_and_keeps_the_old_alive():
    a = bsm.get_or_create_session("op")
    a.status = "running"
    b = bsm.get_or_create_session("op", force_new=True)
    assert b.session_id != a.session_id
    assert a.session_id in bsm._sessions          # old desktop survives
    assert bsm._sessions[a.session_id].status == "running"
    # MRU pointer follows the newest session.
    assert bsm._operator_sessions["op"] == b.session_id


def test_fresh_create_no_longer_reclaims_other_operators_sessions():
    a = bsm.get_or_create_session("alice")
    a.status = "idle"
    b = bsm.get_or_create_session("bob")
    assert a.session_id in bsm._sessions          # alice's desktop untouched
    assert b.session_id in bsm._sessions


def test_old_session_still_reachable_by_explicit_id():
    a = bsm.get_or_create_session("op")
    a.status = "running"
    b = bsm.get_or_create_session("op", force_new=True)
    assert bsm.get_session("op", a.session_id) is a
    assert bsm.get_session("op", b.session_id) is b
    assert bsm.get_session("op") is b             # MRU without explicit id


def test_cleanup_repairs_the_mru_pointer():
    a = bsm.get_or_create_session("op")
    b = bsm.get_or_create_session("op", force_new=True)
    assert bsm._operator_sessions["op"] == b.session_id
    bsm._cleanup_session(b.session_id)
    # Pointer repairs to the operator's surviving session, not a dead key.
    assert bsm._operator_sessions.get("op") == a.session_id
    bsm._cleanup_session(a.session_id)
    assert "op" not in bsm._operator_sessions


def test_ensure_browser_surfaces_the_allocator_cap_message(monkeypatch):
    class _CapAlloc:
        def get(self, sid):
            return None

        def allocate(self, sid, backend="anthropic", operator="system"):
            raise RuntimeError(
                "CU virtual-display cap reached (3 concurrent sessions)")

    monkeypatch.setattr("Orchestrator.browser.display.get_allocator",
                        lambda: _CapAlloc())
    s = bsm.get_or_create_session("op")
    s.native_mode = False
    import asyncio
    ok = asyncio.run(s.ensure_browser("about:blank"))
    assert ok is False
    # The stream needs the REASON to tell the user which lever to pull —
    # "Failed to start browser session" hid the cap entirely.
    assert "cap" in (s.last_error or "")


def test_streams_open_a_new_desktop_when_busy():
    """The D1 tripwire: all three chat CU streams must branch busy+new-prompt
    into a force_new session instead of enqueueing into the running agent."""
    import inspect
    from Orchestrator.routes import chat_routes
    for fn in (chat_routes.stream_computer_use,
               chat_routes.stream_openai_computer_use,
               chat_routes.stream_gemini_computer_use):
        src = inspect.getsource(fn)
        assert "force_new=True" in src, f"{fn.__name__} missing busy->new-desktop"


def test_gemini_manager_force_new_appends():
    from Orchestrator.gemini_cu import session_manager as gsm
    gsm._sessions.clear()
    gsm._operator_sessions.clear()
    try:
        a = gsm.get_or_create_session("op", "blackbox", "desktop")
        a.status = "running"
        b = gsm.get_or_create_session("op", "blackbox", "desktop",
                                      force_new=True)
        assert b.session_id != a.session_id
        assert a.session_id in gsm._sessions
        assert gsm._operator_sessions["op"] == b.session_id
    finally:
        gsm._sessions.clear()
        gsm._operator_sessions.clear()


def test_cu_status_resolves_an_explicit_session_id():
    a = bsm.get_or_create_session("op")
    a.status = "running"
    a.current_step = 7
    b = bsm.get_or_create_session("op", force_new=True)
    from Orchestrator.routes.chat_routes import resolve_cu_session
    hit = resolve_cu_session("op", a.session_id, "")
    assert hit is a                                # not the MRU (b)
