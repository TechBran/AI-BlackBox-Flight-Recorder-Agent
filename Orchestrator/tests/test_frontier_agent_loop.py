"""Unit tests for the M2 frontier ReAct loop (Orchestrator/frontier_agent_loop.py).

Fully mocked — no real device, no real Gemini. The Gemini driver is replaced with a
``FakeDriver`` (canned action sequences) via monkeypatching ``_make_driver``; the phone
endpoints are replaced by monkeypatching the ``_pull_observation`` / ``_post_action`` seams.
Proves: a multi-step task (open_app → type → tap → done) marshals the correct ``action``
frames onto the wire; grounding is applied end-to-end; the embedded follow-on observation is
consumed (no double-observe); and timeouts / retries / structured errors behave.
"""
import asyncio
import json

import pytest

from Orchestrator import frontier_agent_loop as fal
from Orchestrator.frontier_agent_loop import Decision


# ── a device screen the grounding can resolve deterministically ──────────────────────
def _node(node_id, bounds, *, clickable=False, editable=False, resource_id="", role="View"):
    return {"node_id": node_id, "role": role, "text": "", "resource_id": resource_id,
            "bounds": bounds, "clickable": clickable, "editable": editable, "is_password": False}


# device 1080×2400 (from the full-screen node's bounds); an editable field around px(540,720)
# and a clickable button around px(540,1681).
OBS = {
    "msg": "observation",
    "ui_tree": [
        _node(0, "0,0,1080,2400", clickable=True, resource_id="root"),
        _node(1, "400,600,700,850", editable=True, resource_id="app:id/field"),
        _node(2, "400,1600,700,1750", clickable=True, resource_id="app:id/submit"),
    ],
    "device_capability": {"formFactor": "phone", "hasScreenshot": False,
                          "supportsCoordinateGesture": True, "displayId": 0},
    "timestamp": 1,
}


class FakeDriver:
    """A canned frontier driver. ``script`` is a list of normalized model actions (dicts) or
    the sentinel 'done'. Records the last_result it was handed each turn."""

    def __init__(self, script):
        self._script = list(script)
        self.seen_results = []

    async def next_action(self, observation, last_result):
        self.seen_results.append(last_result)
        if not self._script:
            return Decision(kind="done", text="Task complete.")
        item = self._script.pop(0)
        if item == "done":
            return Decision(kind="done", text="All set.")
        return Decision(kind="action", model_action=item)


def _fast(monkeypatch):
    """Spin the loop instantly: no retry backoff, generous per-turn/per-action budgets."""
    monkeypatch.setattr(fal, "_retry_max", lambda: 0)
    monkeypatch.setattr(fal, "_retry_backoff_secs", lambda: 0.0)
    monkeypatch.setattr(fal, "_per_action_secs", lambda: 5.0)
    monkeypatch.setattr(fal, "_per_turn_secs", lambda: 5.0)
    monkeypatch.setattr(fal, "_session_base_secs", lambda: 300.0)
    monkeypatch.setattr(fal, "_session_max_secs", lambda: 600.0)
    monkeypatch.setattr(fal, "_max_steps", lambda: 40)


def _run(coro):
    return asyncio.run(coro)


# ── happy path: multi-step task marshals the right action frames ─────────────────────
def test_multi_step_open_type_tap_done(monkeypatch):
    _fast(monkeypatch)
    posted = []

    async def fake_pull(base_url, task_id, operator, timeout):
        return OBS

    async def fake_post(base_url, frame, timeout):
        posted.append(frame)
        return {"msg": "action_result", "success": True}

    monkeypatch.setattr(fal, "_pull_observation", fake_pull)
    monkeypatch.setattr(fal, "_post_action", fake_post)
    driver = FakeDriver([
        {"op": "open_app", "app": "com.foo.bar"},
        {"op": "type", "x": 500, "y": 300, "text": "hello"},
        {"op": "tap", "x": 500, "y": 700},
        "done",
    ])
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: driver)

    res = _run(fal.run_frontier_loop("http://phone:8765", "log in and search", "Brandon"))
    assert res.success is True
    assert res.final_text == "All set."
    # exactly the three actions were dispatched, correctly grounded + enveloped
    assert len(posted) == 3
    for f in posted:
        assert f["msg"] == "action" and f["operator"] == "Brandon" and f["task_id"]
    assert posted[0]["type"] == "open_app" and posted[0]["package"] == "com.foo.bar"
    assert posted[1] == {**posted[1], "type": "element_set_text",
                         "resource_id": "app:id/field", "text": "hello"}
    assert posted[2]["type"] == "element_click" and posted[2]["resource_id"] == "app:id/submit"
    # the driver saw the previous action_result before deciding the next step
    assert driver.seen_results[0] is None
    assert driver.seen_results[1] == {"msg": "action_result", "success": True}


