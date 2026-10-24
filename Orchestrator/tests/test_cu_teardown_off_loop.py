"""Audit rank 46 (O13-computer-use-01, with its siblings O13-computer-use-17
and -19): computer-use session lookups, teardowns and the main-desktop stream
never stall the server's event loop.

A CU teardown stops Chrome (wait 10 s + 5 s) and the display quartet
(terminate, wait 3 s, kill, wait 2 s per role); the main-desktop stream's first
viewer spawns x11vnc and websockify and waits up to 5 s for each port. These
ran inline in async routes, in the chat CU streams (a lookup tears down an
expired session) and in the periodic reaper. Now they run in worker threads,
and the browser session store keeps its bookkeeping under a lock so a
teardown in one thread and a lookup in another cannot both take a session.

The tests measure the longest gap between turns of a 20 ms ticker: a call
blocking the loop for 0.4 s shows up as a 0.4 s gap.

Hermetic: no display, no Chrome, no API; every teardown is a stub.
"""
import ast
import asyncio
import pathlib
import threading
import time

import pytest

from Orchestrator.browser import session_manager as bsm
from Orchestrator.gemini_cu import session_manager as gsm

BLOCK = 0.4
ORCH = pathlib.Path(__file__).resolve().parents[1]


async def _max_gap_during(coro):
    """Await ``coro`` while a 20 ms ticker runs; return (result, longest gap)."""
    gaps = []
    stop = asyncio.Event()
    ticking = asyncio.Event()

    async def ticker():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            gaps.append(now - last)
            last = now
            ticking.set()

    t = asyncio.create_task(ticker())
    await ticking.wait()  # a call that blocks at once must land between ticks
    try:
        result = await coro
    finally:
        stop.set()
        await t
    return result, max(gaps or [0.0])


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture
def stores():
    """Empty both CU session stores for the test and restore them after."""
    saved = (dict(bsm._sessions), dict(bsm._operator_sessions),
             dict(gsm._sessions), dict(gsm._operator_sessions))
    for d in (bsm._sessions, bsm._operator_sessions, gsm._sessions, gsm._operator_sessions):
        d.clear()
    yield
    for d, old in zip((bsm._sessions, bsm._operator_sessions,
                       gsm._sessions, gsm._operator_sessions), saved):
        d.clear()
        d.update(old)


@pytest.fixture
def slow_teardown(monkeypatch):
    """Every session teardown blocks for BLOCK seconds; returns the log."""
    torn = []

    def slow_destroy(self):
        time.sleep(BLOCK)
        torn.append(self.session_id)

    monkeypatch.setattr(bsm.ComputerUseSession, "destroy", slow_destroy)
    monkeypatch.setattr(gsm.GeminiCUSession, "destroy", slow_destroy)
    return torn


def _expired_browser_session(operator):
    s = bsm.get_or_create_session(operator)
    s.last_activity = time.time() - 10 * 3600
    return s


def _expired_gemini_session(operator, device="blackbox", env="desktop"):
    s = gsm.get_or_create_session(operator, device, env)
    s.last_activity = time.time() - 10 * 3600
    return s


# ---------------------------------------------------------------------------
# Main-desktop stream (/cu/view/main/ws)
# ---------------------------------------------------------------------------

class _WS:
    client = None

    def __init__(self):
        self.closed = None

    async def accept(self):
        pass

    async def close(self, code=None, reason=None):
        self.closed = (code, reason)


class _SlowStream:
    """Like NativeDesktopStream: acquire() spawns the pair (slow), then counts."""

    def __init__(self, fail=False):
        self.fail = fail
        self.lock = threading.Lock()
        self._viewers = 0
        self.released = 0

    @property
    def viewers(self):
        with self.lock:
            return self._viewers

    def acquire(self):
        time.sleep(BLOCK)
        if self.fail:
            raise RuntimeError("desktop unavailable")
        with self.lock:
            self._viewers += 1
        return 6099

    def release(self):
        time.sleep(BLOCK)
        with self.lock:
            self._viewers -= 1
            self.released += 1


