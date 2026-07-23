"""OpenAI CUA driver (Responses API) — plan task 13.

The OpenAI client is mocked entirely (no SDK network): a scripted two-step
sequence drives the loop and we assert the action execution, the
previous_response_id / acknowledged_safety_checks continuity contract, and
the shared CU event vocabulary (cu_step/cu_action/cu_screenshot/cu_safety/
content/done/usage/cu_stopped).
"""
from types import SimpleNamespace

import pytest

from Orchestrator.openai_cu import agent_loop as O
from Orchestrator.openai_cu.config import (
    OPENAI_CU_MODEL_DEFAULT, OPENAI_CU_WIDTH, OPENAI_CU_HEIGHT,
)


# ---------------------------------------------------------------------------
# Unit: keypress translation (OpenAI key names -> xdotool combo)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("keys,expected", [
    (["CTRL", "A"], "ctrl+a"),
    (["ctrl", "a"], "ctrl+a"),              # case-insensitive
    (["ENTER"], "Return"),
    (["SHIFT", "ARROWUP"], "shift+Up"),
    (["ALT", "TAB"], "alt+Tab"),
    (["CMD", "C"], "super+c"),
    (["SUPER"], "super"),
    (["META", "L"], "super+l"),
    (["ESC"], "Escape"),
    (["ESCAPE"], "Escape"),
    (["BACKSPACE"], "BackSpace"),
    (["SPACE"], "space"),
    (["ARROWDOWN"], "Down"),
    (["ARROWLEFT"], "Left"),
    (["ARROWRIGHT"], "Right"),
    (["PAGEUP"], "Prior"),
    (["PAGEDOWN"], "Next"),
    (["F5"], "F5"),                          # pass-through
    (["DELETE"], "Delete"),
    (["B"], "b"),                            # single letters lowercase
])
def test_map_openai_keys(keys, expected):
    assert O._map_openai_keys(keys) == expected


# ---------------------------------------------------------------------------
# Unit: scroll direction/amount heuristic (40px per wheel notch)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sx,sy,expected", [
    (0, 120, ("down", 3)),
    (0, -40, ("up", 1)),
    (0, 10, ("down", 1)),     # sub-notch still scrolls one tick
    (0, -10, ("up", 1)),
    (80, 0, ("right", 2)),
    (-200, 0, ("left", 5)),
    (0, 0, ("down", 1)),      # degenerate: default down, one tick
    (50, 120, ("down", 3)),   # vertical wins when both present
])
def test_scroll_heuristic(sx, sy, expected):
    assert O._scroll_direction_amount(sx, sy) == expected


# ---------------------------------------------------------------------------
# Loop integration — fully mocked client
# ---------------------------------------------------------------------------

class FakeExecutor:
    """Session-bound ActionExecutor spy. Module-level (not fixture-local) so
    FakeSession can own an instance — mirroring ComputerUseSession.actions,
    the executor session_manager.ensure_browser re-binds to the session's own
    virtual display (D4). Calls record on the CLASS; the patched_loop fixture
    resets the list per test."""
    calls = []

    def __init__(self, *a, **k):
        pass

    def execute(self, action, **params):
        FakeExecutor.calls.append((action, params))
        return {"success": True, "message": "ok"}


class FakeSession:
    """Minimal ComputerUseSession stand-in (browser/session_manager shape)."""
    def __init__(self):
        self.operator = "TestOp"
        self.screenshot_count = 0
        self.current_step = 0
        self.stop_requested = False
        self.status = "idle"
        self.final_response = ""
        self.total_tokens = {"input": 0, "output": 0}
        self.display = None
        # Mirrors ComputerUseSession.__init__ (session_manager.py): the loop
        # must drive THIS executor (D4), never construct a fresh default.
        self.actions = FakeExecutor()

    def capture_screenshot_bytes(self) -> bytes:
        # Mirror ComputerUseSession.capture_screenshot_bytes (display=None):
        # route through a test-injected O.capture_screenshot seam (set with
        # monkeypatch(raising=False)) so tests drive the loop's screenshot
        # path. Production agent_loop no longer imports capture_screenshot —
        # it calls session.capture_screenshot_bytes() directly.
        return O.capture_screenshot()


