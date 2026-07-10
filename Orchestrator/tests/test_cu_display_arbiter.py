"""M1-T6: shared-layer display arbitration across the THREE CU session kinds.

The BlackBox drives ONE physical local X display. Three kinds of CU session can
contend for it, spread across TWO registries:

  * browser ComputerUseSession   — browser/session_manager.py (Anthropic + OpenAI)
  * Gemini CHAT session          — gemini_cu/session_manager.py (_operator_sessions)
  * Gemini TASK session          — gemini_cu/session_manager.py (_sessions ONLY)

Before M1-T6 no single guard saw all three (browser lock saw only browser
sessions; the Gemini guards saw only the browser session). These tests pin the
shared `browser.display_arbiter.local_display_owner` and its integration at every
guard site, and — crucially — the two directions that were UNGUARDED before:

  (a) a browser CU session must refuse to start while a Gemini DESKTOP session is
      driving the display (previously the Anthropic/headless browser path never
      checked the Gemini registry);
  (b) a Gemini DESKTOP task must refuse to co-drive with another Gemini DESKTOP
      session — same OR different operator (the old accidental mutual-exclusion
      via per-operator caching was removed by the task-session isolation fix).

Real sessions are registered in the real registries (behavior, not mocks); the
autouse fixture keeps BOTH registries empty around every test so the shared
arbiter can never report a phantom owner leaked from another test.
"""
import asyncio

import pytest

from Orchestrator.browser import session_manager as bsm
from Orchestrator.browser.session_manager import ComputerUseSession
from Orchestrator.browser.display_arbiter import local_display_owner, DisplayOwner
from Orchestrator.gemini_cu import session_manager as gsm
from Orchestrator.browser import headless
from Orchestrator.browser import display_arbiter as da


@pytest.fixture(autouse=True)
def _clean_registries(monkeypatch):
    """Both CU registries AND the reservation table start and end EMPTY, and a
    constructed browser session reports alive without a real Chrome/display so
    lookups are deterministic regardless of NATIVE_MODE."""
    monkeypatch.setattr(ComputerUseSession, "is_alive", lambda self: True)
    monkeypatch.setattr(ComputerUseSession, "destroy", lambda self: None)
    for reg in (bsm._sessions, bsm._operator_sessions, gsm._sessions, gsm._operator_sessions):
        reg.clear()
    da._reservations.clear()
    yield
    for reg in (bsm._sessions, bsm._operator_sessions, gsm._sessions, gsm._operator_sessions):
        reg.clear()
    da._reservations.clear()


# ── helpers: register REAL sessions in each registry ────────────────────────
def _register_browser(operator, status="running", device_id="blackbox"):
    s = ComputerUseSession(operator, device_id=device_id)
    s.status = status
    bsm._sessions[s.session_id] = s
    bsm._operator_sessions[operator] = s.session_id
    return s


def _register_gemini_chat(operator, status="running", environment="desktop", device_id="blackbox"):
    s = gsm.get_or_create_session(operator, device_id, environment)  # registers in BOTH dicts
    s.status = status
    return s


def _register_gemini_task(operator, status="running", environment="desktop", device_id="blackbox"):
    s = gsm.create_task_session(operator, device_id, environment)    # registers in _sessions ONLY
    s.status = status
    return s


# ═══════════════════════════════════════════════════════════════════════════
# Arbiter unit behavior
# ═══════════════════════════════════════════════════════════════════════════

def test_empty_registries_display_is_free():
    assert local_display_owner() is None


def test_running_browser_owns_display():
    s = _register_browser("alice", status="running")
    owner = local_display_owner()
    assert owner == DisplayOwner("browser", "alice", s.session_id)


def test_running_gemini_chat_owns_display():
    s = _register_gemini_chat("bob", status="running")
    owner = local_display_owner()
    assert owner is not None
    assert owner.kind == "gemini-chat" and owner.operator == "bob"
    assert owner.session_id == s.session_id