def test_global_and_scroll_actions_marshal(monkeypatch):
    _fast(monkeypatch)
    posted = []
    monkeypatch.setattr(fal, "_pull_observation", _aret(OBS))

    async def fake_post(base_url, frame, timeout):
        posted.append(frame)
        return {"msg": "action_result", "success": True}
    monkeypatch.setattr(fal, "_post_action", fake_post)
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: FakeDriver([
        {"op": "back"}, {"op": "home"}, {"op": "scroll", "direction": "up"}, "done",
    ]))
    res = _run(fal.run_frontier_loop("http://phone:8765", "navigate", "Brandon"))
    assert res.success
    assert posted[0] == {**posted[0], "type": "global_action", "action": "back"}
    assert posted[1]["type"] == "global_action" and posted[1]["action"] == "home"
    assert posted[2] == {**posted[2], "type": "scroll", "direction": "up"}


def test_type_with_no_prior_click_not_dispatched(monkeypatch):
    # M7-M2: a type action with no prior click (abs-px `type` carries no coordinate → reuses a
    # (0,0) last-click that snaps to the root container) must NOT be typed into the root — it is
    # ungroundable, fed back to the model, and never hits the wire. A subsequent real tap does.
    _fast(monkeypatch)
    posted = []
    monkeypatch.setattr(fal, "_pull_observation", _aret(OBS))

    async def fake_post(base_url, frame, timeout):
        posted.append(frame)
        return {"msg": "action_result", "success": True}
    monkeypatch.setattr(fal, "_post_action", fake_post)
    driver = FakeDriver([
        {"op": "type", "x": 0, "y": 0, "text": "hi"},   # no prior click → root, non-editable
        {"op": "tap", "x": 500, "y": 700},              # then a real tap on the submit button
        "done",
    ])
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: driver)

    res = _run(fal.run_frontier_loop("http://phone:8765", "type without clicking first", "Brandon"))
    assert res.success is True
    # the type never hit the wire (ungroundable); only the element_click did
    kinds = [f["type"] for f in posted]
    assert kinds == ["element_click"]
    assert "element_set_text" not in kinds
    # the driver was fed back the ungroundable failure before deciding its next step
    assert driver.seen_results[1]["success"] is False
    assert "type" in driver.seen_results[1]["detail"]


# ── embedded follow-on observation is consumed (no double-observe) ───────────────────
def test_embedded_observation_used_instead_of_pull(monkeypatch):
    _fast(monkeypatch)
    pulls = {"n": 0}

    async def fake_pull(base_url, task_id, operator, timeout):
        pulls["n"] += 1
        return OBS

    async def fake_post(base_url, frame, timeout):
        # action_result carries a fresh observation → the loop must NOT pull again
        return {"msg": "action_result", "success": True, "observation": OBS}

    monkeypatch.setattr(fal, "_pull_observation", fake_pull)
    monkeypatch.setattr(fal, "_post_action", fake_post)
    monkeypatch.setattr(fal, "_make_driver",
                        lambda *a, **k: FakeDriver([{"op": "tap", "x": 500, "y": 700}, "done"]))
    res = _run(fal.run_frontier_loop("http://phone:8765", "tap", "Brandon"))
    assert res.success
    # exactly ONE pull: the initial observation. The post-action state came embedded.
    assert pulls["n"] == 1


# ── structured errors ────────────────────────────────────────────────────────────────
def test_no_device_when_initial_observation_absent(monkeypatch):
    _fast(monkeypatch)
    monkeypatch.setattr(fal, "_pull_observation", _aret(None))  # stream has no observation
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: FakeDriver(["done"]))
    res = _run(fal.run_frontier_loop("http://phone:8765", "x", "Brandon"))
    assert res.success is False
    assert res.error_kind == "no_device"


