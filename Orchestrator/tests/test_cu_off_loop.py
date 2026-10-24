"""Audit rank 46 (O13-computer-use-01, O02-chat-routes-A-02, O13-computer-use-09,
-02): computer-use work never stalls the server's event loop.

The chat path runs the Anthropic CU driver as a task on the main loop, where
every step used to block it: the model's `bash` tool ran subprocess.run (up to
120 s), and input actions, screenshot captures, desktop allocation (Xvfb +
WM + VNC, ~4.5 s) and Chrome start (3 s) were all synchronous. Now bash is an
asyncio subprocess in its own process group (killed whole on timeout or
E-stop) and the rest runs in worker threads.

The CU tests measure the longest gap between turns of a 20 ms ticker: a call
blocking the loop for 0.4 s shows up as a 0.4 s gap.

Hermetic: no display, no Chrome, no API (httpx is a scripted stand-in).
"""
import asyncio
import json
import os
import sys
import threading
import time
import types

import pytest

from Orchestrator.browser.session_manager import ComputerUseSession

BLOCK = 0.4


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


def _pid_alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except FileNotFoundError:
        return False


def _wait_dead(pid, within=2.0):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def pidfile(tmp_path):
    p = tmp_path / "child.pid"
    yield p
    # Never leave a test's sleeper running.
    if p.exists():
        try:
            os.kill(int(p.read_text().strip()), 9)
        except (ValueError, ProcessLookupError, PermissionError):
            pass


def _read_pid(p, within=2.0):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if p.exists() and p.read_text().strip():
            return int(p.read_text().strip())
        time.sleep(0.02)
    raise AssertionError("the command never wrote its child's pid")


# ---------------------------------------------------------------------------
# bash tool
# ---------------------------------------------------------------------------

def test_bash_tool_does_not_stall_the_loop():
    from Orchestrator.routes import chat_routes
    result, gap = asyncio.run(_max_gap_during(
        chat_routes.execute_bash_command("sleep 0.5; echo done")))
    assert result == {"output": "done\n", "exit_code": 0}
    assert gap < 0.2, f"the bash step blocked the event loop for {gap:.2f}s"


def test_bash_tool_keeps_its_output_shape():
    from Orchestrator.routes import chat_routes
    result = asyncio.run(chat_routes.execute_bash_command(
        "printf 'out\\r\\n'; printf 'err\\n' >&2; exit 3"))
    # stdout, then stderr after a newline; newlines normalised like text=True.
    assert result == {"output": "out\n\nerr\n", "exit_code": 3}


def test_bash_tool_caps_output_at_10kb():
    from Orchestrator.routes import chat_routes
    result = asyncio.run(chat_routes.execute_bash_command("head -c 20000 /dev/zero | tr '\\0' x"))
    assert result["exit_code"] == 0
    assert result["output"].endswith("[OUTPUT TRUNCATED at 10KB]")
    assert len(result["output"]) < 10100


def _loop_runners():
    """asyncio.run, and uvloop.run as uvicorn runs the server when it is installed."""
    runners = [pytest.param(asyncio.run, id="asyncio")]
    try:
        import uvloop
        runners.append(pytest.param(uvloop.run, id="uvloop"))
    except ImportError:
        pass
    return runners


@pytest.mark.parametrize("run", _loop_runners())
def test_bash_background_launch_returns_at_once_and_the_app_survives(pidfile, run):
    """The CU prompt tells the model to open apps with `xterm &`. The app
    inherits the output pipes, so waiting for end-of-file held the step for the
    whole timeout, and the process-group kill then took the app down with it."""
    from Orchestrator.routes import chat_routes
    t0 = time.monotonic()
    result = run(chat_routes.execute_bash_command(
        f"echo started; sleep 30 & echo $! > {pidfile}", timeout=5))
    assert result == {"output": "started\n", "exit_code": 0}
    assert time.monotonic() - t0 < 2, "the step waited on the launched app's pipe"
    assert _pid_alive(_read_pid(pidfile)), "the launched app was killed"