def test_main_desktop_stream_attach_and_detach_run_off_the_loop(monkeypatch):
    from Orchestrator.browser import native_stream
    from Orchestrator.routes import browser_routes
    mgr = _SlowStream()
    monkeypatch.setattr(native_stream, "get_native_stream", lambda: mgr)
    proxied = []

    async def fake_proxy(ws, url):
        proxied.append(url)

    monkeypatch.setattr(browser_routes, "_proxy_ws_to_loopback", fake_proxy)
    _, gap = asyncio.run(_max_gap_during(browser_routes._cu_view_ws_main(_WS())))
    assert proxied == ["ws://127.0.0.1:6099/"]
    assert mgr.viewers == 0 and mgr.released == 1
    assert gap < 0.2, f"the main-desktop stream blocked the event loop for {gap:.2f}s"


def test_main_desktop_stream_refusal_runs_off_the_loop(monkeypatch):
    from Orchestrator.browser import native_stream
    from Orchestrator.routes import browser_routes
    mgr = _SlowStream(fail=True)
    monkeypatch.setattr(native_stream, "get_native_stream", lambda: mgr)
    ws = _WS()
    _, gap = asyncio.run(_max_gap_during(browser_routes._cu_view_ws_main(ws)))
    assert ws.closed == (1008, "desktop unavailable")
    assert mgr.released == 0
    assert gap < 0.2, f"a refused attach blocked the event loop for {gap:.2f}s"


def test_a_viewer_cancelled_while_attaching_gives_its_place_back(monkeypatch):
    """The worker thread finishes the acquire even when the handler is
    cancelled: its viewer must still be released, or the pair never stops."""
    from Orchestrator.browser import native_stream
    from Orchestrator.routes import browser_routes
    mgr = _SlowStream()
    monkeypatch.setattr(native_stream, "get_native_stream", lambda: mgr)

    async def fake_proxy(ws, url):
        raise AssertionError("a cancelled attach must not proxy")

    monkeypatch.setattr(browser_routes, "_proxy_ws_to_loopback", fake_proxy)

    async def go():
        task = asyncio.create_task(browser_routes._cu_view_ws_main(_WS()))
        await asyncio.sleep(0.1)  # inside acquire()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        deadline = time.monotonic() + 3
        while mgr.released == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

    asyncio.run(go())
    assert mgr.released == 1 and mgr.viewers == 0


# ---------------------------------------------------------------------------
# Session routes and the chat CU streams
# ---------------------------------------------------------------------------

def test_cu_new_session_tears_down_off_the_loop(stores, slow_teardown):
    from Orchestrator.routes import chat_routes
    a = bsm.get_or_create_session("cu-new-op")
    g = gsm.get_or_create_session("cu-new-op", "blackbox", "desktop")
    result, gap = asyncio.run(_max_gap_during(
        chat_routes.cu_new_session(_Req({"operator": "cu-new-op"}))))
    assert result["success"] is True
    assert sorted(slow_teardown) == sorted([a.session_id, g.session_id])
    assert bsm.get_operator_session("cu-new-op") is None and gsm.get_session("cu-new-op") is None
    assert gap < 0.2, f"/chat/cu-new-session blocked the event loop for {gap:.2f}s"


def test_cu_session_open_replaces_an_expired_desktop_off_the_loop(stores, slow_teardown, monkeypatch):
    from Orchestrator.routes import browser_routes
    old = _expired_browser_session("cu-open-op")

    async def started(self, url=None):
        return True

    monkeypatch.setattr(bsm.ComputerUseSession, "ensure_browser", started)
    result, gap = asyncio.run(_max_gap_during(
        browser_routes.cu_session_open(browser_routes.CuSessionOpenIn(operator="cu-open-op"))))
    assert result["reused"] is False and result["session_id"] != old.session_id
    assert slow_teardown == [old.session_id]
    assert gap < 0.2, f"/cu/session/open blocked the event loop for {gap:.2f}s"