def test_lost_contact_when_post_action_keeps_failing(monkeypatch):
    _fast(monkeypatch)
    monkeypatch.setattr(fal, "_pull_observation", _aret(OBS))

    async def boom(base_url, frame, timeout):
        raise ConnectionError("dropped")

    monkeypatch.setattr(fal, "_post_action", boom)
    monkeypatch.setattr(fal, "_make_driver",
                        lambda *a, **k: FakeDriver([{"op": "tap", "x": 500, "y": 700}, "done"]))
    res = _run(fal.run_frontier_loop("http://phone:8765", "x", "Brandon"))
    assert res.success is False
    assert res.error_kind == "lost_contact"


def test_post_action_retries_then_succeeds(monkeypatch):
    _fast(monkeypatch)
    monkeypatch.setattr(fal, "_retry_max", lambda: 2)  # allow retries
    monkeypatch.setattr(fal, "_pull_observation", _aret(OBS))
    calls = {"n": 0}

    async def flaky(base_url, frame, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionResetError("transient")
        return {"msg": "action_result", "success": True}

    monkeypatch.setattr(fal, "_post_action", flaky)
    monkeypatch.setattr(fal, "_make_driver",
                        lambda *a, **k: FakeDriver([{"op": "tap", "x": 500, "y": 700}, "done"]))
    res = _run(fal.run_frontier_loop("http://phone:8765", "x", "Brandon"))
    assert res.success is True
    assert calls["n"] == 2  # one transient failure, then a retry succeeded


def test_session_timeout(monkeypatch):
    _fast(monkeypatch)
    monkeypatch.setattr(fal, "_session_base_secs", lambda: 0.0)  # deadline == now → trips at step 1
    monkeypatch.setattr(fal, "_session_max_secs", lambda: 0.0)
    monkeypatch.setattr(fal, "_pull_observation", _aret(OBS))
    monkeypatch.setattr(fal, "_make_driver",
                        lambda *a, **k: FakeDriver([{"op": "tap", "x": 1, "y": 1}, "done"]))
    res = _run(fal.run_frontier_loop("http://phone:8765", "x", "Brandon"))
    assert res.success is False
    assert res.error_kind == "timeout"


def test_max_steps(monkeypatch):
    _fast(monkeypatch)
    monkeypatch.setattr(fal, "_max_steps", lambda: 2)
    monkeypatch.setattr(fal, "_pull_observation", _aret(OBS))
    monkeypatch.setattr(fal, "_post_action",
                        _aret({"msg": "action_result", "success": True}))
    # a driver that NEVER says done → the loop must stop at the step limit
    monkeypatch.setattr(fal, "_make_driver",
                        lambda *a, **k: FakeDriver([{"op": "tap", "x": 500, "y": 700}] * 10))
    res = _run(fal.run_frontier_loop("http://phone:8765", "x", "Brandon"))
    assert res.success is False
    assert res.error_kind == "max_steps"
    assert res.steps == 2


def test_ungroundable_action_is_fed_back_not_fatal(monkeypatch):
    _fast(monkeypatch)
    posted = []
    monkeypatch.setattr(fal, "_pull_observation", _aret(OBS))

    async def fake_post(base_url, frame, timeout):
        posted.append(frame)
        return {"msg": "action_result", "success": True}

    monkeypatch.setattr(fal, "_post_action", fake_post)
    # first action is unsupported (ungroundable) → skipped + fed back; then a real tap; done
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: FakeDriver([
        {"op": "unsupported", "name": "key_combination"},
        {"op": "tap", "x": 500, "y": 700},
        "done",
    ]))
    res = _run(fal.run_frontier_loop("http://phone:8765", "x", "Brandon"))
    assert res.success is True
    # the unsupported action never hit the wire; only the real tap did
    assert len(posted) == 1 and posted[0]["type"] == "element_click"


def test_requires_task(monkeypatch):
    res = _run(fal.run_frontier_loop("http://phone:8765", "   ", "Brandon"))
    assert res.success is False
    assert res.error_kind == "invalid_argument"