def test_a_launched_app_that_writes_later_is_not_killed_by_sigpipe(pidfile):
    """The step returns at the shell's exit, but the app still holds the pipes:
    closing them would kill it with SIGPIPE on its next log line."""
    from Orchestrator.routes import chat_routes

    async def go():
        result = await chat_routes.execute_bash_command(
            f"(sleep 1; echo late; echo late >&2; exec sleep 30) & echo $! > {pidfile}",
            timeout=5)
        await asyncio.sleep(2.0)  # the loop keeps running, as the server's does
        return result

    t0 = time.monotonic()
    result = asyncio.run(go())
    assert result == {"output": "", "exit_code": 0}
    assert time.monotonic() - t0 < 4
    assert _pid_alive(_read_pid(pidfile)), "the launched app died after writing"


@pytest.mark.parametrize("run", _loop_runners())
def test_background_launch_leaves_no_transport_behind(pidfile, run, monkeypatch):
    """A headless CU run ends its own loop with the launched app still holding
    the pipes: the transport must be closed with the loop, not left for the GC
    ("Event loop is closed")."""
    import gc
    from Orchestrator.routes import chat_routes
    unraisable = []
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)
    run(chat_routes.execute_bash_command(f"sleep 30 & echo $! > {pidfile}", timeout=5))
    gc.collect()
    assert not unraisable, [str(u.exc_value) for u in unraisable]


def test_bash_output_beyond_the_pipe_buffer_does_not_wedge_the_step():
    from Orchestrator.routes import chat_routes
    result = asyncio.run(chat_routes.execute_bash_command(
        "head -c 300000 /dev/zero | tr '\\0' x; echo; echo tail >&2", timeout=10))
    assert result == {"output": "x" * 10000 + "\n... [OUTPUT TRUNCATED at 10KB]", "exit_code": 0}


def test_bash_timeout_kills_the_whole_process_group(pidfile):
    """A command still running at the timeout loses its whole group, children
    included: subprocess.run's timeout killed only the shell and orphaned them."""
    from Orchestrator.routes import chat_routes
    t0 = time.monotonic()
    result = asyncio.run(chat_routes.execute_bash_command(
        f"sleep 30 & echo $! > {pidfile}; wait", timeout=0.5))
    assert result == {"output": "Command timed out after 0.5s", "exit_code": -1}
    assert time.monotonic() - t0 < 3
    assert _wait_dead(_read_pid(pidfile)), "the backgrounded child survived the timeout"


def test_estop_cancel_kills_the_whole_process_group(pidfile):
    from Orchestrator.routes import chat_routes

    async def go():
        task = asyncio.create_task(chat_routes.execute_bash_command(
            f"sleep 30 & echo $! > {pidfile}; wait", timeout=60))
        while not (pidfile.exists() and pidfile.read_text().strip()):
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert _wait_dead(_read_pid(pidfile)), "the child survived the E-stop"


def test_text_editor_file_io_runs_off_the_loop(tmp_path, monkeypatch):
    import pathlib
    from Orchestrator.routes import chat_routes
    target = tmp_path / "notes.txt"
    target.write_text("a\nb\n")
    real_read = pathlib.Path.read_text

    def slow_read(self, *a, **k):
        time.sleep(BLOCK)
        return real_read(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "read_text", slow_read)
    result, gap = asyncio.run(_max_gap_during(
        chat_routes.execute_text_editor("view", str(target))))
    assert result == {"output": "1\ta\n2\tb\n3\t"}
    assert gap < 0.2, f"the editor step blocked the event loop for {gap:.2f}s"


# ---------------------------------------------------------------------------
# Anthropic CU driver: actions and screenshots
# ---------------------------------------------------------------------------

def _sse(events):
    return ["data: " + json.dumps(e) for e in events]


