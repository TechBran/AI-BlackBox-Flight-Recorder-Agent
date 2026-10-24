"""CU input actions report what actually happened (audit O13-computer-use-04,
-07, -18; W19).

* A non-zero xdotool/ydotool exit or a timeout is a FAILED action, never
  {"success": True} on a stale screen.
* Long text is typed in bounded chunks with a timeout sized to the chunk, so a
  >830-char `type` is no longer killed mid-text and reported as "Typed N chars".
* Typed text follows `--` (and `--escape=0` for ydotool): a leading '-' is not an
  option and a backslash is typed as a backslash.
* Scroll is clamped to 1..50 ticks on the local and the remote path; Gemini's
  0-999 scroll magnitude becomes ticks instead of a raw click count.
* cursor_position is implemented (local + remote), the remote path supports
  zoom / left_mouse_down / left_mouse_up, and the driver hands a position back
  as text.
"""
import subprocess

import pytest

from Orchestrator.browser import actions as act


def _ok(cmd=None):
    return subprocess.CompletedProcess(cmd or [], 0, stdout="", stderr="")


@pytest.fixture
def xdo(monkeypatch):
    """A virtual-display executor whose xdotool calls are recorded."""
    monkeypatch.setattr(act.time, "sleep", lambda s: None)
    calls = []
    results = {}

    def _run(*args, **kw):
        calls.append(args)
        return results.get(args[0], _ok(list(args)))

    monkeypatch.setattr(act, "_run_xdotool", _run)
    ex = act.ActionExecutor(native_mode=False)
    ex.use_ydotool = False
    return ex, calls, results


@pytest.fixture
def ydo(monkeypatch):
    monkeypatch.setattr(act.time, "sleep", lambda s: None)
    calls = []
    monkeypatch.setattr(act, "_run_ydotool", lambda *a, **k: calls.append(a) or _ok(list(a)))
    monkeypatch.setattr(act, "_run_xdotool", lambda *a, **k: calls.append(("xdotool",) + a) or _ok())
    ex = act.ActionExecutor(native_mode=False)
    ex.use_ydotool = True
    return ex, calls


# ── return codes and timeouts ───────────────────────────────────────────────

def test_nonzero_exit_fails_the_click(xdo):
    ex, _, results = xdo
    results["click"] = subprocess.CompletedProcess([], 1, stdout="",
                                                   stderr="Error: Can't open display: :99")
    r = ex.execute("left_click", coordinate=[10, 10])
    assert r["success"] is False
    assert "Can't open display" in r["message"]


def test_timeout_fails_the_action(monkeypatch):
    monkeypatch.setattr(act.time, "sleep", lambda s: None)

    def _hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(act.subprocess, "run", _hang)
    ex = act.ActionExecutor(native_mode=False)
    ex.use_ydotool = False
    r = ex.execute("key", text="Return")
    assert r["success"] is False
    assert "timed out" in r["message"]


@pytest.mark.parametrize("action,params", [
    ("mouse_move", {"coordinate": [1, 2]}),
    ("type", {"text": "hello"}),
    ("left_mouse_down", {}),
    ("left_mouse_up", {}),
    ("hold_key", {"text": "shift", "duration": 0}),
    ("scroll", {"direction": "down", "amount": 2}),
])
def test_every_input_action_surfaces_a_failure(xdo, action, params):
    ex, _, results = xdo
    for verb in ("mousemove", "type", "mousedown", "mouseup", "keydown", "keyup", "click"):
        results[verb] = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
    assert ex.execute(action, **params)["success"] is False


def test_drag_releases_the_button_when_the_move_fails(xdo, monkeypatch):
    ex, calls, _ = xdo
    moves = {"n": 0}

    # The first mousemove (to the start) works; the second (to the end) fails.
    def _run(*args, **kw):
        calls.append(args)
        if args[0] == "mousemove":
            moves["n"] += 1
            if moves["n"] == 2:
                return subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        return _ok()

    monkeypatch.setattr(act, "_run_xdotool", _run)
    r = ex.execute("left_click_drag", start_coordinate=[1, 1], coordinate=[5, 5])
    assert r["success"] is False
    assert calls[-1][0] == "mouseup", "a failed drag must not leave the button held"