def test_config_error_for_unknown_provider(monkeypatch):
    _fast(monkeypatch)
    monkeypatch.setattr(fal, "_pull_observation", _aret(OBS))
    monkeypatch.setattr(fal, "_frontier_provider", lambda: "acme")  # no driver for this
    res = _run(fal.run_frontier_loop("http://phone:8765", "x", "Brandon"))
    assert res.success is False
    assert res.error_kind == "config_error"


# ── normalize mapping (provider function name → neutral op) ──────────────────────────
def test_normalize_gemini_calls():
    n = fal._normalize_gemini_call
    assert n("click_at", {"x": 1, "y": 2}) == {"op": "tap", "x": 1, "y": 2}
    assert n("type_text_at", {"x": 1, "y": 2, "text": "hi"})["op"] == "type"
    assert n("open_app", {"app_name": "com.x"}) == {"op": "open_app", "app": "com.x"}
    assert n("go_home", {}) == {"op": "home"}
    assert n("go_back_android", {}) == {"op": "back"}
    assert n("scroll_down", {}) == {"op": "scroll", "direction": "down"}
    assert n("wait_5_seconds", {}) == {"op": "wait", "seconds": 5}
    assert n("mystery_fn", {}) == {"op": "unsupported", "name": "mystery_fn"}


def _aret(value):
    """An async stub ignoring its args, returning ``value`` (mirrors control_phone tests)."""
    async def f(*a, **k):
        return value
    return f


# ── F1: normalize table (all Gemini fn names) + key_combination + press_enter ────────
def test_normalize_gemini_call_full_table():
    n = fal._normalize_gemini_call
    assert n("click_at", {"x": 3, "y": 4}) == {"op": "tap", "x": 3, "y": 4}
    # type_text_at carries press_enter (F1) — default False, honored when true.
    assert n("type_text_at", {"x": 1, "y": 2, "text": "hi", "clear_before_typing": True,
                              "press_enter": True}) == {
        "op": "type", "x": 1, "y": 2, "text": "hi", "clear": True, "press_enter": True}
    assert n("type_text_at", {"text": "x"})["press_enter"] is False
    assert n("long_press_at", {"x": 5, "y": 6}) == {"op": "long_press", "x": 5, "y": 6}
    assert n("drag_and_drop", {"x": 1, "y": 2, "destination_x": 3, "destination_y": 4}) == {
        "op": "drag", "x": 1, "y": 2, "x2": 3, "y2": 4}
    assert n("scroll_at", {"direction": "left"}) == {"op": "scroll", "direction": "left"}
    assert n("scroll_down", {}) == {"op": "scroll", "direction": "down"}
    assert n("scroll_up", {}) == {"op": "scroll", "direction": "up"}
    assert n("open_app", {"app_name": "com.x"}) == {"op": "open_app", "app": "com.x"}
    assert n("open_app", {"package": "com.y"}) == {"op": "open_app", "app": "com.y"}
    assert n("go_home", {}) == {"op": "home"}
    assert n("go_back_android", {}) == {"op": "back"}
    assert n("go_back", {}) == {"op": "back"}
    assert n("wait_5_seconds", {}) == {"op": "wait", "seconds": 5}
    assert n("hover_at", {}) == {"op": "wait", "seconds": 0}
    assert n("frobnicate", {}) == {"op": "unsupported", "name": "frobnicate"}


def test_normalize_key_combination():
    n = fal._normalize_gemini_call
    # single mappable keys → press_key (enter/return→enter is the critical submit case)
    assert n("key_combination", {"keys": "Enter"}) == {"op": "press_key", "key": "enter"}
    assert n("key_combination", {"keys": "Return"}) == {"op": "press_key", "key": "enter"}
    assert n("key_combination", {"keys": " enter "}) == {"op": "press_key", "key": "enter"}
    assert n("key_combination", {"keys": "Tab"}) == {"op": "press_key", "key": "tab"}
    assert n("key_combination", {"keys": "Backspace"}) == {"op": "press_key", "key": "delete"}
    assert n("key_combination", {"keys": "Escape"}) == {"op": "press_key", "key": "back"}
    # a modifier combo / unknown key has no Android press_key equivalent → unsupported (re-plan)
    assert n("key_combination", {"keys": "Control+A"})["op"] == "unsupported"
    assert n("key_combination", {"keys": ""})["op"] == "unsupported"