def test_running_gemini_task_owns_display():
    s = _register_gemini_task("carol", status="running")
    owner = local_display_owner()
    assert owner is not None
    assert owner.kind == "gemini-task" and owner.session_id == s.session_id


def test_only_running_sessions_own_the_display():
    # idle/complete/starting are NOT display-holders (matches the status every
    # pre-M1-T6 guard already keyed on; "starting" widening is out of scope).
    _register_browser("alice", status="idle")
    _register_gemini_chat("bob", status="complete")
    _register_gemini_task("carol", status="starting")
    assert local_display_owner() is None


def test_android_gemini_never_owns_the_local_display():
    _register_gemini_task("carol", status="running", environment="android", device_id="pixel-9")
    _register_gemini_chat("bob", status="running", environment="android", device_id="pixel-9")
    assert local_display_owner() is None


def test_remote_browser_never_owns_the_local_display():
    _register_browser("alice", status="running", device_id="desk-vnc-1")
    assert local_display_owner() is None


def test_exclude_session_ids_skips_self():
    s = _register_gemini_chat("bob", status="running")
    assert local_display_owner() is not None
    assert local_display_owner(exclude_session_ids=frozenset({s.session_id})) is None


def test_describe_contains_running_and_computer_use():
    _register_gemini_task("carol", status="running")
    msg = local_display_owner().describe()
    assert "running" in msg.lower() and "Computer Use" in msg


# ═══════════════════════════════════════════════════════════════════════════
# Integration — browser lock (browser/session_manager.get_or_create_session)
# ═══════════════════════════════════════════════════════════════════════════

def test_gemini_desktop_task_blocks_new_browser_session():
    """(a) reverse direction — was UNGUARDED. A running Gemini desktop TASK must
    make the browser lock refuse a new browser session (RuntimeError shape)."""
    _register_gemini_task("gem-op", status="running", environment="desktop")
    with pytest.raises(RuntimeError) as ei:
        bsm.get_or_create_session("brandon", device_id="blackbox")
    assert "Computer Use" in str(ei.value)


def test_gemini_desktop_chat_blocks_new_browser_session_same_operator():
    """(a) even for the SAME operator: a browser CU task and a Gemini desktop
    chat are different drivers — both would grab the one mouse."""
    _register_gemini_chat("brandon", status="running", environment="desktop")
    with pytest.raises(RuntimeError):
        bsm.get_or_create_session("brandon", device_id="blackbox")


def test_running_browser_blocks_new_browser_session_other_operator():
    """Existing browser-vs-browser semantics preserved through the arbiter."""
    _register_browser("alice", status="running")
    with pytest.raises(RuntimeError):
        bsm.get_or_create_session("brandon", device_id="blackbox")


def test_android_gemini_does_not_block_new_browser_desktop_session():
    _register_gemini_task("gem-op", status="running", environment="android", device_id="pixel-9")
    s = bsm.get_or_create_session("brandon", device_id="blackbox")  # must NOT raise
    assert s.operator == "brandon"


def test_remote_browser_request_not_blocked_by_local_gemini_desktop():
    """A REMOTE (non-blackbox) browser request does not touch the local X server,
    so a local Gemini desktop holder must not block it."""
    _register_gemini_chat("gem-op", status="running", environment="desktop")
    s = bsm.get_or_create_session("brandon", device_id="desk-vnc-1")
    assert s.device_id == "desk-vnc-1"


def test_resume_own_running_browser_session_no_self_block():
    """Reentrancy: resuming the operator's OWN session returns it BEFORE the lock
    — a running session must never block itself."""
    s1 = bsm.get_or_create_session("brandon", device_id="blackbox")
    s1.status = "running"
    s2 = bsm.get_or_create_session("brandon", device_id="blackbox")
    assert s2 is s1


def test_idle_gemini_desktop_does_not_block_new_browser_session():
    _register_gemini_chat("gem-op", status="idle", environment="desktop")
    s = bsm.get_or_create_session("brandon", device_id="blackbox")
    assert s.operator == "brandon"