def test_type_run_timeout_scales_with_text(monkeypatch):
    seen = []

    def _fake_run(cmd, **kw):
        seen.append(kw["timeout"])
        return _ok(cmd)

    monkeypatch.setattr(act.subprocess, "run", _fake_run)
    act._run_xdotool("type", "--", "x" * 150, display_number=99, native=False,
                     timeout=act._type_timeout(150))
    act._run_xdotool("key", "Return", display_number=99, native=False)
    assert seen[0] > seen[1] >= 10


# ── typing: chunks, `--`, escapes ───────────────────────────────────────────

def test_long_text_is_typed_in_chunks_and_all_of_it_lands(xdo):
    ex, calls, _ = xdo
    text = "a" * 2000
    r = ex.execute("type", text=text)
    assert r["success"] is True
    typed = [c for c in calls if c[0] == "type"]
    assert len(typed) > 1
    assert "".join(c[-1] for c in typed) == text
    for c in typed:
        assert c[-2] == "--"
        assert len(c[-1]) <= act._TYPE_CHUNK_CHARS


def test_xdotool_type_puts_text_after_double_dash(xdo):
    ex, calls, _ = xdo
    ex.execute("type", text="-n C:\\new\\table")
    assert calls[-1] == ("type", "--clearmodifiers", "--delay", "12", "--", "-n C:\\new\\table")


def test_ydotool_type_disables_escapes_and_ends_options(ydo):
    ex, calls = ydo
    ex.execute("type", text="-n C:\\new\\table")
    assert calls[-1] == ("type", "--escape=0", "--key-delay", "12", "--", "-n C:\\new\\table")


def test_a_failed_chunk_stops_typing_and_says_how_much_landed(xdo, monkeypatch):
    ex, calls, _ = xdo
    n = {"i": 0}

    def _run(*args, **kw):
        calls.append(args)
        if args[0] == "type":
            n["i"] += 1
            if n["i"] == 2:
                return subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        return _ok()

    monkeypatch.setattr(act, "_run_xdotool", _run)
    r = ex.execute("type", text="b" * (act._TYPE_CHUNK_CHARS * 3))
    assert r["success"] is False
    assert f"{act._TYPE_CHUNK_CHARS} of {act._TYPE_CHUNK_CHARS * 3}" in r["message"]
    assert n["i"] == 2


# ── scroll clamp ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("amount,ticks", [(100000, 50), (51, 50), (50, 50), (0, 1),
                                          (-5, 1), ("7", 7), ("lots", 3), (None, 3)])
def test_scroll_is_clamped(xdo, amount, ticks):
    ex, calls, _ = xdo
    params = {"direction": "down"}
    if amount is not None:
        params["amount"] = amount
    assert ex.execute("scroll", **params)["success"] is True
    assert len([c for c in calls if c[0] == "click"]) == ticks


def test_ydotool_scroll_is_clamped(ydo):
    ex, calls = ydo
    ex.execute("scroll", direction="up", amount=100000)
    assert len([c for c in calls if c[:2] == ("mousemove", "--wheel")]) == 50


# ── remote (VNC) path ───────────────────────────────────────────────────────

class _Dev:
    tailscale_ip, vnc_port, metadata = "100.0.0.1", 5900, {}


class _Reg:
    def get_device(self, _id):
        return _Dev()


class _FakeVNC:
    log = []
    fail = set()

    def __init__(self, *a):
        pass

    async def _rec(self, *call):
        _FakeVNC.log.append(call)
        if call[0] in _FakeVNC.fail:
            raise RuntimeError(f"VNC {call[0]} failed: connection refused")

    async def click(self, x, y, button=1):
        await self._rec("click", x, y, button)

    async def move(self, x, y):
        await self._rec("move", x, y)

    async def type_text(self, text):
        await self._rec("type", text)

    async def key(self, k):
        await self._rec("key", k)

    async def scroll_click(self, button):
        await self._rec("scroll", button)

    async def mouse_down(self, button=1):
        await self._rec("mousedown", button)

    async def mouse_up(self, button=1):
        await self._rec("mouseup", button)

    async def key_down(self, k):
        await self._rec("keydown", k)

    async def key_up(self, k):
        await self._rec("keyup", k)