# ── F1: press_enter emits the type frame THEN a follow-on press_key(enter) ────────────
def test_press_enter_emits_type_then_press_key(monkeypatch):
    _fast(monkeypatch)
    posted = []
    monkeypatch.setattr(fal, "_pull_observation", _aret(OBS))

    async def fake_post(base_url, frame, timeout):
        posted.append(frame)
        return {"msg": "action_result", "success": True}

    monkeypatch.setattr(fal, "_post_action", fake_post)
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: FakeDriver([
        {"op": "type", "x": 500, "y": 300, "text": "coffee", "press_enter": True},
        "done",
    ]))
    res = _run(fal.run_frontier_loop("http://phone:8765", "search", "Brandon"))
    assert res.success
    # the text was SET, then a follow-on press_key(enter) submitted it — a "type → submit" flow
    assert len(posted) == 2
    assert posted[0]["type"] == "element_set_text" and posted[0]["text"] == "coffee"
    assert posted[1] == {**posted[1], "type": "press_key", "key": "enter"}
    # both frames carry the SAME transport envelope (task_id/operator)
    assert posted[1]["msg"] == "action" and posted[1]["task_id"] == posted[0]["task_id"]
    assert posted[1]["operator"] == "Brandon"


def test_press_key_op_grounds_to_frame_and_unknown_key_is_ungroundable(monkeypatch):
    _fast(monkeypatch)
    posted = []
    monkeypatch.setattr(fal, "_pull_observation", _aret(OBS))

    async def fake_post(base_url, frame, timeout):
        posted.append(frame)
        return {"msg": "action_result", "success": True}

    monkeypatch.setattr(fal, "_post_action", fake_post)
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: FakeDriver([
        {"op": "press_key", "key": "enter"},
        {"op": "press_key", "key": "zzz"},   # not in PRESS_KEYS → ungroundable, fed back
        "done",
    ]))
    res = _run(fal.run_frontier_loop("http://phone:8765", "x", "Brandon"))
    assert res.success
    assert len(posted) == 1  # only the valid enter reached the wire
    assert posted[0]["type"] == "press_key" and posted[0]["key"] == "enter"


# ── F2: terminal device states short-circuit (no wasted model calls) ─────────────────
def test_terminal_error_classifies():
    te = fal._terminal_error
    assert te(None) is None
    assert te({"success": True}) is None
    # an ordinary recoverable failure is NOT terminal (fed back to the model)
    assert te({"success": False, "error": "node_not_found", "detail": "node 5 not found"}) is None
    assert te({"success": False, "error": "not_wired"})[0] == "no_device"
    assert te({"success": False, "detail": "remote control stopped by user"})[0] == "stopped"
    assert te({"success": False, "error": "not_enabled"})[0] == "accessibility_off"
    assert te({"success": False, "detail": "accessibility service not enabled"})[0] == "accessibility_off"


def test_terminal_not_wired_short_circuits_to_no_device(monkeypatch):
    _fast(monkeypatch)
    posted = []
    monkeypatch.setattr(fal, "_pull_observation", _aret(OBS))

    async def fake_post(base_url, frame, timeout):
        posted.append(frame)
        return {"msg": "action_result", "success": False, "error": "not_wired",
                "detail": "no action dispatcher registered on this device"}

    monkeypatch.setattr(fal, "_post_action", fake_post)
    monkeypatch.setattr(fal, "_make_driver",
                        lambda *a, **k: FakeDriver([{"op": "tap", "x": 500, "y": 700}] * 5))
    res = _run(fal.run_frontier_loop("http://phone:8765", "x", "Brandon"))
    assert res.success is False
    assert res.error_kind == "no_device"
    assert len(posted) == 1   # short-circuited after the first failing action — no re-plan


def test_terminal_stopped_short_circuits(monkeypatch):
    _fast(monkeypatch)
    monkeypatch.setattr(fal, "_pull_observation", _aret(OBS))
    monkeypatch.setattr(fal, "_post_action", _aret(
        {"msg": "action_result", "success": False, "detail": "remote control stopped by user"}))
    monkeypatch.setattr(fal, "_make_driver",
                        lambda *a, **k: FakeDriver([{"op": "tap", "x": 1, "y": 1}] * 5))
    res = _run(fal.run_frontier_loop("http://phone:8765", "x", "Brandon"))
    assert res.success is False
    assert res.error_kind == "stopped"