CLICK_TURN = _sse([
    {"type": "content_block_start",
     "content_block": {"type": "tool_use", "id": "tu_1", "name": "computer"}},
    {"type": "content_block_delta",
     "delta": {"type": "input_json_delta",
               "partial_json": "{\"action\": \"left_click\", \"coordinate\": [5, 5]}"}},
    {"type": "content_block_stop"},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
     "usage": {"input_tokens": 1, "output_tokens": 1}},
])
END_TURN = _sse([
    {"type": "content_block_start", "content_block": {"type": "text"}},
    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "done"}},
    {"type": "content_block_stop"},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
     "usage": {"input_tokens": 1, "output_tokens": 1}},
])


def _scripted_httpx(turns):
    calls = []

    class _Resp:
        status_code = 200

        def __init__(self, lines):
            self._lines = lines

        async def aiter_lines(self):
            for line in self._lines:
                yield line

        async def aread(self):
            return b""

    class _Ctx:
        def __init__(self, lines):
            self._lines = lines

        async def __aenter__(self):
            return _Resp(self._lines)

        async def __aexit__(self, *exc):
            return False

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, headers=None, json=None):
            calls.append(1)
            return _Ctx(turns[min(len(calls) - 1, len(turns) - 1)])

    mod = types.SimpleNamespace(
        AsyncClient=lambda **kw: _Client(),
        TimeoutException=type("TimeoutException", (Exception,), {}),
        ConnectError=type("ConnectError", (Exception,), {}),
    )
    return mod, calls


def test_cu_driver_runs_actions_and_captures_off_the_loop(monkeypatch):
    from Orchestrator.routes import chat_routes

    def slow_capture(self):
        time.sleep(BLOCK)
        return b"\x89PNG-fake" * 30

    monkeypatch.setattr(ComputerUseSession, "capture_screenshot_bytes", slow_capture)
    monkeypatch.setattr("Orchestrator.browser.screenshot.save_screenshot_to_uploads",
                        lambda png, ident, step: "/ui/uploads/x.png")

    async def _no_save(*a, **k):
        return None

    monkeypatch.setattr(chat_routes, "_cu_save_to_blackbox", _no_save)
    httpx_stub, calls = _scripted_httpx([CLICK_TURN, END_TURN])
    monkeypatch.setitem(sys.modules, "httpx", httpx_stub)

    executed = []

    class _SlowActions:
        def execute(self, action, **params):
            time.sleep(BLOCK)
            executed.append(action)
            return {"success": True, "message": "ok"}

    session = ComputerUseSession("cu-loop-op")
    session.actions = _SlowActions()

    from Orchestrator.browser.driver_anthropic import run_anthropic_cu_loop
    history = [{"role": "user", "content": [{"type": "text", "text": "click"}]}]
    _, gap = asyncio.run(_max_gap_during(run_anthropic_cu_loop(
        session, history, "sys", [], {}, "claude-sonnet-5", "cu-loop-op", "click")))
    assert executed == ["left_click"]
    assert len(calls) == 2
    assert gap < 0.2, f"a CU step blocked the event loop for {gap:.2f}s"


ZOOM_TURN = _sse([
    {"type": "content_block_start",
     "content_block": {"type": "tool_use", "id": "tu_z", "name": "computer"}},
    {"type": "content_block_delta",
     "delta": {"type": "input_json_delta",
               "partial_json": "{\"action\": \"zoom\", \"region\": [0, 0, 4, 4]}"}},
    {"type": "content_block_stop"},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
     "usage": {"input_tokens": 1, "output_tokens": 1}},
])