class FakeResponses:
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls = []  # kwargs of each create()

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.scripted.pop(0)


class FakeAsyncOpenAI:
    last = None

    def __init__(self, api_key=None, **kw):
        self.responses = FakeResponses(FakeAsyncOpenAI._scripted)
        FakeAsyncOpenAI.last = self


def _scripted_two_step():
    """Response 1: one computer_call click(100,200) with a pending safety
    check.  Response 2: final assistant message with reasoning summary."""
    r1 = SimpleNamespace(
        id="resp_1",
        output=[
            SimpleNamespace(
                type="reasoning",
                summary=[SimpleNamespace(type="summary_text", text="I will click the button.")],
            ),
            SimpleNamespace(
                type="computer_call",
                call_id="call_1",
                action=SimpleNamespace(type="click", x=100, y=200, button="left"),
                pending_safety_checks=[SimpleNamespace(
                    id="sc_1", code="malicious_instructions", message="Check me")],
            ),
        ],
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )
    r2 = SimpleNamespace(
        id="resp_2",
        output=[
            SimpleNamespace(
                type="reasoning",
                summary=[SimpleNamespace(type="summary_text", text="Task finished.")],
            ),
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text="CUA done")],
            ),
        ],
        usage=SimpleNamespace(input_tokens=50, output_tokens=10),
    )
    return [r1, r2]


