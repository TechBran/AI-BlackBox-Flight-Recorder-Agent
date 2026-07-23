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
# M9: only NATIVE sessions contend for the one physical display, so the arbiter
# scan sees them. These helpers register NATIVE holders (native_mode=True) — a
# virtual session drives its own per-session display and never appears here.
def _register_browser(operator, status="running", device_id="blackbox", native_mode=True):
    s = ComputerUseSession(operator, device_id=device_id)
    s.native_mode = native_mode
    s.status = status
    bsm._sessions[s.session_id] = s
    bsm._operator_sessions[operator] = s.session_id
    return s


def _register_gemini_chat(operator, status="running", environment="desktop", device_id="blackbox", native_mode=True):
    s = gsm.get_or_create_session(operator, device_id, environment)   # BOTH dicts
    s.native_mode = native_mode
    s.status = status
    return s


def _register_gemini_task(operator, status="running", environment="desktop", device_id="blackbox", native_mode=True):
    s = gsm.create_task_session(operator, device_id, environment)     # _sessions only
    s.native_mode = native_mode
    s.status = status
    return s


def _stub_browser_run(monkeypatch):
    """Stub run_cu_task's browser seams so a launch can reach + run the (stubbed)
    driver without Xvfb/Chrome/Anthropic."""
    monkeypatch.setattr(headless, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(headless, "NATIVE_MODE", True)

    class _FakeHandle:
        display_num = 100

        def get_env(self):
            return {"DISPLAY": ":100"}

        def touch(self):
            pass

    async def _ensure(self, url="about:blank", backend="anthropic"):
        self.display = _FakeHandle()
        return True

    monkeypatch.setattr(ComputerUseSession, "ensure_browser", _ensure)
    monkeypatch.setattr(
        "Orchestrator.browser.screenshot.capture_screenshot_display",
        lambda n: b"\x89PNG-fake")
    monkeypatch.setattr(
        "Orchestrator.browser.screenshot.capture_screenshot",
        lambda *a, **k: b"\x89PNG-fake")
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
    """C1: a REUSED NATIVE browser session's launch must claim the display.
    Pre-register an idle browser session, run a NATIVE browser CU task (which
    REUSES it), and prove that WHILE its driver runs a concurrent cross-kind claim
    is REFUSED. Mutation-verified in the report: drop run_cu_task's try_claim and
    it is GRANTED. (M9: virtual launches never claim — this pins the native path.)"""
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

    result = await headless.run_cu_task("t-reuse", "op", "hi", native_mode=True)   # reuses the idle session
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

    result = await headless.run_cu_task("t-refused", "op", "hi", native_mode=True,
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
    result = await headless.run_cu_task("t-b-same", "brandon", "hi", native_mode=True,
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
    result = await headless.run_cu_task("t-b-diff", "brandon", "hi", native_mode=True,
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
    result = await headless.run_cu_task("t-brws", "brandon", "hi", native_mode=True,
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
        [{"role": "user", "content": "open settings"}], "", "brandon", device_id="blackbox",
        native_mode=True))
    assert any(e["type"] == "error" and "Cannot start Gemini CU" in e["data"] for e in events)
    assert not da._reservations                        # nothing leaked (finally)


@pytest.mark.asyncio
async def test_gemini_chat_true_reconnect_does_not_claim_or_leak(monkeypatch):
    """C3 / reentrancy: a TRUE reconnect (same message, own session running) does
    NOT launch, so it never claims — the finally leaves the table clean and the
    operator is never self-blocked. (Under D1 multi-desktop a DIFFERENT prompt
    now spawns a new desktop instead of queueing — covered below.)"""
    monkeypatch.setattr("Orchestrator.config.GOOGLE_API_KEY", "fake", raising=False)
    own = _register_gemini_chat("brandon", status="running")
    own.user_message = "first task"
    # A reconnect falls through to the stream's queue-consumer loop; seed the
    # None sentinel so it exits instead of blocking on an empty queue.
    own.event_queue.put_nowait(None)

    events = await _drive(cr.stream_gemini_computer_use(
        [{"role": "user", "content": "first task"}], "", "brandon", device_id="blackbox"))
    # A matching message is a reconnect, not a new prompt: no new desktop, no leak.
    assert not any(e["type"] == "error" for e in events)   # never self-blocked
    assert not any(e.get("data", {}).get("new_desktop") for e in events
                   if e["type"] == "cu_session")
    assert not da._reservations                            # no leak (C3)


@pytest.mark.asyncio
async def test_gemini_chat_busy_new_prompt_spawns_desktop_without_leak(monkeypatch):
    """D1 multi-desktop (2026-07-23): a NEW prompt while the own session is busy
    appends a brand-new desktop (cu_session new_desktop=True) instead of queueing,
    and a virtual launch never claims the physical-display arbiter — so the table
    stays clean."""
    monkeypatch.setattr("Orchestrator.config.GOOGLE_API_KEY", "fake", raising=False)
    monkeypatch.setattr(cr, "build_cu_context", lambda *a, **k: ("", {}))

    async def _noop_loop(session, *a, **k):
        # Push the None sentinel so the stream's queue-consumer loop exits
        # (the real driver does this at the end of its run).
        session.event_queue.put_nowait(None)
    monkeypatch.setattr(cr, "_gemini_cu_agent_loop", _noop_loop)

    own = _register_gemini_chat("brandon", status="running")
    own.user_message = "first task"

    events = await _drive(cr.stream_gemini_computer_use(
        [{"role": "user", "content": "a different follow-up"}], "", "brandon",
        device_id="blackbox"))
    new_desktops = [e for e in events
                    if e["type"] == "cu_session" and e.get("data", {}).get("new_desktop")]
    assert new_desktops, "a busy agent + new prompt must append a new desktop"
    assert not any(e["type"] == "error" for e in events)   # never self-blocked
    assert not da._reservations                            # virtual launch never claims


def test_all_three_chat_streams_release_via_agent_task_done_callback():
    """Structural guard (Hole 3): each chat CU stream must bind its per-launch
    release to the DRIVER TASK's lifecycle (agent_task.add_done_callback), NOT a
    consumer-generator finally — otherwise a client disconnect during the
    pre-"running" window drops a live claim. Also assert no bare consumer finally
    releases the claim (the mutation this test discriminates against)."""
    import ast
    import inspect

    for fn in (cr.stream_computer_use, cr.stream_openai_computer_use,
               cr.stream_gemini_computer_use):
        tree = ast.parse(inspect.getsource(fn))
        callbacks = [n for n in ast.walk(tree)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "add_done_callback"]
        # the callback body must reference release_claim
        bound = any("release_claim" in ast.dump(cb) for cb in callbacks)
        assert bound, f"{fn.__name__} does not release via agent_task.add_done_callback"
        # and there must be no finally-body that calls release_claim (the old,
        # disconnect-buggy shape)
        for n in ast.walk(tree):
            if isinstance(n, ast.Try) and n.finalbody:
                fin = ast.dump(ast.Module(body=n.finalbody, type_ignores=[]))
                assert "release_claim" not in fin, (
                    f"{fn.__name__} still releases in a consumer finally (Hole 3)")


# ═══════════════════════════════════════════════════════════════════════════
# Hole 2: the Gemini "browser" environment drives the local display
# ═══════════════════════════════════════════════════════════════════════════

def test_gemini_browser_environment_owns_local_display():
    """A `browser`-environment Gemini session drives the local X server
    (agent_loop._capture_screenshot/_execute_predefined_action branch on
    `in ("browser","desktop")`), so the arbiter must report it as a holder."""
    s = _register_gemini_task("carol", status="running", environment="browser")
    o = local_display_owner()
    assert o is not None and o.session_id == s.session_id


@pytest.mark.asyncio
async def test_gemini_browser_session_blocks_browser_and_gemini_launches(monkeypatch):
    """A running `browser`-environment Gemini session blocks BOTH a browser CU
    launch and a gemini-task launch."""
    monkeypatch.setattr(headless, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(headless, "GOOGLE_API_KEY", "fake")
    _register_gemini_chat("holder", status="running", environment="browser")

    # browser headless launch refused (returns before ensure_browser)
    r1 = await headless.run_cu_task("t-b1", "op", "hi", native_mode=True, model="claude-opus-4-6")
    assert r1["success"] is False and "Computer Use" in r1["result_text"]

    # gemini-task launch refused
    def _boom(*a, **k):
        raise AssertionError("launched despite a browser-env holder")

    monkeypatch.setattr(headless, "gemini_create_task_session", _boom)
    monkeypatch.setattr(headless, "run_gemini_cu_loop", _boom)
    r2 = await headless.run_cu_task("t-b2", "op", "hi", native_mode=True,
                                    model="gemini-2.5-computer-use-preview-10-2025")
    assert r2["success"] is False


def _scan_gemini_driver_environments(source: str):
    """Collect every `session.environment` comparison-constant in a Gemini-driver
    source string, and the names of the functions that branch on `environment`.
    Whole-MODULE scan (M-3): not pinned to a fixed function list, so a THIRD
    function that drives the local display for a new environment is seen too."""
    import ast
    tree = ast.parse(source)
    envs = set()
    branching_fns = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for n in ast.walk(fn):
            if (isinstance(n, ast.Compare)
                    and isinstance(n.left, ast.Attribute)
                    and n.left.attr == "environment"):
                branching_fns.add(fn.name)
                for comp in n.comparators:
                    elts = comp.elts if isinstance(comp, (ast.Tuple, ast.List, ast.Set)) else [comp]
                    for e in elts:
                        if isinstance(e, ast.Constant) and isinstance(e.value, str):
                            envs.add(e.value)
    return envs, branching_fns


def test_arbiter_local_env_superset_of_gemini_driver():
    """Drift guard (Hole 2, widened per M-3): scan the WHOLE gemini driver module
    for every `session.environment` comparison (not a fixed function list, so a
    THIRD local-display function for a new environment is caught), drop the known
    non-local branch ("android"), and assert every remaining env is recognised by
    the arbiter's PUBLIC predicate. A driver that starts driving the local display
    for an environment the arbiter doesn't know fails here at commit time — before
    it surfaces with a live mouse. Mutation-verified in the report: inject a fake
    third local-display function into a scratch copy of the driver and this
    fails."""
    import inspect
    import Orchestrator.gemini_cu.agent_loop as al

    envs, branching_fns = _scan_gemini_driver_environments(inspect.getsource(al))
    driver_local = envs - {"android"}   # android = the driver's non-local branch
    assert driver_local, "drift guard matched no environment comparison — matcher broken"
    unknown = {e for e in driver_local if not da.is_local_environment(e)}
    assert not unknown, (
        f"gemini driver branches on local environment(s) {sorted(unknown)} the "
        f"arbiter does not recognise (branching fns: {sorted(branching_fns)}) — "
        f"widen display_arbiter._GEMINI_LOCAL_ENVIRONMENTS / is_local_environment")


def test_local_env_source_of_truth_not_reimported_privately():
    """I-1: no module outside display_arbiter may reference the PRIVATE
    _GEMINI_LOCAL_ENVIRONMENTS — that reach-into-internals is the exact drift
    (a hardcoded/copied env set) the public is_local_environment predicate exists
    to prevent. Callers must go through the predicate."""
    import pathlib
    root = pathlib.Path(da.__file__).resolve().parents[2] / "Orchestrator"
    offenders = []
    for p in root.rglob("*.py"):
        if p.name == "display_arbiter.py" or "/tests/" in p.as_posix():
            continue
        if "_GEMINI_LOCAL_ENVIRONMENTS" in p.read_text(encoding="utf-8"):
            offenders.append(str(p.relative_to(root)))
    assert not offenders, (
        f"these modules import the private _GEMINI_LOCAL_ENVIRONMENTS instead of "
        f"calling is_local_environment(): {offenders}")


# ═══════════════════════════════════════════════════════════════════════════
# Hole 1: gemini_cu_routes is the sixth launch site
# ═══════════════════════════════════════════════════════════════════════════

def _fake_task_db(monkeypatch, task):
    from types import SimpleNamespace
    from Orchestrator import tasks as tasks_mod
    db = SimpleNamespace(get_task=lambda tid: task, save_task=lambda t: None)
    monkeypatch.setattr(tasks_mod, "task_db", db)


def _make_route_task(environment):
    from types import SimpleNamespace
    return SimpleNamespace(
        task_id="rt1", status=None, progress=0, result_url=None,
        result_data={"device_id": "blackbox", "environment": environment,
                     "model": "m", "url": None})


@pytest.mark.asyncio
async def test_gemini_route_run_claims_and_releases(monkeypatch):
    """gemini_cu_routes._run_task (the /run driver): a LOCAL launch claims the
    display while it drives, and releases on exit."""
    from Orchestrator.routes import gemini_cu_routes as gr
    task = _make_route_task("browser")
    _fake_task_db(monkeypatch, task)
    observed = {}

    async def fake_loop(session, prompt, model, system_prompt, url):
        observed["blocked"] = da.try_claim("browser", "op2", "concurrent") is not None
        da.release_claim("concurrent")
        yield {"type": "done", "data": {"content": "ok"}}

    monkeypatch.setattr(gr, "run_gemini_cu_loop", fake_loop)

    async def _no_snap(*a, **k):
        return None

    monkeypatch.setattr(gr, "_snapshot_cu_result", _no_snap)

    await gr._run_task("rt1", "op", "blackbox", "browser", "do it", "m", None, None)
    assert observed["blocked"] is True         # the route launch had claimed
    assert not da._reservations                 # released in finally


@pytest.mark.asyncio
async def test_gemini_route_run_android_does_not_claim(monkeypatch):
    """An ADB/android /run task must NOT claim the local display."""
    from Orchestrator.routes import gemini_cu_routes as gr
    task = _make_route_task("android")
    _fake_task_db(monkeypatch, task)
    observed = {}

    async def fake_loop(session, prompt, model, system_prompt, url):
        observed["blocked"] = da.try_claim("browser", "op2", "concurrent") is not None
        da.release_claim("concurrent")
        yield {"type": "done", "data": {"content": "ok"}}

    monkeypatch.setattr(gr, "run_gemini_cu_loop", fake_loop)

    async def _no_snap(*a, **k):
        return None

    monkeypatch.setattr(gr, "_snapshot_cu_result", _no_snap)

    await gr._run_task("rt1", "op", "pixel-9", "android", "tap", "m", None, None)
    assert observed["blocked"] is False        # android never claimed the display
    assert not da._reservations


@pytest.mark.asyncio
async def test_gemini_route_run_releases_on_driver_error(monkeypatch):
    """A LOCAL /run task whose driver raises must still release the claim."""
    from Orchestrator.routes import gemini_cu_routes as gr
    from Orchestrator.models import TaskStatus
    task = _make_route_task("browser")
    _fake_task_db(monkeypatch, task)

    async def boom_loop(session, prompt, model, system_prompt, url):
        raise RuntimeError("driver exploded")
        yield  # unreachable — makes this an async generator

    monkeypatch.setattr(gr, "run_gemini_cu_loop", boom_loop)

    await gr._run_task("rt1", "op", "blackbox", "browser", "x", "m", None, None)
    assert task.status == TaskStatus.FAILED
    assert not da._reservations                 # released in finally despite the error


@pytest.mark.asyncio
async def test_gemini_route_run_refuses_when_display_taken(monkeypatch):
    """A LOCAL /run task is refused (task FAILED, driver never launched) when the
    display is already held."""
    from Orchestrator.routes import gemini_cu_routes as gr
    from Orchestrator.models import TaskStatus
    task = _make_route_task("browser")
    _fake_task_db(monkeypatch, task)
    _register_browser("someone", status="running")   # display busy

    def _boom(*a, **k):
        raise AssertionError("driver launched despite a display conflict")

    monkeypatch.setattr(gr, "run_gemini_cu_loop", _boom)
    await gr._run_task("rt1", "op", "blackbox", "browser", "x", "m", None, None)
    assert task.status == TaskStatus.FAILED
    assert "Computer Use" in task.result_data["error"]


@pytest.mark.asyncio
async def test_gemini_route_stream_claims_and_releases(monkeypatch):
    """gemini_cu_routes /stream: the launch claims (driver runs inline in the
    generator) and releases when the stream is exhausted."""
    from types import SimpleNamespace
    from Orchestrator.routes import gemini_cu_routes as gr

    class _Reg:
        def get_device(self, did):
            return SimpleNamespace(protocol=gr.DeviceProtocol.HTTP if hasattr(gr.DeviceProtocol, "HTTP") else None,
                                   name="Desk", id=did)

    # A non-ADB device -> environment "browser".
    monkeypatch.setattr(gr, "get_registry", lambda: _Reg())
    observed = {}

    async def fake_loop(session, prompt, model, system_prompt, url):
        observed["blocked"] = da.try_claim("browser", "op2", "concurrent") is not None
        da.release_claim("concurrent")
        yield {"type": "done", "data": {"content": "ok"}}

    monkeypatch.setattr(gr, "run_gemini_cu_loop", fake_loop)

    body = gr.GeminiCURequest(prompt="go", operator="op", device_id="blackbox")
    resp = await gr.stream_gemini_cu(body)
    chunks = [c async for c in resp.body_iterator]
    assert observed["blocked"] is True          # the stream launch had claimed
    assert not da._reservations                  # released in the generator finally
    assert any("[DONE]" in c for c in chunks)


# ═══════════════════════════════════════════════════════════════════════════
# Hole 3: disconnect during the pre-"running" window must NOT drop a live claim
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_disconnect_during_starting_keeps_claim(monkeypatch):
    """Hole 3: the driver task survives client disconnect by design and only sets
    status="running" after being scheduled. A disconnect in the [claim … running]
    window must NOT release the claim — release is bound to the DRIVER TASK's
    done-callback, not the consumer generator's exit. Mutation-verified in the
    report: move the release back to a consumer finally and this fails."""
    import asyncio
    import contextlib

    monkeypatch.setattr("Orchestrator.config.GOOGLE_API_KEY", "fake", raising=False)
    monkeypatch.setattr(cr, "build_cu_context", lambda *a, **k: ("", {}))

    started = asyncio.Event()
    finish = asyncio.Event()

    async def blocking_wrapper(session, operator, user_text, model, system_prompt):
        # deliberately does NOT set status="running" — this IS the pre-running window
        started.set()
        await finish.wait()

    monkeypatch.setattr(cr, "_gemini_cu_agent_loop", blocking_wrapper)

    agen = cr.stream_gemini_computer_use(
        [{"role": "user", "content": "go"}], "", "brandon", device_id="blackbox",
        native_mode=True)

    async def consume():
        async for _ in agen:
            pass

    task = asyncio.create_task(consume())
    await started.wait()                       # driver launched → claim made
    assert da._reservations                     # claim held while "starting"

    task.cancel()                               # simulate client disconnect
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    # THE FIX: the claim is STILL held (driver task not done → callback not fired)
    assert da._reservations, "Hole 3: disconnect during 'starting' dropped a live claim"

    finish.set()                                # let the driver finish
    await asyncio.sleep(0.02)                    # let the done-callback run
    assert not da._reservations                  # released on the driver's lifecycle