def test_cu_session_open_failure_tears_down_off_the_loop(stores, slow_teardown, monkeypatch):
    from Orchestrator.routes import browser_routes

    async def failed(self, url=None):
        return False

    monkeypatch.setattr(bsm.ComputerUseSession, "ensure_browser", failed)
    result, gap = asyncio.run(_max_gap_during(
        browser_routes.cu_session_open(browser_routes.CuSessionOpenIn(operator="cu-open-op"))))
    assert result.status_code == 502
    assert len(slow_teardown) == 1 and bsm.get_operator_session("cu-open-op") is None
    assert gap < 0.2, f"a failed /cu/session/open blocked the event loop for {gap:.2f}s"


async def _first_event(agen):
    try:
        return await agen.__anext__()
    finally:
        await agen.aclose()


MESSAGES = [{"role": "user", "content": "open the page"}]


def test_anthropic_cu_stream_replaces_an_expired_session_off_the_loop(stores, slow_teardown, monkeypatch):
    from Orchestrator.browser import config as cu_config
    from Orchestrator.routes import chat_routes
    monkeypatch.setattr(cu_config, "ANTHROPIC_API_KEY", "test-key")
    old = _expired_browser_session("cu-stream-op")
    evt, gap = asyncio.run(_max_gap_during(_first_event(
        chat_routes.stream_computer_use(MESSAGES, "claude-cu", "cu-stream-op"))))
    assert evt["type"] == "cu_session" and evt["data"]["session_id"] != old.session_id
    assert slow_teardown == [old.session_id]
    assert gap < 0.2, f"the CU stream's session lookup blocked the event loop for {gap:.2f}s"


def test_openai_cu_stream_replaces_an_expired_session_off_the_loop(stores, slow_teardown, monkeypatch):
    from Orchestrator import config
    from Orchestrator.routes import chat_routes
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key")
    old = _expired_browser_session("cu-stream-op")
    evt, gap = asyncio.run(_max_gap_during(_first_event(
        chat_routes.stream_openai_computer_use(MESSAGES, "", "cu-stream-op"))))
    assert evt["type"] == "cu_session" and evt["data"]["session_id"] != old.session_id
    assert slow_teardown == [old.session_id]
    assert gap < 0.2, f"the OpenAI CU stream's session lookup blocked the event loop for {gap:.2f}s"


def test_gemini_cu_stream_replaces_an_expired_session_off_the_loop(stores, slow_teardown, monkeypatch):
    import types
    from Orchestrator import config
    from Orchestrator import device_registry
    from Orchestrator.routes import chat_routes
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(device_registry, "get_registry",
                        lambda: types.SimpleNamespace(get_device=lambda d: None))
    old = _expired_gemini_session("cu-stream-op")
    evt, gap = asyncio.run(_max_gap_during(_first_event(
        chat_routes.stream_gemini_computer_use(MESSAGES, "", "cu-stream-op"))))
    assert evt["type"] == "cu_session" and evt["data"]["session_id"] != old.session_id
    assert slow_teardown == [old.session_id]
    assert gap < 0.2, f"the Gemini CU stream's session lookup blocked the event loop for {gap:.2f}s"


def test_gemini_cu_end_session_tears_down_off_the_loop(stores, slow_teardown):
    from Orchestrator.routes import gemini_cu_routes
    g = gsm.get_or_create_session("cu-end-op", "blackbox", "desktop")
    result, gap = asyncio.run(_max_gap_during(gemini_cu_routes.end_session("cu-end-op")))
    assert result == {"status": "destroyed"} and slow_teardown == [g.session_id]
    assert gap < 0.2, f"DELETE /gemini-cu/session blocked the event loop for {gap:.2f}s"


# ---------------------------------------------------------------------------
# The periodic reaper
# ---------------------------------------------------------------------------

def test_reaper_sweep_runs_off_the_loop(stores, slow_teardown, monkeypatch):
    from Orchestrator import startup
    import Orchestrator.browser.display as display

    class _Alloc:
        reaped = 0

        def reap_idle(self):
            time.sleep(BLOCK)
            _Alloc.reaped += 1

    monkeypatch.setattr(display, "get_allocator", lambda: _Alloc())
    monkeypatch.setattr(bsm, "cleanup_old_screenshots", lambda *a, **k: None)
    a = _expired_browser_session("cu-reap-op")
    g = _expired_gemini_session("cu-reap-op")
    _, gap = asyncio.run(_max_gap_during(startup._cu_sweep_once()))
    assert _Alloc.reaped == 1
    assert sorted(slow_teardown) == sorted([a.session_id, g.session_id])
    assert gap < 0.2, f"the CU reaper blocked the event loop for {gap:.2f}s"