@pytest.fixture
def remote(monkeypatch):
    _FakeVNC.log = []
    _FakeVNC.fail = set()
    act._REMOTE_POINTER.clear()
    monkeypatch.setattr("Orchestrator.device_registry.get_registry", lambda: _Reg())
    monkeypatch.setattr("Orchestrator.remote_desktop.VNCClient", _FakeVNC)
    return _FakeVNC


@pytest.mark.asyncio
async def test_remote_scroll_is_clamped(remote):
    r = await act.execute_remote_action("dev-1", "scroll", coordinate=[5, 5],
                                        direction="down", amount=100000)
    assert r["success"] is True
    assert len([c for c in remote.log if c[0] == "scroll"]) == 50


@pytest.mark.asyncio
async def test_remote_failure_is_reported(remote):
    remote.fail = {"click"}
    r = await act.execute_remote_action("dev-1", "left_click", coordinate=[5, 5])
    assert r["success"] is False and "connection refused" in r["error"]


@pytest.mark.asyncio
async def test_remote_zoom_and_mouse_hold(remote):
    assert (await act.execute_remote_action("dev-1", "zoom", region=[0, 0, 10, 10]))["success"]
    assert not (await act.execute_remote_action("dev-1", "zoom", region=[1, 2]))["success"]
    assert (await act.execute_remote_action("dev-1", "left_mouse_down"))["success"]
    assert (await act.execute_remote_action("dev-1", "left_mouse_up"))["success"]
    assert [c[0] for c in remote.log] == ["mousedown", "mouseup"]


@pytest.mark.asyncio
async def test_remote_cursor_position_tracks_the_last_pointer(remote):
    r = await act.execute_remote_action("dev-1", "cursor_position")
    assert r["success"] is False                       # honest: nothing moved yet
    await act.execute_remote_action("dev-1", "mouse_move", coordinate=[40, 50])
    r = await act.execute_remote_action("dev-1", "cursor_position")
    assert r["success"] is True and r["coordinate"] == [40, 50]


@pytest.mark.asyncio
async def test_remote_empty_type_is_a_noop(remote):
    r = await act.execute_remote_action("dev-1", "type", text="")
    assert r["success"] is True
    assert remote.log == []


# ── cursor_position (local) ─────────────────────────────────────────────────

def test_cursor_position_reads_xdotool_in_model_space(xdo):
    ex, calls, results = xdo
    results["getmouselocation"] = subprocess.CompletedProcess(
        [], 0, stdout="X=640\nY=360\nSCREEN=0\nWINDOW=1\n", stderr="")
    r = ex.execute("cursor_position")
    assert r["success"] is True
    assert r["coordinate"] == [640, 360]
    assert "640" in r["message"] and "360" in r["message"]


def test_cursor_position_converts_native_pixels_back_to_model_space(monkeypatch):
    monkeypatch.setattr(act.time, "sleep", lambda s: None)
    monkeypatch.setattr("Orchestrator.browser.config.detect_native_resolution",
                        lambda force=False: (2560, 1440))
    monkeypatch.setattr(act, "_run_xdotool", lambda *a, **k: subprocess.CompletedProcess(
        [], 0, stdout="X=1280\nY=720\n", stderr=""))
    ex = act.ActionExecutor(native_mode=True)
    ex.use_ydotool = False
    assert ex.execute("cursor_position")["coordinate"] == [640, 360]


def test_cursor_position_on_ydotool_uses_the_last_move(ydo):
    ex, _ = ydo
    ex.execute("mouse_move", coordinate=[12, 34])
    assert ex.execute("cursor_position")["coordinate"] == [12, 34]


def test_every_documented_action_has_a_handler():
    members = ["screenshot", "left_click", "right_click", "middle_click", "double_click",
               "triple_click", "type", "key", "mouse_move", "scroll", "left_click_drag",
               "left_mouse_down", "left_mouse_up", "hold_key", "wait", "zoom",
               "cursor_position"]
    for name in members:
        assert hasattr(act.ActionExecutor, f"_action_{name}"), name


