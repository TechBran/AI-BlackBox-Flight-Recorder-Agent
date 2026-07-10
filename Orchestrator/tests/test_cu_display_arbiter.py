"""M1-T6: shared, PER-LAUNCH display arbitration across the three CU session kinds.

The BlackBox drives ONE physical local X display. Three kinds of CU session can
contend for it, across TWO registries (browser ComputerUseSession; Gemini chat;
Gemini task). A *session* is long-lived and reused across turns; a *launch* is the
thing that grabs the mouse. So reservations are keyed PER-LAUNCH (a task id / a
fresh per-turn uuid), never per-session, and each launch site claims BEFORE it
drives and releases in a finally.

These tests pin: the arbiter scan + reservation primitive, the atomic two-thread
race, leak reclaim (prune + TTL, C4-correct), and the per-launch integration at
the headless (run_cu_task) and chat (stream_*_computer_use) launch sites — the
review's C1 (reuse claims), C2 (a refused launch never drops another's claim),
C3 (chat early returns leak nothing).

Real sessions/reservations in the real registries (behavior, not mocks); the
autouse fixture keeps both registries AND the reservation table empty around
every test so the shared arbiter never reports a phantom owner.
"""
import time

import pytest

from Orchestrator.browser import session_manager as bsm
from Orchestrator.browser.session_manager import ComputerUseSession
from Orchestrator.browser import display_arbiter as da
from Orchestrator.browser.display_arbiter import local_display_owner, DisplayOwner
from Orchestrator.gemini_cu import session_manager as gsm
from Orchestrator.browser import headless
from Orchestrator.routes import chat_routes as cr


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(ComputerUseSession, "is_alive", lambda self: True)
    monkeypatch.setattr(ComputerUseSession, "destroy", lambda self: None)
    for reg in (bsm._sessions, bsm._operator_sessions, gsm._sessions, gsm._operator_sessions):
        reg.clear()
    da._reservations.clear()
    yield
    for reg in (bsm._sessions, bsm._operator_sessions, gsm._sessions, gsm._operator_sessions):
        reg.clear()
    da._reservations.clear()


# ── helpers ──────────────────────────────────────────────────────────────────
def _register_browser(operator, status="running", device_id="blackbox"):
    s = ComputerUseSession(operator, device_id=device_id)
    s.status = status
    bsm._sessions[s.session_id] = s
    bsm._operator_sessions[operator] = s.session_id
    return s


def _register_gemini_chat(operator, status="running", environment="desktop", device_id="blackbox"):
    s = gsm.get_or_create_session(operator, device_id, environment)   # BOTH dicts
    s.status = status
    return s


def _register_gemini_task(operator, status="running", environment="desktop", device_id="blackbox"):
    s = gsm.create_task_session(operator, device_id, environment)     # _sessions only
    s.status = status
    return s