# ═══════════════════════════════════════════════════════════════════════════
# Integration — Gemini headless guard (browser/headless._run_gemini_cu_task)
# ═══════════════════════════════════════════════════════════════════════════

def _stub_headless_no_launch(monkeypatch):
    """Make the Gemini headless path fail LOUD if it ever gets past the guard."""
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake-google-key")

    def _no_session(*a, **k):
        raise AssertionError("Gemini task session created despite a display conflict")

    def _no_driver(*a, **k):
        raise AssertionError("Gemini driver launched despite a display conflict")

    monkeypatch.setattr(headless, "gemini_create_task_session", _no_session)
    monkeypatch.setattr(headless, "run_gemini_cu_loop", _no_driver)


@pytest.mark.asyncio
async def test_gemini_task_blocked_by_running_gemini_task_same_operator(monkeypatch):
    """(b) same operator — the old accidental caching mutual-exclusion is gone, so
    the shared arbiter must now refuse the second Gemini desktop task."""
    _stub_headless_no_launch(monkeypatch)
    _register_gemini_task("brandon", status="running", environment="desktop")
    result = await headless.run_cu_task(
        "t-b-same", "brandon", "hi", model="gemini-2.5-computer-use-preview-10-2025")
    assert result["success"] is False
    assert "running" in result["result_text"].lower()


@pytest.mark.asyncio
async def test_gemini_task_blocked_by_running_gemini_task_different_operator(monkeypatch):
    """(b) DIFFERENT operator — two operators' Gemini desktop tasks would co-drive
    the one physical display; refuse."""
    _stub_headless_no_launch(monkeypatch)
    _register_gemini_task("alice", status="running", environment="desktop")
    result = await headless.run_cu_task(
        "t-b-diff", "brandon", "hi", model="gemini-2.5-computer-use-preview-10-2025")
    assert result["success"] is False
    assert "running" in result["result_text"].lower()


@pytest.mark.asyncio
async def test_gemini_task_blocked_by_running_browser_cu(monkeypatch):
    """A running browser (Anthropic/OpenAI) desktop CU session blocks a Gemini
    desktop task — the pre-existing guarantee, now proven through the arbiter."""
    _stub_headless_no_launch(monkeypatch)
    _register_browser("brandon", status="running")
    result = await headless.run_cu_task(
        "t-brws-blocks-gem", "brandon", "hi",
        model="gemini-2.5-computer-use-preview-10-2025")
    assert result["success"] is False
    assert "Computer Use" in result["result_text"]


@pytest.mark.asyncio
async def test_gemini_task_not_blocked_when_display_free(monkeypatch):
    """Sanity: with the display free, the guard passes and the driver launches."""
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake-google-key")
    launched = {}

    async def fake_loop(session, *a, **k):
        launched["yes"] = True
        session.current_step = 1
        yield {"type": "done", "data": {"content": "ran"}}

    monkeypatch.setattr(headless, "run_gemini_cu_loop", fake_loop)
    result = await headless.run_cu_task(
        "t-free", "brandon", "hi", model="gemini-2.5-computer-use-preview-10-2025")
    assert result["success"] is True
    assert launched.get("yes") is True


# ═══════════════════════════════════════════════════════════════════════════
# Integration — Gemini chat guard (chat_routes.stream_gemini_computer_use)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_gemini_chat_guard_blocks_on_conflict(monkeypatch):
    """A different operator's running Gemini desktop task blocks a new Gemini chat
    desktop turn — the guard yields an SSE error event (its unchanged shape)."""
    from Orchestrator.routes import chat_routes as cr
    monkeypatch.setattr("Orchestrator.config.GOOGLE_API_KEY", "fake", raising=False)
    _register_gemini_task("other-op", status="running", environment="desktop")

    first = None
    async for ev in cr.stream_gemini_computer_use(
            [{"role": "user", "content": "do it"}], "", "brandon", device_id="blackbox"):
        first = ev
        break
    assert first is not None
    assert first["type"] == "error"
    assert "Cannot start Gemini CU" in first["data"]