# ── Gemini scroll magnitude ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gemini_scroll_magnitude_becomes_ticks():
    from Orchestrator.gemini_cu import agent_loop as G
    calls = []

    class FakeExecutor:
        coord_space = "gemini-999"

        def execute(self, action, **params):
            calls.append((action, params))
            return {"success": True}

        def to_native(self, x, y):           # a 1440x900 session display
            return int(x / 1000 * 1440), int(y / 1000 * 900)

    class S:
        environment = "desktop"; device_id = "blackbox"
        native_mode = False; actions = FakeExecutor()

    await G._execute_predefined_action(S(), "scroll_at",
                                       {"x": 500, "y": 500, "direction": "down", "magnitude": 800})
    await G._execute_predefined_action(S(), "scroll_at",
                                       {"x": 500, "y": 500, "direction": "down"})
    await G._execute_predefined_action(S(), "scroll_at",
                                       {"x": 500, "y": 500, "direction": "right", "magnitude": 999})
    amounts = [p["amount"] for a, p in calls if a == "scroll"]
    assert amounts == [7, 7, 14]                    # 720px, default 800, 1438px


@pytest.mark.asyncio
async def test_gemini_type_text_at_reports_a_failed_type():
    from Orchestrator.gemini_cu import agent_loop as G

    class FakeExecutor:
        coord_space = "gemini-999"

        def execute(self, action, **params):
            if action == "type":
                return {"success": False, "message": "Action 'type' failed: boom"}
            return {"success": True}

        def to_native(self, x, y):
            return x, y

    class S:
        environment = "desktop"; device_id = "blackbox"
        native_mode = False; actions = FakeExecutor()

    r = await G._execute_predefined_action(S(), "type_text_at", {"x": 1, "y": 1, "text": "hi"})
    assert r["success"] is False


# ── moves onto the pointer's current position ───────────────────────────────
# xdotool 3.20160805's `mousemove --sync` waits for the pointer to LEAVE its
# start position, so a move to where the pointer already is never returns. Once
# a timeout became a failed action, a second scroll at one spot, a click after a
# hover, and the first Gemini scroll_document on a fresh Xvfb (whose pointer
# starts at the centre) all failed after 10 s without acting (review blocker).

def _at(x, y):
    return subprocess.CompletedProcess([], 0, stdout=f"X={x}\nY={y}\nSCREEN=0\nWINDOW=1\n",
                                       stderr="")


def test_move_onto_the_current_pointer_position_is_skipped(xdo):
    ex, calls, results = xdo
    results["getmouselocation"] = _at(300, 300)
    r = ex.execute("left_click", coordinate=[300, 300])
    assert r["success"] is True
    assert not [c for c in calls if c[0] == "mousemove"], \
        "a --sync move onto the pointer's own position hangs; it must not be issued"
    assert ("click", "1") in calls


def test_sync_timeout_with_the_pointer_on_target_is_success(xdo, monkeypatch):
    ex, calls, _ = xdo
    where = {"pos": (5, 5)}

    def _run(*args, **kw):
        calls.append(args)
        if args[0] == "getmouselocation":
            return _at(*where["pos"])
        if args[0] == "mousemove":
            where["pos"] = (int(args[-2]), int(args[-1]))   # the warp lands...
            return subprocess.CompletedProcess(               # ...but --sync never returns
                [], -1, stdout="", stderr="ACTION_TIMEOUT: xdotool command timed out after 10s")
        return _ok()

    monkeypatch.setattr(act, "_run_xdotool", _run)
    r = ex.execute("mouse_move", coordinate=[40, 50])
    assert r["success"] is True, r


