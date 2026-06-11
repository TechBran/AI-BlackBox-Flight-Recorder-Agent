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
    """Patch SDK client, screenshots, and ActionExecutor; return spies."""
    executor_calls = []

    class FakeExecutor:
        def __init__(self, *a, **k):
            pass

        def execute(self, action, **params):
            executor_calls.append((action, params))
            return {"success": True, "message": "ok"}

    monkeypatch.setattr(O, "ActionExecutor", FakeExecutor)
    monkeypatch.setattr(O, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(O, "capture_screenshot", lambda: b"\x89PNGfake")
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
    assert first["model"] == OPENAI_CU_MODEL_DEFAULT
    tool = first["tools"][0]
    assert tool["type"] == "computer_use_preview"
    assert tool["display_width"] == OPENAI_CU_WIDTH == 1280
    assert tool["display_height"] == OPENAI_CU_HEIGHT == 720
    assert tool["environment"] == "browser"
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
async def test_action_map_dispatch(monkeypatch):
    calls = []

    class FakeExecutor:
        def __init__(self, *a, **k):
            pass

        def execute(self, action, **params):
            calls.append((action, params))
            return {"success": True}

    monkeypatch.setattr(O, "ActionExecutor", FakeExecutor)

    A = SimpleNamespace
    await O._execute_openai_action(A(type="click", x=1, y=2, button="right"))
    await O._execute_openai_action(A(type="click", x=1, y=2, button="wheel"))
    await O._execute_openai_action(A(type="double_click", x=3, y=4))
    await O._execute_openai_action(A(type="scroll", x=5, y=6, scroll_x=0, scroll_y=120))
    await O._execute_openai_action(A(type="type", text="hi"))
    await O._execute_openai_action(A(type="keypress", keys=["CTRL", "A"]))
    await O._execute_openai_action(
        A(type="drag", path=[{"x": 1, "y": 1}, {"x": 9, "y": 9}]))
    await O._execute_openai_action(A(type="move", x=7, y=8))
    res = await O._execute_openai_action(A(type="bogus"))

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
    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("executor must not be constructed")

    monkeypatch.setattr(O, "ActionExecutor", Boom)
    async def _no_sleep(_secs):
        return None
    monkeypatch.setattr(O.asyncio, "sleep", _no_sleep)
    r1 = await O._execute_openai_action(SimpleNamespace(type="screenshot"))
    r2 = await O._execute_openai_action(SimpleNamespace(type="wait"))
    assert r1["success"] and r2["success"]


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