def test_terminal_accessibility_off_short_circuits(monkeypatch):
    _fast(monkeypatch)
    monkeypatch.setattr(fal, "_pull_observation", _aret(OBS))
    monkeypatch.setattr(fal, "_post_action", _aret(
        {"msg": "action_result", "success": False, "error": "not_enabled",
         "detail": "accessibility service not enabled"}))
    monkeypatch.setattr(fal, "_make_driver",
                        lambda *a, **k: FakeDriver([{"op": "tap", "x": 1, "y": 1}] * 5))
    res = _run(fal.run_frontier_loop("http://phone:8765", "x", "Brandon"))
    assert res.success is False
    assert res.error_kind == "accessibility_off"


# ── M3: an a11y-off FIRST observation short-circuits to intent_only (no model call) ──
A11Y_OFF_OBS = {
    "msg": "observation",
    "ui_tree": [],  # a11y off → the device reports an empty tree
    "device_capability": {"formFactor": "phone", "hasScreenshot": False,
                          "supportsCoordinateGesture": False, "accessibilityEnabled": False,
                          "displayId": 0},
    "timestamp": 1,
}


def test_observation_a11y_off_short_circuits_to_intent_only(monkeypatch):
    # (M3) The device's FIRST observation reports accessibility OFF → the loop stops CLEANLY at the
    # intent_only terminal BEFORE building a driver / spending a model call. Proof of "no model
    # call": _make_driver is stubbed to explode; it must never be reached. Nothing is posted.
    _fast(monkeypatch)
    posted = []

    async def fake_post(base_url, frame, timeout):
        posted.append(frame)
        return {"msg": "action_result", "success": True}

    def boom_driver(*a, **k):
        raise AssertionError("a driver must NOT be built when a11y is off at the first observation")

    monkeypatch.setattr(fal, "_pull_observation", _aret(A11Y_OFF_OBS))
    monkeypatch.setattr(fal, "_post_action", fake_post)
    monkeypatch.setattr(fal, "_make_driver", boom_driver)

    res = _run(fal.run_frontier_loop("http://phone:8765", "do a thing", "Brandon"))
    assert res.success is False
    assert res.error_kind == "intent_only"
    assert res.steps == 0                       # short-circuited before any step
    assert posted == []                         # nothing dispatched
    assert "accessibility" in res.message.lower()


def test_observation_a11y_on_does_not_short_circuit(monkeypatch):
    # Back-compat: an observation WITHOUT the accessibilityEnabled key (OBS) is treated as a11y-ON
    # (default True) and proceeds normally — the M3 short-circuit only fires on an explicit false.
    _fast(monkeypatch)
    monkeypatch.setattr(fal, "_pull_observation", _aret(OBS))

    async def fake_post(base_url, frame, timeout):
        return {"msg": "action_result", "success": True}

    monkeypatch.setattr(fal, "_post_action", fake_post)
    monkeypatch.setattr(fal, "_make_driver",
                        lambda *a, **k: FakeDriver([{"op": "tap", "x": 500, "y": 700}, "done"]))
    res = _run(fal.run_frontier_loop("http://phone:8765", "x", "Brandon"))
    assert res.success is True                   # not short-circuited
    assert res.error_kind is None


# ── MINOR 9: fake-HTTP round-trip over the real _pull_observation / _post_action seams ─
class _FakeStreamCtx:
    """Async-context-manager stand-in for httpx's streaming response."""

    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakePostResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeHttpxClient:
    """Drop-in for httpx.AsyncClient: canned SSE lines for stream(), canned JSON for post()."""
    stream_lines: list = []
    post_payload: dict = {}
    posted: list = []
    last_stream = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, params=None):
        _FakeHttpxClient.last_stream = (method, url, params)
        return _FakeStreamCtx(_FakeHttpxClient.stream_lines)

    async def post(self, url, json=None):
        _FakeHttpxClient.posted.append((url, json))
        return _FakePostResp(_FakeHttpxClient.post_payload)


