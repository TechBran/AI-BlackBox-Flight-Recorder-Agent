"""M9: per-session DisplayAllocator — allocate/spawn (mocked)/teardown by pid,
per-backend native resolution, concurrency cap. No global pkill/pgrep anywhere.

Spawn is mocked (no real Xvfb), so these run headless in CI. Behavior, not mocks:
the allocator's own bookkeeping (slots, ports, resolution, pid tracking) is real.
"""
import itertools
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


def test_no_global_process_kill_in_source():
    """Guard: the rewritten module must never shell out to a blanket pkill/pgrep."""
    import inspect
    src = inspect.getsource(disp)
    assert "pkill" not in src
    assert "pgrep -f x11vnc" not in src and "pgrep -f openbox" not in src