def test_cu_driver_zoom_crop_runs_off_the_loop(monkeypatch):
    """The zoom action decodes, crops and re-encodes a full frame (audit
    O13-computer-use-01's correction: 0.1-0.2 s of PIL work per zoom)."""
    import io
    import PIL.Image
    from Orchestrator.routes import chat_routes

    buf = io.BytesIO()
    PIL.Image.new("RGB", (16, 16), (1, 2, 3)).save(buf, format="PNG")
    frame = buf.getvalue()
    monkeypatch.setattr(ComputerUseSession, "capture_screenshot_bytes", lambda self: frame)
    monkeypatch.setattr("Orchestrator.browser.screenshot.save_screenshot_to_uploads",
                        lambda png, ident, step: "/ui/uploads/x.png")
    real_open = PIL.Image.open
    sizes = []

    def slow_open(fp, *a, **k):
        time.sleep(BLOCK)
        img = real_open(fp, *a, **k)
        return img

    monkeypatch.setattr(PIL.Image, "open", slow_open)

    async def _no_save(*a, **k):
        return None

    monkeypatch.setattr(chat_routes, "_cu_save_to_blackbox", _no_save)
    httpx_stub, calls = _scripted_httpx([ZOOM_TURN, END_TURN])
    monkeypatch.setitem(sys.modules, "httpx", httpx_stub)

    class _Actions:
        def execute(self, action, **params):
            return {"success": True, "message": "ok"}

    session = ComputerUseSession("cu-zoom-op")
    session.actions = _Actions()
    import Orchestrator.browser.screenshot as shot
    real_b64 = shot.screenshot_to_base64

    def record_b64(png):
        sizes.append(real_open(io.BytesIO(png)).size)
        return real_b64(png)

    monkeypatch.setattr(shot, "screenshot_to_base64", record_b64)

    from Orchestrator.browser.driver_anthropic import run_anthropic_cu_loop
    history = [{"role": "user", "content": [{"type": "text", "text": "zoom"}]}]
    _, gap = asyncio.run(_max_gap_during(run_anthropic_cu_loop(
        session, history, "sys", [], {}, "claude-sonnet-5", "cu-zoom-op", "zoom")))
    assert len(calls) == 2
    assert sizes == [(4, 4)], "the zoom result is the cropped region"
    assert gap < 0.2, f"the zoom crop blocked the event loop for {gap:.2f}s"


# ---------------------------------------------------------------------------
# Desktop allocation and Chrome start
# ---------------------------------------------------------------------------

class _FakeHandle:
    display_num = 199
    width, height = 1280, 800
    display = ":199"

    def touch(self):
        pass


class _FakeAllocator:
    """Like DisplayAllocator: allocate() is idempotent per session, under a lock."""

    def __init__(self):
        self.handle = None
        self._lock = threading.Lock()

    def get(self, session_id):
        return self.handle

    def allocate(self, session_id, backend="anthropic", operator="system"):
        with self._lock:
            if self.handle is None:
                time.sleep(BLOCK)
                self.handle = _FakeHandle()
            return self.handle


class _FakeChrome:
    def __init__(self):
        self.running = False
        self.launches = 0

    def is_running(self):
        return self.running

    def start(self, url, handle=None):
        if self.running:          # ChromeInstance.start's own guard
            return True
        time.sleep(BLOCK)
        self.launches += 1
        self.running = True
        return True

    def stop(self):
        self.running = False


def test_new_desktop_is_allocated_and_started_off_the_loop(monkeypatch):
    import Orchestrator.browser.display as display
    alloc = _FakeAllocator()
    monkeypatch.setattr(display, "get_allocator", lambda: alloc)
    session = ComputerUseSession("cu-alloc-op")
    session.chrome = _FakeChrome()
    ok, gap = asyncio.run(_max_gap_during(session.ensure_browser("about:blank")))
    assert ok is True and session.display is alloc.handle
    assert gap < 0.2, f"desktop start blocked the event loop for {gap:.2f}s"


def test_concurrent_starts_of_one_session_launch_chrome_once(monkeypatch):
    import Orchestrator.browser.display as display
    alloc = _FakeAllocator()
    monkeypatch.setattr(display, "get_allocator", lambda: alloc)
    session = ComputerUseSession("cu-race-op")
    session.chrome = _FakeChrome()

    async def both():
        return await asyncio.gather(session.ensure_browser("about:blank"),
                                    session.ensure_browser("about:blank"))

    assert asyncio.run(both()) == [True, True]
    assert session.chrome.launches == 1