# ---------------------------------------------------------------------------
# Store races: a teardown thread and a lookup thread never both take a session
# ---------------------------------------------------------------------------

def test_browser_session_is_torn_down_once_under_concurrent_cleanups(stores, slow_teardown, monkeypatch):
    monkeypatch.setattr(bsm, "cleanup_old_screenshots", lambda *a, **k: None)
    s = _expired_browser_session("cu-race-op")
    errors = []

    def run(fn, *a):
        try:
            fn(*a)
        except Exception as e:  # pragma: no cover - the failure being guarded
            errors.append(e)

    threads = [threading.Thread(target=run, args=(bsm.cleanup_inactive_sessions, 600)),
               threading.Thread(target=run, args=(bsm.destroy_session_by_id, s.session_id)),
               threading.Thread(target=run, args=(bsm.get_or_create_session, "cu-race-op"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert slow_teardown == [s.session_id]
    live = [x for x in bsm._sessions.values() if x.operator == "cu-race-op"]
    assert len(live) <= 1 and s.session_id not in bsm._sessions


def test_gemini_session_is_torn_down_once_under_concurrent_cleanups(stores, slow_teardown):
    s = _expired_gemini_session("cu-race-op")
    errors = []

    def run(fn, *a):
        try:
            fn(*a)
        except Exception as e:  # pragma: no cover - the failure being guarded
            errors.append(e)

    threads = [threading.Thread(target=run, args=(gsm.cleanup_expired_sessions,)),
               threading.Thread(target=run, args=(gsm.destroy_session, "cu-race-op")),
               threading.Thread(target=run, args=(gsm.get_or_create_session,
                                                  "cu-race-op", "blackbox", "desktop"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert slow_teardown == [s.session_id]
    assert s.session_id not in gsm._sessions


# ---------------------------------------------------------------------------
# Static guard: async code never calls a CU store lookup or teardown inline
# ---------------------------------------------------------------------------

# Calls that can tear a CU session or the main-desktop stream down (or start
# it), each a multi-second blocking wait. Pass them to asyncio.to_thread.
_BLOCKING_CU_CALLS = {
    "get_or_create_session", "gemini_get_or_create", "destroy_session",
    "destroy_session_by_id", "destroy_task_session", "gemini_destroy",
    "cleanup_inactive_sessions", "cleanup_expired_sessions", "reap_idle",
    "_cleanup_session",
}
# Files whose async code runs on a loop of its own in a worker thread.
_OWN_LOOP = {"browser/headless.py"}


def _inline_cu_calls(path):
    tree = ast.parse(path.read_text())
    hits = []

    def walk(node, in_async):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.AsyncFunctionDef):
                walk(child, True)
            elif isinstance(child, (ast.FunctionDef, ast.Lambda)):
                walk(child, False)
            else:
                if in_async and isinstance(child, ast.Call):
                    f = child.func
                    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                    if name in _BLOCKING_CU_CALLS:
                        hits.append(f"{path.relative_to(ORCH)}:{child.lineno}: {ast.unparse(child)[:90]}")
                    if isinstance(f, ast.Attribute) and f.attr in ("acquire", "release") \
                            and isinstance(f.value, ast.Name) and f.value.id == "mgr":
                        hits.append(f"{path.relative_to(ORCH)}:{child.lineno}: {ast.unparse(child)[:90]}")
                walk(child, in_async)

    walk(tree, False)
    return hits


def test_no_async_code_calls_a_cu_teardown_inline():
    hits = []
    for path in sorted(ORCH.rglob("*.py")):
        rel = path.relative_to(ORCH).as_posix()
        if rel.startswith(("venv/", "tests/")) or "/tests/" in rel or rel in _OWN_LOOP:
            continue
        hits += _inline_cu_calls(path)
    assert not hits, "CU lookups/teardowns called inline in async code:\n" + "\n".join(hits)