def test_sync_timeout_with_the_pointer_elsewhere_still_fails(xdo, monkeypatch):
    ex, calls, _ = xdo

    def _run(*args, **kw):
        calls.append(args)
        if args[0] == "getmouselocation":
            return _at(5, 5)                                  # never moved
        if args[0] == "getdisplaygeometry":
            return subprocess.CompletedProcess([], 0, stdout="1280 720\n", stderr="")
        if args[0] == "mousemove":
            return subprocess.CompletedProcess(
                [], -1, stdout="", stderr="ACTION_TIMEOUT: xdotool command timed out after 10s")
        return _ok()

    monkeypatch.setattr(act, "_run_xdotool", _run)
    r = ex.execute("left_click", coordinate=[40, 50])
    assert r["success"] is False
    assert "timed out" in r["message"]
    assert not [c for c in calls if c[0] == "click"], "the click must not land elsewhere"


def test_sync_timeout_at_a_clamped_screen_edge_is_success(xdo, monkeypatch):
    """A target past the screen edge clamps to the last pixel; when the pointer
    already sits there --sync never sees a move, yet the pointer IS on target."""
    ex, calls, _ = xdo

    def _run(*args, **kw):
        calls.append(args)
        if args[0] == "getmouselocation":
            return _at(1279, 400)
        if args[0] == "getdisplaygeometry":
            return subprocess.CompletedProcess([], 0, stdout="1280 800\n", stderr="")
        if args[0] == "mousemove":
            return subprocess.CompletedProcess(
                [], -1, stdout="", stderr="ACTION_TIMEOUT: xdotool command timed out after 10s")
        return _ok()

    monkeypatch.setattr(act, "_run_xdotool", _run)
    assert ex.execute("mouse_move", coordinate=[1280, 400])["success"] is True


def _free_display():
    import os
    for n in range(420, 480):
        if not os.path.exists(f"/tmp/.X11-unix/X{n}") and not os.path.exists(f"/tmp/.X{n}-lock"):
            return n
    return None


@pytest.fixture
def real_xvfb(tmp_path, monkeypatch):
    """A throwaway Xvfb (no TCP) for the xdotool behaviour fakes cannot show.

    The suite refuses the host's xdotool (conftest.py isolation), so a copy of
    the binary under tmp_path drives this test's own display and nothing else.
    """
    import os
    import shutil
    import time as _t
    xdotool = shutil.which("xdotool")
    if not (shutil.which("Xvfb") and xdotool):
        pytest.skip("Xvfb/xdotool not installed")
    local = tmp_path / "xdotool"
    shutil.copy2(xdotool, local)
    real_run = subprocess.run

    def _run(cmd, *a, **kw):
        if cmd and cmd[0] == "xdotool":
            cmd = [str(local)] + list(cmd[1:])
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(act.subprocess, "run", _run)
    n = _free_display()
    if n is None:
        pytest.skip("no free X display number")
    proc = subprocess.Popen(["Xvfb", f":{n}", "-screen", "0", "1440x900x24",
                             "-nolisten", "tcp", "-noreset"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if os.path.exists(f"/tmp/.X11-unix/X{n}"):
                break
            _t.sleep(0.1)
        else:
            pytest.skip("Xvfb did not start")
        yield n
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_real_xdotool_same_point_actions_succeed_quickly(real_xvfb):
    import time as _t
    ex = act.ActionExecutor(display_number=real_xvfb, coord_space=act.COORD_SPACE_GEMINI,
                            native_mode=False, resolution=(1440, 900))
    assert ex.use_ydotool is False
    steps = [
        # Gemini scroll_document always scrolls at [500,500] = the fresh pointer.
        ("scroll", {"coordinate": [500, 500], "direction": "down", "amount": 2}),
        ("scroll", {"coordinate": [500, 500], "direction": "down", "amount": 2}),
        ("mouse_move", {"coordinate": [200, 300]}),
        ("left_click", {"coordinate": [200, 300]}),
        ("left_click_drag", {"start_coordinate": [200, 300], "coordinate": [200, 300]}),
    ]
    for action, params in steps:
        t0 = _t.monotonic()
        r = ex.execute(action, **params)
        took = _t.monotonic() - t0
        assert r["success"] is True, (action, r)
        assert took < 5, f"{action} took {took:.1f}s"
    assert ex.execute("cursor_position")["coordinate"] == [200, 300]