# ---------------------------------------------------------------------------
# Interactive viewer routes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route,body,helper", [
    ("browser_click", {"x": 5, "y": 5}, "click"),
    ("browser_type", {"text": "hello"}, "type_text"),
    ("browser_key", {"key": "Return"}, "press_key"),
    ("browser_scroll", {"x": 5, "y": 5}, "scroll"),
])
def test_viewer_input_runs_off_the_loop(monkeypatch, route, body, helper):
    from Orchestrator.routes import browser_routes

    def slow(*a, **k):
        time.sleep(BLOCK)
        return {"success": True}

    monkeypatch.setattr(browser_routes, helper, slow)
    result, gap = asyncio.run(_max_gap_during(getattr(browser_routes, route)(body)))
    assert result == {"success": True}
    assert gap < 0.2, f"{route} blocked the event loop for {gap:.2f}s"


def test_viewer_live_screenshot_runs_off_the_loop(monkeypatch, tmp_path):
    import io
    from PIL import Image
    from Orchestrator import config
    import Orchestrator.browser.screenshot as screenshot
    from Orchestrator.routes import browser_routes

    buf = io.BytesIO()
    Image.new("RGBA", (8, 8), (10, 20, 30, 255)).save(buf, format="PNG")
    png = buf.getvalue()

    def slow_capture(*a, **k):
        time.sleep(BLOCK)
        return png

    monkeypatch.setattr(screenshot, "capture_screenshot", slow_capture)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path)
    result, gap = asyncio.run(_max_gap_during(browser_routes.browser_screenshot_live(None)))
    assert result["url"].startswith("/ui/uploads/browser_live_")
    assert (tmp_path / result["url"].rsplit("/", 1)[1]).exists()
    assert gap < 0.2, f"the live screenshot blocked the event loop for {gap:.2f}s"


def test_concurrent_live_screenshots_tolerate_the_cleanup_sweep(monkeypatch, tmp_path):
    """Live saves run in threads now, so one call's keep-last-5 sweep can delete
    a file another call is about to stat. A file that vanishes mid-sweep (a
    dangling link here, deterministically) must not fail a call whose own JPEG
    was saved."""
    import io
    from PIL import Image
    from Orchestrator import config
    import Orchestrator.browser.screenshot as screenshot
    from Orchestrator.routes import browser_routes

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buf, format="PNG")
    png = buf.getvalue()
    monkeypatch.setattr(screenshot, "capture_screenshot", lambda *a, **k: png)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path)
    (tmp_path / "browser_live_1.jpg").symlink_to(tmp_path / "gone.jpg")

    result = asyncio.run(browser_routes.browser_screenshot_live(None))
    assert "error" not in result, result
    assert (tmp_path / result["url"].rsplit("/", 1)[1]).exists()

    async def burst():
        return await asyncio.gather(*[browser_routes.browser_screenshot_live(None)
                                      for _ in range(8)])

    for _ in range(10):
        errors = [r for r in asyncio.run(burst()) if "error" in r]
        assert not errors, errors[:2]
    assert len(list(tmp_path.glob("browser_live_*.jpg"))) <= 5 + 8


def test_gemini_cu_display_setup_runs_off_the_loop(monkeypatch):
    from Orchestrator.gemini_cu import agent_loop
    from Orchestrator.gemini_cu.session_manager import GeminiCUSession

    session = GeminiCUSession("gem-op", "blackbox", "browser")

    def slow_ensure_display(start_url=None):
        time.sleep(BLOCK)
        return False  # allocation failed: the loop reports it and stops

    session.ensure_display = slow_ensure_display

    async def drain():
        return [evt async for evt in agent_loop.run_gemini_cu_loop(session, "open the page")]

    events, gap = asyncio.run(_max_gap_during(drain()))
    assert events and events[0]["type"] == "error"
    assert gap < 0.2, f"Gemini display setup blocked the event loop for {gap:.2f}s"