def test_pull_observation_parses_first_sse_data_frame(monkeypatch):
    _FakeHttpxClient.stream_lines = [
        ": keep-alive comment", "",
        "data: " + json.dumps(OBS),
        "data: " + json.dumps({"msg": "observation", "second": True}),
    ]
    monkeypatch.setattr("httpx.AsyncClient", _FakeHttpxClient)
    obs = _run(fal._pull_observation("http://phone:8765", "t1", "Brandon", 5.0))
    assert obs["msg"] == "observation"
    assert "second" not in obs                       # only the FIRST data frame is consumed
    assert _FakeHttpxClient.last_stream[1] == "http://phone:8765/stream/t1"
    assert _FakeHttpxClient.last_stream[2] == {"operator": "Brandon"}   # operator-scoped query


def test_pull_observation_none_on_comment_only_stream(monkeypatch):
    _FakeHttpxClient.stream_lines = [": scaffold — no observation source wired", ""]
    monkeypatch.setattr("httpx.AsyncClient", _FakeHttpxClient)
    obs = _run(fal._pull_observation("http://phone:8765", "t1", "Brandon", 5.0))
    assert obs is None                               # SSE comments only → no observation


def test_post_action_round_trip(monkeypatch):
    _FakeHttpxClient.posted = []
    _FakeHttpxClient.post_payload = {"msg": "action_result", "success": True, "detail": "ok"}
    monkeypatch.setattr("httpx.AsyncClient", _FakeHttpxClient)
    frame = {"msg": "action", "type": "scroll", "direction": "up",
             "task_id": "t1", "operator": "Brandon"}
    res = _run(fal._post_action("http://phone:8765", frame, 5.0))
    assert res == {"msg": "action_result", "success": True, "detail": "ok"}
    assert _FakeHttpxClient.posted[0] == ("http://phone:8765/action", frame)  # full envelope POSTed


# ── M6.2: XR capture-independent, node+intent-only loop ───────────────────────────────
# An XR observation: coordinate-less (supportsCoordinateGesture=False), NO screenshot
# capability, and — critically — no "screenshot" key ever on the wire (the device never
# captures one). Same resolvable tree so element grounding still lands on real nodes.
XR_OBS = {
    "msg": "observation",
    "ui_tree": [
        _node(0, "0,0,1080,2400", clickable=True, resource_id="root"),
        _node(1, "400,600,700,850", editable=True, resource_id="app:id/field"),
        _node(2, "400,1600,700,1750", clickable=True, resource_id="app:id/submit"),
    ],
    "device_capability": {"formFactor": "xr_headset", "hasScreenshot": False,
                          "supportsCoordinateGesture": False, "displayId": 0},
    "timestamp": 1,
}


class _ObsRecordingDriver:
    """A canned driver that also records EVERY observation it was handed — so a test can prove
    the loop never synthesizes/forwards a screenshot on an XR device (capture-independence)."""

    def __init__(self, script):
        self._script = list(script)
        self.seen_observations = []

    async def next_action(self, observation, last_result):
        self.seen_observations.append(observation)
        if not self._script:
            return Decision(kind="done", text="done")
        item = self._script.pop(0)
        if item == "done":
            return Decision(kind="done", text="XR task complete.")
        return Decision(kind="action", model_action=item)