@pytest.mark.asyncio
async def test_gemini_chat_guard_excludes_own_chat_session(monkeypatch):
    """Reentrancy: the operator's OWN running Gemini chat session must NOT block a
    reconnect/queue to it. The guard excludes that session id, so it passes and
    reaches gemini_get_or_create (proven by a sentinel), never yielding a conflict
    error."""
    from Orchestrator.routes import chat_routes as cr
    import Orchestrator.gemini_cu as gemini_pkg
    monkeypatch.setattr("Orchestrator.config.GOOGLE_API_KEY", "fake", raising=False)

    _register_gemini_chat("brandon", status="running", environment="desktop")

    class _Reached(Exception):
        pass

    def _sentinel(*a, **k):
        raise _Reached()

    monkeypatch.setattr(gemini_pkg, "get_or_create_session", _sentinel)

    with pytest.raises(_Reached):
        async for ev in cr.stream_gemini_computer_use(
                [{"role": "user", "content": "resume"}], "", "brandon", device_id="blackbox"):
            assert ev.get("type") != "error", "guard wrongly blocked the operator's own session"


# ═══════════════════════════════════════════════════════════════════════════
# Reservation lifecycle (M1-T6 review Issue 1: atomic check-and-reserve)
# ═══════════════════════════════════════════════════════════════════════════

def test_try_claim_reserves_and_is_visible_before_any_session_exists():
    """The whole point: a reservation makes the display "taken" in the gap BEFORE
    a session is registered / running. No session exists here, yet the display is
    owned."""
    assert da.try_claim("gemini-task", "op", "task-1") is None      # claimed
    owner = da.local_display_owner()
    assert owner is not None and owner.kind == "gemini-task" and owner.operator == "op"


def test_second_claim_is_denied_then_freed_by_release():
    assert da.try_claim("gemini-task", "op1", "task-1") is None       # first wins
    denied = da.try_claim("browser", "op2", "sid-2")                  # second denied
    assert denied is not None and denied.session_id == "task-1"
    da.release_claim("task-1")                                        # free it
    assert da.try_claim("browser", "op2", "sid-2") is None            # now granted


def test_release_claim_is_idempotent_and_safe_for_unknown_id():
    da.release_claim("never-claimed")   # must not raise
    assert da.local_display_owner() is None


def test_reservation_pruned_once_its_session_is_running():
    """A browser reservation is redundant once its session is visible as running
    via the scan — prune drops it, and the scan still reports the owner."""
    s = _register_browser("alice", status="idle")
    da._reservations[s.session_id] = (
        da.DisplayOwner("browser", "alice", s.session_id), __import__("time").monotonic())
    s.status = "running"
    owner = da.local_display_owner()                 # triggers prune
    assert s.session_id not in da._reservations       # redundant reservation dropped
    assert owner is not None and owner.session_id == s.session_id  # scan still owns it


def test_reservation_pruned_once_its_session_is_terminal():
    """After the task ends (complete), the reservation is reclaimed so the next
    task is not wedged — even though the persistent chat session lingers."""
    s = _register_gemini_chat("bob", status="running")
    da._reservations["gemini-chat:bob"] = (
        da.DisplayOwner("gemini-chat", "bob", "gemini-chat:bob"), __import__("time").monotonic())
    s.status = "complete"
    assert da.local_display_owner() is None            # display freed
    assert "gemini-chat:bob" not in da._reservations


def test_leaked_reservation_expires_by_ttl(monkeypatch):
    """The named primary hazard: a leaked reservation (no session, never released)
    must not wedge the display forever. It expires after the TTL."""
    monkeypatch.setattr(da, "_RESERVATION_TTL", 0.05)
    assert da.try_claim("gemini-task", "op", "leaked") is None
    assert da.local_display_owner() is not None        # held now
    __import__("time").sleep(0.06)
    assert da.local_display_owner() is None             # reclaimed by TTL


