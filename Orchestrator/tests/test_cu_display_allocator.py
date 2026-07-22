"""M9: per-session DisplayAllocator — allocate/spawn (mocked)/teardown by pid,
per-backend native resolution, concurrency cap. No global pkill/pgrep anywhere.

Spawn is mocked (no real Xvfb), so these run headless in CI. Behavior, not mocks:
the allocator's own bookkeeping (slots, ports, resolution, pid tracking) is real.
"""
import itertools
import time
import pytest

from Orchestrator.browser import display as disp


class _FakePopen:
    _ids = itertools.count(1000)

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.pid = next(self._ids)
        self._alive = True
        self.args = cmd

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0

    def kill(self):
        self._alive = False


@pytest.fixture
def alloc(monkeypatch):
    # Mock every spawn + the readiness probe so no real X server is touched, and
    # stub the startup sleeps so the suite stays fast.
    monkeypatch.setattr(disp.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(disp.time, "sleep", lambda *_: None)
    monkeypatch.setattr(disp, "_xvfb_ready", lambda display_num: True)
    monkeypatch.setattr(disp, "_live_view_available", lambda: True)
    a = disp.DisplayAllocator()
    yield a
    a.shutdown_all()


def test_allocate_assigns_slot0_ports_and_anthropic_resolution(alloc):
    h = alloc.allocate("sess-a", backend="anthropic", operator="Brandon")
    assert h.slot == 0
    assert h.display == ":100"
    assert h.vnc_port == 5901
    assert h.ws_port == 6101
    assert (h.width, h.height) == (1280, 720)
    assert set(h.pids) >= {"xvfb", "openbox", "x11vnc"}  # websockify too when live view available
    assert h.pids["websockify"]


def test_gemini_backend_gets_1440x900(alloc):
    h = alloc.allocate("sess-g", backend="google", operator="op")
    assert (h.width, h.height) == (1440, 900)


def test_openai_backend_gets_1280x720(alloc):
    h = alloc.allocate("sess-o", backend="openai", operator="op")
    assert (h.width, h.height) == (1280, 720)


def test_second_session_gets_slot1_distinct_ports_and_display(alloc):
    a = alloc.allocate("s1", backend="anthropic", operator="op")
    b = alloc.allocate("s2", backend="google", operator="op")
    assert (b.slot, b.display, b.vnc_port, b.ws_port) == (1, ":101", 5902, 6102)
    assert a.display != b.display


def test_allocate_is_idempotent_per_session(alloc):
    a = alloc.allocate("dup", backend="anthropic", operator="op")
    b = alloc.allocate("dup", backend="anthropic", operator="op")
    assert a is b  # same handle, no second quartet


def test_release_terminates_all_tracked_pids_and_frees_slot(alloc):
    h = alloc.allocate("sess-r", backend="anthropic", operator="op")
    procs = list(alloc._procs["sess-r"].values())
    alloc.release("sess-r")
    assert all(p.poll() is not None for p in procs)   # every child terminated by pid
    assert "sess-r" not in alloc._sessions
    # slot 0 is now free — next allocate reuses it
    h2 = alloc.allocate("sess-r2", backend="anthropic", operator="op")
    assert h2.slot == 0


def test_release_keeps_slot_reserved_until_teardown_completes(alloc):
    """Race guard: the slot (and its display_num/rfbport) must not be reclaimable
    until the old children are down. We prove the slot is still reserved at the
    moment teardown runs by having a proc's terminate() re-enter allocate()."""
    h = alloc.allocate("sess-race", backend="anthropic", operator="op")
    reused_during_teardown = {}

    xvfb = alloc._procs["sess-race"]["xvfb"]
    orig_terminate = xvfb.terminate

    def terminate_and_probe():
        # While the old Xvfb on :100 is being torn down, a contender allocates.
        # It must land on slot 1 (:101), never reuse the still-reserved slot 0.
        b = alloc.allocate("contender", backend="anthropic", operator="op")
        reused_during_teardown["slot"] = b.slot
        reused_during_teardown["display"] = b.display
        orig_terminate()

    xvfb.terminate = terminate_and_probe
    alloc.release("sess-race")

    assert reused_during_teardown["slot"] == 1
    assert reused_during_teardown["display"] == ":101"
    # slot 0 is free again only after teardown returned
    assert 0 not in alloc._slots


def test_reap_idle_sweeps_stale_and_reflects_live_set(alloc):
    fresh = alloc.allocate("fresh", backend="anthropic", operator="op")
    stale = alloc.allocate("stale", backend="anthropic", operator="op")
    stale_procs = list(alloc._procs["stale"].values())
    # Age the stale session well past the TTL.
    stale.last_activity = time.time() - (disp.VIRTUAL_DISPLAY_TTL + 60)

    swept = alloc.reap_idle()

    assert swept == ["stale"]
    assert all(p.poll() is not None for p in stale_procs)  # its children were torn down
    live = {s["session_id"] for s in alloc.active_sessions()}
    assert live == {"fresh"}                                # /cu/sessions reflects live set
    assert "stale" not in alloc._sessions and "fresh" in alloc._sessions


def test_reap_idle_handles_already_dead_children_gracefully(alloc):
    h = alloc.allocate("dead", backend="anthropic", operator="op")
    # Simulate the children having already exited (crash / external kill).
    for p in alloc._procs["dead"].values():
        p._alive = False
    h.last_activity = time.time() - (disp.VIRTUAL_DISPLAY_TTL + 1)

    swept = alloc.reap_idle()  # must not raise on already-dead pids

    assert swept == ["dead"]
    assert alloc.active_sessions() == []


def test_reap_idle_keeps_recent_sessions(alloc):
    alloc.allocate("recent", backend="anthropic", operator="op")
    assert alloc.reap_idle() == []
    assert {s["session_id"] for s in alloc.active_sessions()} == {"recent"}


def test_active_sessions_reflects_allocate_and_release(alloc):
    alloc.allocate("s1", backend="anthropic", operator="op")
    alloc.allocate("s2", backend="google", operator="op")
    assert {s["session_id"] for s in alloc.active_sessions()} == {"s1", "s2"}
    alloc.release("s1")
    remaining = alloc.active_sessions()
    assert {s["session_id"] for s in remaining} == {"s2"}


def test_to_public_shape_and_view_url(alloc):
    h = alloc.allocate("pub", backend="google", operator="Brandon")
    pub = h.to_public()
    assert set(pub) >= {"session_id", "operator", "backend", "width", "height",
                        "display", "live_view", "view_url", "started_at"}
    assert pub["view_url"] == "/cu/view/pub"
    assert (pub["width"], pub["height"]) == (1440, 900)
    assert pub["operator"] == "Brandon"


def test_cu_sessions_endpoint_reflects_live_set(monkeypatch):
    """GET /cu/sessions (the badge source) returns the allocator's live set +
    cap. Exercised against the module singleton with all spawns mocked."""
    monkeypatch.setattr(disp.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(disp.time, "sleep", lambda *_: None)
    monkeypatch.setattr(disp, "_xvfb_ready", lambda display_num: True)
    monkeypatch.setattr(disp, "_live_view_available", lambda: True)
    from Orchestrator.routes.browser_routes import cu_sessions

    a = disp.get_allocator()
    for sid in list(a._sessions):  # start from a clean singleton
        a.release(sid)
    try:
        assert cu_sessions() == {"sessions": [], "count": 0,
                                 "cap": disp.MAX_VIRTUAL_SESSIONS}
        a.allocate("badge-1", backend="anthropic", operator="op")
        a.allocate("badge-2", backend="google", operator="op")
        resp = cu_sessions()
        assert resp["count"] == 2
        assert {s["session_id"] for s in resp["sessions"]} == {"badge-1", "badge-2"}
        a.release("badge-1")
        assert cu_sessions()["count"] == 1
    finally:
        for sid in list(a._sessions):
            a.release(sid)


def test_fourth_concurrent_session_raises_cap(alloc):
    for i in range(disp.MAX_VIRTUAL_SESSIONS):
        alloc.allocate(f"s{i}", backend="anthropic", operator="op")
    with pytest.raises(RuntimeError, match="cap reached"):
        alloc.allocate("s-over", backend="anthropic", operator="op")


def test_reap_idle_releases_only_stale_sessions(alloc, monkeypatch):
    a = alloc.allocate("fresh", backend="anthropic", operator="op")
    b = alloc.allocate("stale", backend="anthropic", operator="op")
    b.last_activity = 0.0  # ancient
    alloc.reap_idle()
    assert "fresh" in alloc._sessions
    assert "stale" not in alloc._sessions


def test_reap_orphans_kills_untracked_slot_survivors(alloc, monkeypatch):
    # Simulate a restart-survivor Xvfb on slot 2 with pid 4242 that we do NOT track.
    killed = []
    monkeypatch.setattr(disp, "_terminate_pid", lambda pid: killed.append(pid))
    def fake_matching(pattern):
        return [4242] if "Xvfb :102" in pattern else []
    monkeypatch.setattr(disp, "_pids_matching", fake_matching)
    alloc.reap_orphans()
    assert 4242 in killed


def test_reap_orphans_spares_tracked_pids(alloc, monkeypatch):
    h = alloc.allocate("live", backend="anthropic", operator="op")  # slot 0 -> :100
    tracked_xvfb = alloc._procs["live"]["xvfb"].pid
    killed = []
    monkeypatch.setattr(disp, "_terminate_pid", lambda pid: killed.append(pid))
    monkeypatch.setattr(disp, "_pids_matching",
                        lambda pattern: [tracked_xvfb] if "Xvfb :100" in pattern else [])
    alloc.reap_orphans()
    assert tracked_xvfb not in killed  # never kill a pid we own


def test_no_global_process_kill_in_source():
    """Guard: the rewritten module must never shell out to a process-name matcher.
    pkill kills ALL sessions; pgrep -f <role> false-positives across sessions.
    Teardown is pid-tracked (Popen objects) — there is no pgrep/pkill at all."""
    import inspect
    src = inspect.getsource(disp)
    assert "pkill" not in src
    assert "pgrep" not in src  # no process-name discovery of any kind