def _stub_browser_run(monkeypatch):
    """Stub run_cu_task's browser seams so a launch can reach + run the (stubbed)
    driver without Xvfb/Chrome/Anthropic."""
    monkeypatch.setattr(headless, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(headless, "NATIVE_MODE", True)

    async def _ensure(self, url="about:blank"):
        return True

    monkeypatch.setattr(ComputerUseSession, "ensure_browser", _ensure)
    monkeypatch.setattr(headless, "capture_screenshot", lambda *a, **k: b"\x89PNG-fake")
    monkeypatch.setattr(headless, "screenshot_to_base64", lambda p: "ZmFrZQ==")
    monkeypatch.setattr(headless, "save_screenshot_to_uploads",
                        lambda png, ident, step: f"/ui/uploads/{ident}_{step}.png")
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(cr, "build_cu_context", lambda *a, **k: ("", {}))

    async def _instant(_s):
        return None

    monkeypatch.setattr(headless.asyncio, "sleep", _instant)


# ═══════════════════════════════════════════════════════════════════════════
# Arbiter scan
# ═══════════════════════════════════════════════════════════════════════════

def test_empty_display_is_free():
    assert local_display_owner() is None


def test_running_browser_owns_display():
    s = _register_browser("alice")
    assert local_display_owner() == DisplayOwner("browser", "alice", s.session_id)


def test_running_gemini_chat_owns_display():
    s = _register_gemini_chat("bob")
    o = local_display_owner()
    assert o.kind == "gemini-chat" and o.operator == "bob" and o.session_id == s.session_id


def test_running_gemini_task_owns_display():
    s = _register_gemini_task("carol")
    o = local_display_owner()
    assert o.kind == "gemini-task" and o.session_id == s.session_id


def test_only_running_sessions_own_the_display():
    _register_browser("alice", status="idle")
    _register_gemini_chat("bob", status="complete")
    _register_gemini_task("carol", status="starting")
    assert local_display_owner() is None


def test_android_gemini_never_owns_local_display():
    _register_gemini_task("carol", environment="android", device_id="pixel-9")
    _register_gemini_chat("bob", environment="android", device_id="pixel-9")
    assert local_display_owner() is None


def test_remote_browser_never_owns_local_display():
    _register_browser("alice", device_id="desk-vnc-1")
    assert local_display_owner() is None


def test_describe_contains_running_and_computer_use():
    _register_gemini_task("carol")
    assert "Computer Use" in local_display_owner().describe()


# ═══════════════════════════════════════════════════════════════════════════
# Reservation primitive (per-launch keys)
# ═══════════════════════════════════════════════════════════════════════════

def test_reservation_visible_before_any_session_exists():
    assert da.try_claim("gemini-task", "op", "task-1") is None   # claimed
    o = local_display_owner()
    assert o is not None and o.kind == "gemini-task" and o.operator == "op"


def test_second_claim_denied_then_freed_by_release():
    assert da.try_claim("gemini-task", "op1", "task-1") is None
    denied = da.try_claim("browser", "op2", "task-2")
    assert denied is not None and denied.session_id == "task-1"
    da.release_claim("task-1")
    assert da.try_claim("browser", "op2", "task-2") is None


def test_release_claim_idempotent():
    da.release_claim("never-claimed")               # no raise
    assert local_display_owner() is None


def test_claim_context_manager_grants_and_releases():
    with da.claim_local_display("gemini-task", "op", "c1") as owner:
        assert owner is None                         # granted
        assert local_display_owner() is not None     # held inside
    assert local_display_owner() is None             # released on exit


def test_claim_context_manager_denied_does_not_release_holder():
    da.try_claim("gemini-task", "op1", "held")       # someone holds it
    with da.claim_local_display("browser", "op2", "mine") as owner:
        assert owner is not None                     # denied
    # exiting the DENIED context must NOT release op1's reservation
    assert "held" in da._reservations


def test_prune_reclaims_reservation_once_session_running():
    s = _register_browser("alice", status="idle")
    da.try_claim("browser", "alice", "launch-1", session_id=s.session_id)
    s.status = "running"
    owner = local_display_owner()                    # triggers prune
    assert "launch-1" not in da._reservations         # redundant → dropped
    assert owner is not None and owner.session_id == s.session_id  # scan still owns


def test_prune_reclaims_reservation_once_session_terminal():
    s = _register_gemini_chat("bob", status="running")
    da.try_claim("gemini-chat", "bob", "launch-2", session_id=s.session_id)
    s.status = "complete"
    assert local_display_owner() is None             # display freed
    assert "launch-2" not in da._reservations


def test_C4_ttl_does_not_drop_a_still_starting_session():
    """C4: a reservation older than the TTL whose recorded session still exists
    and is only `starting` must STILL hold the display — the state check runs
    before the TTL branch."""
    s = _register_browser("alice", status="starting")
    da.try_claim("browser", "alice", "launch-3", session_id=s.session_id)
    owner, _ts = da._reservations["launch-3"]
    da._reservations["launch-3"] = (owner, time.monotonic() - (da._RESERVATION_TTL + 5))
    held = local_display_owner()                     # triggers prune
    assert "launch-3" in da._reservations             # NOT TTL-dropped
    assert held is not None and held.session_id == s.session_id


def test_leaked_reservation_with_no_session_expires_by_ttl(monkeypatch):
    monkeypatch.setattr(da, "_RESERVATION_TTL", 0.05)
    assert da.try_claim("gemini-task", "op", "leaked") is None    # no real session
    assert local_display_owner() is not None
    time.sleep(0.06)
    assert local_display_owner() is None             # reclaimed by TTL


# ═══════════════════════════════════════════════════════════════════════════
# The race, directly: two OS threads, exactly one wins
# ═══════════════════════════════════════════════════════════════════════════

def test_two_threads_contending_exactly_one_claims(monkeypatch):
    """Two OS threads (the tasks.py ThreadPoolExecutor model) both try to take the
    display at once. A Barrier lines them up; a stall injected BETWEEN the free
    check and the record (via the _before_record_hook seam) forces their critical
    sections to overlap. Under the RLock exactly ONE wins. Mutation-verified in the
    M1-T6 report: remove the lock and both win."""
    import threading

    monkeypatch.setattr(da, "_before_record_hook", lambda: time.sleep(0.15))
    barrier = threading.Barrier(2)
    results = {}

    def worker(i):
        barrier.wait()
        results[i] = da.try_claim("gemini-task", f"op{i}", f"claim-{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    granted = [i for i, r in results.items() if r is None]
    assert len(granted) == 1, f"expected exactly one winner, got {results}"
    assert len(da._reservations) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Headless launch integration (browser/headless.run_cu_task)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_C1_browser_reuse_launch_claims_the_display(monkeypatch):
    """C1: a REUSED browser session's launch must claim the display. Pre-register
    an idle browser session, run a browser CU task (which REUSES it), and prove
    that WHILE its driver runs a concurrent cross-kind claim is REFUSED.
    Mutation-verified in the report: drop run_cu_task's try_claim and it is
    GRANTED."""
    _stub_browser_run(monkeypatch)
    _register_browser("op", status="idle")           # reuse target
    observed = {}

    async def driver(session, history, system_prompt, tools, headers, model, operator, user_text):
        observed["blocked"] = da.try_claim("gemini-task", "op2", "g-concurrent") is not None
        da.release_claim("g-concurrent")
        session.current_step = 1
        await session.event_queue.put({"type": "done", "data": {"thinking": "", "content": "ok"}})
        await session.event_queue.put(None)

    monkeypatch.setattr(headless, "run_anthropic_cu_loop", driver)

    result = await headless.run_cu_task("t-reuse", "op", "hi")   # reuses the idle session
    assert result["success"] is True
    assert observed["blocked"] is True                # the reuse launch had claimed
    assert not da._reservations                        # released in finally


@pytest.mark.asyncio
async def test_C2_refused_task_launch_does_not_drop_another_launchs_claim(monkeypatch):
    """C2: per-launch keys make cross-release impossible. A live launch holds a
    claim; a same-operator task launch is REFUSED and its finally must release
    only ITS OWN (never-recorded) key — the live claim survives."""
    monkeypatch.setattr(headless, "ANTHROPIC_API_KEY", "test-key")
    da.try_claim("browser", "op", "live-chat-launch", session_id="sid-live")

    result = await headless.run_cu_task("t-refused", "op", "hi",
                                        model="claude-opus-4-6")
    assert result["success"] is False
    assert "Computer Use" in result["result_text"]
    assert "live-chat-launch" in da._reservations      # the OTHER launch survived


@pytest.mark.asyncio
async def test_gemini_task_blocked_by_running_gemini_task_same_operator(monkeypatch):
    """(b) same operator — the second Gemini desktop task is refused."""
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake")

    def _boom(*a, **k):
        raise AssertionError("launched despite conflict")

    monkeypatch.setattr(headless, "gemini_create_task_session", _boom)
    monkeypatch.setattr(headless, "run_gemini_cu_loop", _boom)
    _register_gemini_task("brandon", status="running")
    result = await headless.run_cu_task("t-b-same", "brandon", "hi",
                                        model="gemini-2.5-computer-use-preview-10-2025")
    assert result["success"] is False and "running" in result["result_text"].lower()


@pytest.mark.asyncio
async def test_gemini_task_blocked_by_running_gemini_task_different_operator(monkeypatch):
    """(b) different operator — two operators' Gemini desktop tasks are refused."""
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake")

    def _boom(*a, **k):
        raise AssertionError("launched despite conflict")

    monkeypatch.setattr(headless, "gemini_create_task_session", _boom)
    monkeypatch.setattr(headless, "run_gemini_cu_loop", _boom)
    _register_gemini_task("alice", status="running")
    result = await headless.run_cu_task("t-b-diff", "brandon", "hi",
                                        model="gemini-2.5-computer-use-preview-10-2025")
    assert result["success"] is False and "running" in result["result_text"].lower()


@pytest.mark.asyncio
async def test_gemini_task_blocked_by_running_browser_cu(monkeypatch):
    """A running browser desktop CU session blocks a Gemini desktop task."""
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake")

    def _boom(*a, **k):
        raise AssertionError("launched despite conflict")

    monkeypatch.setattr(headless, "gemini_create_task_session", _boom)
    monkeypatch.setattr(headless, "run_gemini_cu_loop", _boom)
    _register_browser("brandon", status="running")
    result = await headless.run_cu_task("t-brws", "brandon", "hi",
                                        model="gemini-2.5-computer-use-preview-10-2025")
    assert result["success"] is False and "Computer Use" in result["result_text"]


@pytest.mark.asyncio
async def test_gemini_task_not_blocked_when_free(monkeypatch):
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake")
    launched = {}

    async def fake_loop(session, *a, **k):
        launched["yes"] = True
        session.current_step = 1
        yield {"type": "done", "data": {"content": "ran"}}

    monkeypatch.setattr(headless, "run_gemini_cu_loop", fake_loop)
    result = await headless.run_cu_task("t-free", "brandon", "hi",
                                        model="gemini-2.5-computer-use-preview-10-2025")
    assert result["success"] is True and launched.get("yes") is True
    assert not da._reservations                        # released in finally


@pytest.mark.asyncio
async def test_android_gemini_task_not_blocked_by_busy_local_desktop(monkeypatch):
    """"Vice versa": a busy LOCAL desktop must not block a Gemini ANDROID task —
    the guard is skipped for environment != desktop; android never claims."""
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake")
    _register_browser("someone", status="running")    # local desktop busy
    launched = {}

    async def fake_loop(session, *a, **k):
        launched["yes"] = True
        session.current_step = 1
        yield {"type": "done", "data": {"content": "tapped"}}

    monkeypatch.setattr(headless, "run_gemini_cu_loop", fake_loop)

    from types import SimpleNamespace
    from Orchestrator import device_registry as dreg

    class _Reg:
        def get_device(self, did):
            return SimpleNamespace(protocol=dreg.DeviceProtocol.ADB, name="Pixel", id=did)

    monkeypatch.setattr(dreg, "get_registry", lambda: _Reg())

    async def _ok(did):
        return {"success": True}

    import Orchestrator.adb as adb_mod
    monkeypatch.setattr(adb_mod, "get_adb_manager", lambda: SimpleNamespace(ensure_connected=_ok))

    result = await headless.run_cu_task("t-android", "gem-op", "tap", device_id="pixel-9",
                                        model="gemini-2.5-computer-use-preview-10-2025")
    assert result["success"] is True and launched.get("yes") is True
    assert not da._reservations                        # android claimed nothing


# ═══════════════════════════════════════════════════════════════════════════
# Chat launch integration (chat_routes.stream_*_computer_use)
# ═══════════════════════════════════════════════════════════════════════════

async def _drive(agen, limit=6):
    events = []
    async for ev in agen:
        events.append(ev)
        if len(events) >= limit:
            break
    return events


@pytest.mark.asyncio
async def test_gemini_chat_launch_refused_when_display_taken(monkeypatch):
    """A running (other-operator) Gemini desktop task holds the display; a new
    Gemini chat turn's LAUNCH claim is refused and yields the SSE error event."""
    monkeypatch.setattr("Orchestrator.config.GOOGLE_API_KEY", "fake", raising=False)
    monkeypatch.setattr(cr, "build_cu_context", lambda *a, **k: ("", {}))
    _register_gemini_task("other-op", status="running")

    events = await _drive(cr.stream_gemini_computer_use(
        [{"role": "user", "content": "open settings"}], "", "brandon", device_id="blackbox"))
    assert any(e["type"] == "error" and "Cannot start Gemini CU" in e["data"] for e in events)
    assert not da._reservations                        # nothing leaked (finally)


@pytest.mark.asyncio
async def test_gemini_chat_reconnect_does_not_claim_or_leak(monkeypatch):
    """C3 / reentrancy: a reconnect/queue turn (own session already running) does
    NOT launch, so it never claims — and the finally leaves the table clean. The
    operator is never self-blocked."""
    monkeypatch.setattr("Orchestrator.config.GOOGLE_API_KEY", "fake", raising=False)
    own = _register_gemini_chat("brandon", status="running")
    own.user_message = "first task"

    events = await _drive(cr.stream_gemini_computer_use(
        [{"role": "user", "content": "a different follow-up"}], "", "brandon", device_id="blackbox"))
    assert any(e["type"] == "cu_queued" for e in events)
    assert not any(e["type"] == "error" for e in events)   # never self-blocked
    assert not da._reservations                            # no leak (C3)


def test_all_three_chat_streams_release_their_claim_in_a_finally():
    """Structural guard (invariant 4): each chat CU stream must release its
    per-launch claim id in a finally, so the two browser streams inherit the
    guarantee the gemini one is driven for above."""
    import ast
    import inspect

    for fn in (cr.stream_computer_use, cr.stream_openai_computer_use,
               cr.stream_gemini_computer_use):
        tree = ast.parse(inspect.getsource(fn))
        released = any(
            isinstance(n, ast.Call) and getattr(n.func, "id", "") == "release_claim"
            and n.args and getattr(n.args[0], "id", "") == "_display_claim_id"
            for n in ast.walk(tree)
        )
        has_finally = any(isinstance(n, ast.Try) and n.finalbody for n in ast.walk(tree))
        assert released and has_finally, f"{fn.__name__} does not release its claim in a finally"