def test_xr_loop_drives_element_intent_only_with_zero_screenshots(monkeypatch):
    """M6.2: on an XR device the loop marshals element/intent/global frames ONLY — the two
    coordinate ops are ungroundable on a coordinate-less device (fed back to the model, never
    posted) — and no observation the loop hands the (fake) driver carries a ``screenshot`` field.

    This proves the WIRE/routing side of capture-independence, NOT that the loop refrains from
    REQUESTING/CAPTURING a screenshot: the ``FakeDriver`` here bypasses the real screenshot path
    (``_screenshot_bytes`` / ``_build_mobile_tools`` / ``_mobile_system_prompt`` are never
    exercised). Those load-bearing guarantees are covered by the unit tests below —
    ``test_mobile_system_prompt_forbids_screenshot_on_capture_less_device`` (prompt forbids it) and
    ``test_build_mobile_tools_prunes_coordinate_functions_on_xr`` (tools pruned) — plus the Kotlin
    ``ObservationTest`` (the device omits ``screenshot`` from the wire when hasScreenshot=false)."""
    _fast(monkeypatch)
    posted = []

    async def fake_pull(base_url, task_id, operator, timeout):
        return XR_OBS

    async def fake_post(base_url, frame, timeout):
        posted.append(frame)
        return {"msg": "action_result", "success": True}   # no embedded screenshot either

    monkeypatch.setattr(fal, "_pull_observation", fake_pull)
    monkeypatch.setattr(fal, "_post_action", fake_post)
    driver = _ObsRecordingDriver([
        {"op": "open_app", "app": "com.foo.bar"},              # intent-class → open_app frame
        {"op": "type", "x": 500, "y": 300, "text": "hello"},   # → element_set_text (node)
        {"op": "tap", "x": 500, "y": 700},                     # → element_click (node)
        {"op": "long_press", "x": 500, "y": 700},              # COORDINATE → ungroundable on XR
        {"op": "drag", "x": 100, "y": 100, "x2": 900, "y2": 900},  # COORDINATE → ungroundable
        "done",
    ])
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: driver)

    res = _run(fal.run_frontier_loop("http://xr:8765", "search on the headset", "Brandon"))
    assert res.success is True
    # ONLY element/intent/global frames hit the wire — the two coordinate ops were ungroundable
    # on a coordinate-less device (fed back to the model, never posted).
    kinds = [f["type"] for f in posted]
    assert kinds == ["open_app", "element_set_text", "element_click"]
    assert not any(str(k).startswith("coordinate_") for k in kinds)
    # element frames addressed real a11y nodes (resource_id), never a raw coordinate
    assert posted[1] == {**posted[1], "type": "element_set_text",
                         "resource_id": "app:id/field", "text": "hello"}
    assert posted[2]["type"] == "element_click" and posted[2]["resource_id"] == "app:id/submit"
    # capture-independence: NOT ONE observation handed to the model carried a screenshot.
    assert driver.seen_observations, "driver must have been consulted"
    assert all((obs or {}).get("screenshot") is None for obs in driver.seen_observations)


def test_mobile_system_prompt_forbids_screenshot_on_capture_less_device():
    """The system prompt itself enforces capture-independence: on a screenshot-less device the
    model is told to reason from the a11y tree only and NOT to request a screenshot."""
    xr = {"formFactor": "xr_headset", "hasScreenshot": False, "supportsCoordinateGesture": False}
    prompt = fal._mobile_system_prompt(xr)
    assert "NO screenshot" in prompt
    assert "Do not request a screenshot" in prompt
    assert "xr_headset" in prompt
    # a phone, by contrast, is told it CAN see a screenshot.
    phone = {"formFactor": "phone", "hasScreenshot": True, "supportsCoordinateGesture": True}
    assert "screenshot AND a list" in fal._mobile_system_prompt(phone)


def test_build_mobile_tools_prunes_coordinate_functions_on_xr():
    """The model is never even OFFERED a coordinate-only gesture on XR: drag_and_drop is
    excluded and long_press_at is dropped, so it can't call a function the grounder must reject."""
    import types as _pytypes

    class _Capture:
        def __init__(self, **kw):
            self.kw = kw

    fake_types = _pytypes.SimpleNamespace(
        Tool=_Capture, ComputerUse=_Capture, FunctionDeclaration=_Capture,
        Environment=_pytypes.SimpleNamespace(ENVIRONMENT_BROWSER="browser"),
    )

    xr_tools = fal._build_mobile_tools(fake_types, {"supportsCoordinateGesture": False})
    cu = next(t for t in xr_tools if "computer_use" in t.kw)
    assert "drag_and_drop" in cu.kw["computer_use"].kw["excluded_predefined_functions"]
    fns = next(t for t in xr_tools if "function_declarations" in t.kw)
    xr_names = [f.kw["name"] for f in fns.kw["function_declarations"]]
    assert "long_press_at" not in xr_names       # coordinate-only → pruned on XR
    assert "open_app" in xr_names and "go_back_android" in xr_names   # element/intent stay

    phone_tools = fal._build_mobile_tools(fake_types, {"supportsCoordinateGesture": True})
    phone_cu = next(t for t in phone_tools if "computer_use" in t.kw)
    assert "drag_and_drop" not in phone_cu.kw["computer_use"].kw["excluded_predefined_functions"]
    phone_fns = next(t for t in phone_tools if "function_declarations" in t.kw)
    assert "long_press_at" in [f.kw["name"] for f in phone_fns.kw["function_declarations"]]