@pytest.fixture
def patched_loop(monkeypatch):
    """Patch SDK client + screenshots; reset the session-executor spy."""
    FakeExecutor.calls = []
    executor_calls = FakeExecutor.calls

    monkeypatch.setattr(O, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(O, "capture_screenshot", lambda: b"\x89PNGfake", raising=False)
    monkeypatch.setattr(O, "resize_screenshot", lambda png, w, h: png)
    saved = []

    def _fake_save(png, task_id, step):
        saved.append((task_id, step))
        return f"/ui/uploads/{task_id}_step{step:03d}.png"

    monkeypatch.setattr(O, "save_screenshot_to_uploads", _fake_save)
    # no real sleeps in tests
    async def _no_sleep(_secs):
        return None
    monkeypatch.setattr(O.asyncio, "sleep", _no_sleep)

    FakeAsyncOpenAI._scripted = _scripted_two_step()
    FakeAsyncOpenAI.last = None
    monkeypatch.setattr(O, "AsyncOpenAI", FakeAsyncOpenAI)
    return {"executor_calls": executor_calls, "saved": saved}


async def _collect(gen):
    return [e async for e in gen]


@pytest.mark.asyncio
async def test_two_step_loop_contract(patched_loop):
    session = FakeSession()
    events = await _collect(O.run_openai_cu_loop(session, "click the button"))
    types = [e["type"] for e in events]

    # ── ActionExecutor received the click in anthropic-1280 pixel space ──
    assert patched_loop["executor_calls"] == [
        ("left_click", {"coordinate": [100, 200]})]

    client = FakeAsyncOpenAI.last
    assert len(client.responses.calls) == 2
    first, second = client.responses.calls

    # ── First call: tool decl + model default + no previous_response_id ──
    assert first["model"] == OPENAI_CU_MODEL_DEFAULT == "gpt-5.5"
    tool = first["tools"][0]
    # Current contract: bare computer tool — no display/environment fields
    # (coords follow the screenshot's own pixel space; we send 1280x720).
    assert tool == {"type": "computer"}
    assert OPENAI_CU_WIDTH == 1280 and OPENAI_CU_HEIGHT == 720
    assert first.get("reasoning") == {"summary": "concise"}
    assert first.get("truncation") == "auto"
    assert "previous_response_id" not in first

    # ── Second call: continuity + safety acknowledgement pass-through ──
    assert second["previous_response_id"] == "resp_1"
    outputs = [i for i in second["input"] if i.get("type") == "computer_call_output"]
    assert len(outputs) == 1
    out = outputs[0]
    assert out["call_id"] == "call_1"
    assert out["output"]["type"] == "computer_screenshot"
    assert out["output"]["image_url"].startswith("data:image/png;base64,")
    assert out["output"]["detail"] == "original"
    acked = out["acknowledged_safety_checks"]
    assert [c["id"] for c in acked] == ["sc_1"]
    assert acked[0]["code"] == "malicious_instructions"

    # ── Event vocabulary parity ──
    assert "cu_step" in types
    assert "cu_action" in types
    assert "cu_safety" in types
    assert types.count("cu_screenshot") >= 2  # initial + post-action
    done = [e for e in events if e["type"] == "done"]
    assert done and done[0]["data"]["content"] == "CUA done"
    usage = [e for e in events if e["type"] == "usage"]
    assert usage and usage[0]["data"] == {"input": 150, "output": 30}
    assert session.total_tokens == {"input": 150, "output": 30}

    # ── Reasoning summaries / messages surfaced as content events ──
    content_texts = " ".join(
        e["data"]["text"] for e in events if e["type"] == "content")
    assert "I will click the button." in content_texts
    assert "CUA done" in content_texts

    # ── Session bookkeeping ──
    assert session.openai_previous_response_id == "resp_2"
    assert session.final_response == "CUA done"
    assert session.status == "complete"
    assert session.screenshot_count >= 2


@pytest.mark.asyncio
async def test_stop_requested_yields_cu_stopped(patched_loop):
    session = FakeSession()
    session.stop_requested = True
    events = await _collect(O.run_openai_cu_loop(session, "do something"))
    types = [e["type"] for e in events]
    assert "cu_stopped" in types
    # stop fires before any API call
    assert FakeAsyncOpenAI.last is None or FakeAsyncOpenAI.last.responses.calls == []


@pytest.mark.asyncio
async def test_missing_api_key_errors(patched_loop, monkeypatch):
    monkeypatch.setattr(O, "OPENAI_API_KEY", "")
    session = FakeSession()
    events = await _collect(O.run_openai_cu_loop(session, "task"))
    assert events[0]["type"] == "error"
    assert "OPENAI_API_KEY" in events[0]["data"]["message"]


@pytest.mark.asyncio
async def test_multi_turn_continuity_uses_session_response_id(patched_loop):
    """A second chat turn must send previous_response_id from the session."""
    session = FakeSession()
    session.openai_previous_response_id = "resp_prev_turn"
    FakeAsyncOpenAI._scripted = [SimpleNamespace(
        id="resp_3",
        output=[SimpleNamespace(
            type="message",
            content=[SimpleNamespace(type="output_text", text="hello again")])],
        usage=None,
    )]
    await _collect(O.run_openai_cu_loop(session, "second turn"))
    first = FakeAsyncOpenAI.last.responses.calls[0]
    assert first["previous_response_id"] == "resp_prev_turn"


# ---------------------------------------------------------------------------
# Action mapping (beyond click) — drag / move / keypress / scroll dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_action_map_dispatch():
    """Actions dispatch onto the CALLER-SUPPLIED executor (the session-bound
    one in production, D4) — _execute_openai_action never constructs its own."""
    calls = []

    class Spy:
        def execute(self, action, **params):
            calls.append((action, params))
            return {"success": True}

    ex = Spy()
    A = SimpleNamespace
    await O._execute_openai_action(A(type="click", x=1, y=2, button="right"), ex)
    await O._execute_openai_action(A(type="click", x=1, y=2, button="wheel"), ex)
    await O._execute_openai_action(A(type="double_click", x=3, y=4), ex)
    await O._execute_openai_action(A(type="scroll", x=5, y=6, scroll_x=0, scroll_y=120), ex)
    await O._execute_openai_action(A(type="type", text="hi"), ex)
    await O._execute_openai_action(A(type="keypress", keys=["CTRL", "A"]), ex)
    await O._execute_openai_action(
        A(type="drag", path=[{"x": 1, "y": 1}, {"x": 9, "y": 9}]), ex)
    await O._execute_openai_action(A(type="move", x=7, y=8), ex)
    res = await O._execute_openai_action(A(type="bogus"), ex)

    assert calls == [
        ("right_click", {"coordinate": [1, 2]}),
        ("middle_click", {"coordinate": [1, 2]}),
        ("double_click", {"coordinate": [3, 4]}),
        ("scroll", {"coordinate": [5, 6], "direction": "down", "amount": 3}),
        ("type", {"text": "hi"}),
        ("key", {"text": "ctrl+a"}),
        ("left_click_drag", {"start_coordinate": [1, 1], "coordinate": [9, 9]}),
        ("mouse_move", {"coordinate": [7, 8]}),
    ]
    assert res["success"] is False  # unknown action reported, not raised


@pytest.mark.asyncio
async def test_screenshot_and_wait_actions_no_executor(monkeypatch):
    """screenshot/wait never touch the executor — None would AttributeError."""
    async def _no_sleep(_secs):
        return None
    monkeypatch.setattr(O.asyncio, "sleep", _no_sleep)
    r1 = await O._execute_openai_action(SimpleNamespace(type="screenshot"), None)
    r2 = await O._execute_openai_action(SimpleNamespace(type="wait"), None)
    assert r1["success"] and r2["success"]


@pytest.mark.asyncio
async def test_loop_drives_session_bound_executor(patched_loop, monkeypatch):
    """D4 display-binding fix: the loop executes actions via session.actions
    (the executor ensure_browser re-binds to the session's OWN virtual display,
    session_manager.py:214-219) — exactly like the Anthropic path. A fresh
    default ActionExecutor() would act on the GLOBAL display no virtual
    session is on."""
    private_calls = []

    class SessionBound:
        def execute(self, action, **params):
            private_calls.append((action, params))
            return {"success": True}

    session = FakeSession()
    session.actions = SessionBound()  # simulate the ensure_browser re-bind
    events = await _collect(O.run_openai_cu_loop(session, "click the button"))

    # The click went through the SESSION'S executor…
    assert private_calls == [("left_click", {"coordinate": [100, 200]})]
    # …and never through a default/module-level one.
    assert patched_loop["executor_calls"] == []
    assert not any(e["type"] == "error" for e in events)


# ---------------------------------------------------------------------------
# Dispatch wiring — chat_routes routes backend=="openai" to the new stream
# ---------------------------------------------------------------------------

def test_chat_routes_dispatch_wires_openai():
    import inspect
    from Orchestrator.routes import chat_routes
    src = inspect.getsource(chat_routes)
    assert "stream_openai_computer_use" in src
    assert 'backend == "openai"' in src
    assert "wired in CU plan task 13" not in src, "Task-13 TODO comments must be gone"
    # operator must be caller-supplied (same ratchet as the other CU streams)
    p = inspect.signature(
        chat_routes.stream_openai_computer_use).parameters["operator"]
    assert p.default is inspect.Parameter.empty


def test_openai_cu_default_matches_dispatch_filter():
    import re
    from Orchestrator.config import CU_MODEL_FILTERS
    assert re.match(CU_MODEL_FILTERS["openai"], OPENAI_CU_MODEL_DEFAULT)
    from Orchestrator.browser.dispatch import resolve_backend
    assert resolve_backend(OPENAI_CU_MODEL_DEFAULT) == "openai"


# ---------------------------------------------------------------------------
# Review fixes C1/C2/I1 — abnormal-exit continuity, screenshot fallback,
# per-turn context refresh
# ---------------------------------------------------------------------------

def _message_response(rid="resp_done", text="all done"):
    return SimpleNamespace(
        id=rid,
        output=[SimpleNamespace(
            type="message",
            content=[SimpleNamespace(type="output_text", text=text)])],
        usage=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_start", [None, "resp_prior"])
async def test_estop_mid_turn_restores_turn_start_id(
        patched_loop, monkeypatch, turn_start):
    """E-stop after step 1's create: the session id must roll back to its
    TURN-START value, not stay pointing at resp_1 (whose computer_calls were
    never answered — continuing from it would 400 forever)."""
    session = FakeSession()
    if turn_start is not None:
        session.openai_previous_response_id = turn_start
    r1 = _scripted_two_step()[0]  # one computer_call

    class StopAfterCreate:
        def __init__(self, api_key=None, **kw):
            self.responses = self
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            session.stop_requested = True  # E-stop lands mid-turn
            return r1

    monkeypatch.setattr(O, "AsyncOpenAI", StopAfterCreate)
    events = await _collect(O.run_openai_cu_loop(session, "task"))
    assert "cu_stopped" in [e["type"] for e in events]
    assert session.openai_previous_response_id == turn_start


@pytest.mark.asyncio
async def test_api_error_restores_id_and_yields_error(patched_loop, monkeypatch):
    """Non-retriable API error -> id back at turn start + error event."""
    session = FakeSession()
    session.openai_previous_response_id = "resp_prior"

    class Raising:
        def __init__(self, api_key=None, **kw):
            self.responses = self

        async def create(self, **kwargs):
            raise Exception("boom 500")

    monkeypatch.setattr(O, "AsyncOpenAI", Raising)
    events = await _collect(O.run_openai_cu_loop(session, "task"))
    errors = [e for e in events if e["type"] == "error"]
    assert errors and "boom 500" in errors[0]["data"]["message"]
    assert session.openai_previous_response_id == "resp_prior"
    assert session.status == "error"


@pytest.mark.asyncio
async def test_stale_response_id_retries_fresh_once(patched_loop, monkeypatch):
    """'No tool output found' on create -> ONE retry without
    previous_response_id and with the developer message re-included."""
    session = FakeSession()
    session.openai_previous_response_id = "resp_poisoned"
    created = []

    class StaleThenOk:
        def __init__(self, api_key=None, **kw):
            self.responses = self
            self.calls = []
            created.append(self)

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise Exception(
                    "Error code: 400 - No tool output found for computer "
                    "call call_x.")
            return _message_response()

    monkeypatch.setattr(O, "AsyncOpenAI", StaleThenOk)
    events = await _collect(O.run_openai_cu_loop(session, "task"))

    first, second = created[0].calls
    assert first["previous_response_id"] == "resp_poisoned"
    assert "previous_response_id" not in second           # fresh conversation
    assert second["input"][0]["role"] == "developer"      # instructions re-sent
    assert [e for e in events if e["type"] == "done"]
    assert session.openai_previous_response_id == "resp_done"


@pytest.mark.asyncio
async def test_post_action_screenshot_retry_once_then_continue(
        patched_loop, monkeypatch):
    """Post-action capture raises once, the retry succeeds -> loop continues
    and finishes normally."""
    calls = {"n": 0}

    def flaky_capture():
        calls["n"] += 1
        if calls["n"] == 2:  # first post-action attempt fails
            raise RuntimeError("scrot died")
        return b"PNG-%d" % calls["n"]

    monkeypatch.setattr(O, "capture_screenshot", flaky_capture, raising=False)
    session = FakeSession()
    events = await _collect(O.run_openai_cu_loop(session, "click"))
    done = [e for e in events if e["type"] == "done"]
    assert done and done[0]["data"]["content"] == "CUA done"
    assert calls["n"] == 3  # initial + failed post-action + successful retry
    out = [i for i in FakeAsyncOpenAI.last.responses.calls[1]["input"]
           if i.get("type") == "computer_call_output"][0]
    assert out["output"]["type"] == "computer_screenshot"


@pytest.mark.asyncio
async def test_post_action_screenshot_persistent_failure_reuses_last_good(
        patched_loop, monkeypatch):
    """Capture keeps failing after a good initial screenshot -> the previous
    good bytes are sent (shape-valid computer_screenshot, never input_text)."""
    calls = {"n": 0}
    good = b"GOOD-INITIAL-PNG"

    def capture():
        calls["n"] += 1
        if calls["n"] == 1:
            return good
        raise RuntimeError("scrot died")

    monkeypatch.setattr(O, "capture_screenshot", capture, raising=False)
    session = FakeSession()
    events = await _collect(O.run_openai_cu_loop(session, "click"))
    out = [i for i in FakeAsyncOpenAI.last.responses.calls[1]["input"]
           if i.get("type") == "computer_call_output"][0]
    assert out["output"]["type"] == "computer_screenshot"
    assert out["output"]["image_url"] == (
        "data:image/png;base64," + O.screenshot_to_base64(good))
    assert [e for e in events if e["type"] == "done"]  # loop survived
    assert calls["n"] == 3  # initial + post-action attempt + retry


@pytest.mark.asyncio
async def test_capture_never_succeeds_aborts_cleanly(patched_loop, monkeypatch):
    """No screenshot has EVER succeeded -> clean error, id stays at its
    turn-start value (never poisoned), no API call issued."""
    def always_fail():
        raise RuntimeError("no display")

    monkeypatch.setattr(O, "capture_screenshot", always_fail, raising=False)
    session = FakeSession()
    session.openai_previous_response_id = "resp_prior"
    events = await _collect(O.run_openai_cu_loop(session, "click"))
    errors = [e for e in events if e["type"] == "error"]
    assert errors and "screenshot" in errors[0]["data"]["message"].lower()
    assert session.openai_previous_response_id == "resp_prior"
    assert FakeAsyncOpenAI.last is None or FakeAsyncOpenAI.last.responses.calls == []


@pytest.mark.asyncio
async def test_continuation_turn_carries_context_refresh(patched_loop):
    """Turn 2+ with a system_prompt: the per-turn fossil context must reach
    the model as an extra input_text part on the user message — the
    developer message is first-turn-only (I1)."""
    session = FakeSession()
    session.openai_previous_response_id = "resp_prev_turn"
    FakeAsyncOpenAI._scripted = [_message_response()]
    await _collect(O.run_openai_cu_loop(
        session, "second turn", system_prompt="CTX"))
    first = FakeAsyncOpenAI.last.responses.calls[0]
    roles = [i.get("role") for i in first["input"]]
    assert "developer" not in roles
    user = [i for i in first["input"] if i.get("role") == "user"][0]
    texts = [p["text"] for p in user["content"] if p["type"] == "input_text"]
    assert texts[0] == "second turn"                 # prompt part first
    assert any("CTX" in t for t in texts[1:])        # context refresh after
    assert "[Context refresh]" in texts[1]


@pytest.mark.asyncio
async def test_batched_actions_one_output_per_call(patched_loop):
    """gpt-5.5 batches multiple actions into one computer_call via the
    `actions` array; the driver executes them in order and answers the
    call_id with ONE screenshot output (detail=original)."""
    r1 = SimpleNamespace(
        id="resp_b1",
        output=[SimpleNamespace(
            type="computer_call",
            call_id="call_b1",
            action=None,  # new contract: batch rides in `actions`
            actions=[
                SimpleNamespace(type="click", x=10, y=20, button="left"),
                SimpleNamespace(type="type", text="hello"),
            ],
            pending_safety_checks=[],
        )],
        usage=SimpleNamespace(input_tokens=10, output_tokens=2),
    )
    r2 = SimpleNamespace(
        id="resp_b2",
        output=[SimpleNamespace(
            type="message",
            content=[SimpleNamespace(type="output_text", text="batched done")],
        )],
        usage=SimpleNamespace(input_tokens=5, output_tokens=1),
    )
    FakeAsyncOpenAI._scripted = [r1, r2]

    session = FakeSession()
    events = await _collect(O.run_openai_cu_loop(session, "do two things"))

    # Both batched actions executed, in order
    assert patched_loop["executor_calls"] == [
        ("left_click", {"coordinate": [10, 20]}),
        ("type", {"text": "hello"}),
    ]
    # Exactly ONE computer_call_output answers the call, detail=original
    second = FakeAsyncOpenAI.last.responses.calls[1]
    outputs = [i for i in second["input"] if i.get("type") == "computer_call_output"]
    assert len(outputs) == 1
    assert outputs[0]["call_id"] == "call_b1"
    assert outputs[0]["output"]["detail"] == "original"
    # One cu_action event per batched action
    action_events = [e for e in events if e["type"] == "cu_action"]
    assert [e["data"]["action"] for e in action_events] == ["click", "type"]
    assert any(e["type"] == "done" for e in events)