def test_browser_reservation_released_on_session_destruction():
    """_cleanup_session releases the reservation keyed by the session id."""
    s = bsm.get_or_create_session("brandon", device_id="blackbox")   # claims
    assert da.local_display_owner() is not None
    bsm._cleanup_session(s.session_id)                                # destroy
    assert s.session_id not in da._reservations


def test_gemini_chat_reservation_released_on_destroy_session():
    da.try_claim("gemini-chat", "brandon", "gemini-chat:brandon")
    assert "gemini-chat:brandon" in da._reservations
    gsm.destroy_session("brandon")
    assert "gemini-chat:brandon" not in da._reservations


@pytest.mark.asyncio
async def test_android_gemini_task_not_blocked_by_busy_local_desktop(monkeypatch):
    """"Vice versa": a running browser desktop CU session (holds the LOCAL display)
    must NOT block a Gemini ANDROID headless task — the guard is skipped for
    environment != desktop, so android neither claims nor is blocked."""
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake-google-key")
    _register_browser("someone", status="running")   # LOCAL desktop is busy

    launched = {}

    async def fake_loop(session, *a, **k):
        launched["yes"] = True
        session.current_step = 1
        yield {"type": "done", "data": {"content": "ran on android"}}

    monkeypatch.setattr(headless, "run_gemini_cu_loop", fake_loop)

    # Resolve an ADB device so environment == "android".
    from types import SimpleNamespace
    from Orchestrator import device_registry as dreg

    class _FakeReg:
        def get_device(self, did):
            return SimpleNamespace(protocol=dreg.DeviceProtocol.ADB, name="Pixel", id=did)

    monkeypatch.setattr(dreg, "get_registry", lambda: _FakeReg())

    async def _ok(did):
        return {"success": True}

    import Orchestrator.adb as adb_mod
    monkeypatch.setattr(adb_mod, "get_adb_manager", lambda: SimpleNamespace(ensure_connected=_ok))

    result = await headless.run_cu_task(
        "t-android", "gem-op", "tap", device_id="pixel-9",
        model="gemini-2.5-computer-use-preview-10-2025")
    # android task ran despite a busy LOCAL desktop — not blocked, did not claim
    assert result["success"] is True
    assert launched.get("yes") is True
    assert not da._reservations   # android claimed nothing


# ═══════════════════════════════════════════════════════════════════════════
# The race, directly (M1-T6 review Issue 1): two OS threads, exactly one wins
# ═══════════════════════════════════════════════════════════════════════════

def test_two_threads_contending_exactly_one_claims(monkeypatch):
    """Two real OS threads (the tasks.py ThreadPoolExecutor model) both attempt to
    take the display at once. A Barrier lines them up; a stall injected BETWEEN
    the free-check and the reservation-record (the exact window the lock must
    serialize) forces their critical sections to overlap. Under the arbiter's
    RLock exactly ONE claim succeeds. Mutation-verified separately: with the lock
    removed BOTH succeed (see the M1-T6 report)."""
    import threading
    import time as _t

    # Stall between "display is free" and "record reservation": the winner holds
    # the lock across this stall, so the loser blocks on the lock and, on
    # acquiring, sees the recorded reservation. Without the lock, both would race
    # through the stall and both record.
    monkeypatch.setattr(da, "_before_record_hook", lambda: _t.sleep(0.15))

    barrier = threading.Barrier(2)
    results = {}

    def worker(i):
        barrier.wait()          # release both threads together
        results[i] = da.try_claim("gemini-task", f"op{i}", f"claim-{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    granted = [i for i, r in results.items() if r is None]
    denied = [i for i, r in results.items() if r is not None]
    assert len(granted) == 1, f"expected exactly one winner, got {results}"
    assert len(denied) == 1
    assert len(da._reservations) == 1       # exactly one reservation recorded
