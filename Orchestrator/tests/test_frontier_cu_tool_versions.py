"""Frontier AnthropicDriver — per-model computer-use tool version (fully mocked; no live key/device).

The driver used to send computer_20251124 + its beta to every Claude model. That 400s on the
Claude 5.5 tier (computer_toolset_20260801 only) and on Sonnet/Haiku 4.5 (computer_20250124
only). Covers:
  * the tools entry / anthropic-beta / tool_choice each model gets (toolset → no beta, one
    action per turn; 4.5 → computer_20250124 + its beta; everyone else unchanged).
  * toolset member blocks: action = block name, params = block input; `key` repeat refused.
  * the history: tool_use echoes toolset_name, thinking blocks are passed back unchanged, every
    tool_result for a member block carries toolset_name, and no tool_use is left unanswered.
  * a max_tokens / refusal stop with no action is a model error, not "Task complete.".
  * run_frontier_loop end-to-end on a toolset model.
"""
import asyncio
import base64
import struct
import types

import pytest

from Orchestrator import frontier_agent_loop as fal


def _run(coro):
    return asyncio.run(coro)


def _ns(**kw):
    return types.SimpleNamespace(**kw)


def _node(node_id, bounds, *, clickable=False, editable=False, resource_id=""):
    return {"node_id": node_id, "role": "View", "text": "", "resource_id": resource_id,
            "bounds": bounds, "clickable": clickable, "editable": editable, "is_password": False}


def _fake_png(width, height):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return sig + ihdr


OBS = {
    "msg": "observation",
    "ui_tree": [
        _node(0, "0,0,1080,2400", clickable=True, resource_id="root"),
        _node(1, "400,600,700,850", clickable=True, editable=True, resource_id="app:id/field"),
        _node(2, "400,1600,700,1750", clickable=True, resource_id="app:id/submit"),
    ],
    "device_capability": {"formFactor": "phone", "hasScreenshot": True,
                          "supportsCoordinateGesture": True, "displayId": 0},
    "screenshot": base64.b64encode(_fake_png(1080, 2400)).decode(),
    "timestamp": 1,
}
CAP = OBS["device_capability"]

TOOLSET = "computer_toolset_20260801"


class _FakeAnthropicClient:
    """Stand-in for AsyncAnthropic: canned `beta.messages.create` responses (one per turn).
    A script entry is a content list, or (content, stop_reason)."""

    def __init__(self, script):
        script = list(script)

        class _Messages:
            def __init__(self):
                self.calls = []

            async def create(self, **kwargs):
                # snapshot the history: the driver keeps appending to the same list
                self.calls.append(dict(kwargs, messages=list(kwargs["messages"])))
                entry = script.pop(0)
                content, stop = entry if isinstance(entry, tuple) else (
                    entry, "tool_use" if entry else "end_turn")
                return _ns(content=content, stop_reason=stop)

        self.beta = _ns(messages=_Messages())


def _member(tid, name, **inp):
    """A toolset member tool_use block, as the SDK parses it (toolset_name kept as an extra)."""
    return _ns(type="tool_use", id=tid, name=name, toolset_name="computer", input=inp)


def _driver(model, script):
    client = _FakeAnthropicClient(script)
    return fal.AnthropicDriver(model, "do it", "Brandon", CAP, client=client), client.beta.messages


# ── request shape per model ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"])
def test_toolset_models_get_the_toolset_no_beta_and_one_action_per_turn(model):
    d, msgs = _driver(model, [[_member("t1", "screenshot")]])
    _run(d.next_action(OBS, None))
    call = msgs.calls[0]
    computer = call["tools"][0]
    assert computer["type"] == TOOLSET
    for banned in ("name", "display_width_px", "display_height_px", "display_number"):
        assert banned not in computer
    assert "betas" not in call                      # no computer-use beta on the toolset
    assert call["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    # the Android-nav custom tools still ride alongside, and nothing else is named "computer"
    names = [t.get("name") for t in call["tools"][1:]]
    assert names == ["open_app", "go_home", "go_back", "go_to_recents"]


@pytest.mark.parametrize("model", ["claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-8",
                                   "claude-sonnet-4-6", "claude-opus-5", "claude-sonnet-5",
                                   "claude-fable-5", "claude-opus-4-5-20251101"])
def test_computer_20251124_models_are_unchanged(model):
    d, msgs = _driver(model, [[_ns(type="tool_use", id="t1", name="computer",
                                   input={"action": "screenshot"})]])
    _run(d.next_action(OBS, None))
    call = msgs.calls[0]
    dw, dh = d.adapter.model_view_dims(1080, 2400)
    assert call["tools"][0] == {"type": "computer_20251124", "name": "computer",
                                "display_width_px": dw, "display_height_px": dh}
    assert call["betas"] == ["computer-use-2025-11-24"]
    assert "tool_choice" not in call


def test_computer_20251124_beta_keeps_the_config_override(monkeypatch):
    monkeypatch.setattr(fal, "_anthropic_cu_beta", lambda: "computer-use-override")
    d, msgs = _driver("claude-opus-4-7", [[]])
    _run(d.next_action(OBS, None))
    assert msgs.calls[0]["betas"] == ["computer-use-override"]


@pytest.mark.parametrize("model", ["claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"])
def test_4_5_models_get_computer_20250124_and_its_beta(model):
    d, msgs = _driver(model, [[]])
    _run(d.next_action(OBS, None))
    call = msgs.calls[0]
    assert call["tools"][0]["type"] == "computer_20250124"
    assert call["tools"][0]["name"] == "computer"
    assert call["betas"] == ["computer-use-2025-01-24"]
    assert "tool_choice" not in call


# ── toolset member blocks → provider-neutral ops ─────────────────────────────────────
def test_toolset_member_blocks_map_like_the_legacy_action_field():
    d, _ = _driver("claude-opus-5-5", [])
    assert d._to_op(_member("a", "left_click", coordinate=[359, 474])) == {"op": "tap", "x": 359, "y": 474}
    # type carries no coordinate → reuses the last click
    assert d._to_op(_member("b", "type", text="hello")) == {"op": "type", "x": 359, "y": 474, "text": "hello"}
    assert d._to_op(_member("c", "key", text="Return")) == {"op": "press_key", "key": "enter"}
    assert d._to_op(_member("c2", "key", text="Return", repeat=1)) == {"op": "press_key", "key": "enter"}
    assert d._to_op(_member("d", "scroll", scroll_direction="up", scroll_amount=3)) == {
        "op": "scroll", "direction": "up"}
    assert d._to_op(_member("e", "left_click_drag", start_coordinate=[1, 2], coordinate=[3, 4])) == {
        "op": "drag", "x": 1, "y": 2, "x2": 3, "y2": 4}
    assert d._to_op(_member("f", "screenshot")) == {"op": "wait", "seconds": 0}
    assert d._to_op(_member("g", "wait", duration=2)) == {"op": "wait", "seconds": 2.0}
    assert d._to_op(_member("h", "hold_key", text="a", duration=1))["op"] == "unsupported"


def test_toolset_key_repeat_is_refused_not_pressed_once():
    d, _ = _driver("claude-opus-5-5", [])
    op = d._to_op(_member("k", "key", text="BackSpace", repeat=4))
    assert op == {"op": "unsupported", "name": "key:BackSpacex4"}


def test_custom_nav_tool_on_a_toolset_model_is_still_a_nav_op():
    d, _ = _driver("claude-opus-5-5", [])
    assert d._to_op(_ns(type="tool_use", id="n", name="open_app", input={"package": "com.x"})) == {
        "op": "open_app", "app": "com.x"}


# ── history: echo, thinking, tool_result toolset_name ───────────────────────────────
def test_toolset_round_trip_history_shape():
    thinking = _ns(type="thinking", thinking="", signature="sig-abc")
    d, msgs = _driver("claude-opus-5-5", [
        [thinking, _member("t1", "left_click", coordinate=[359, 474])],
        [_ns(type="text", text="Done.")],
    ])
    dec = _run(d.next_action(OBS, None))
    assert dec.kind == "action" and dec.model_action == {"op": "tap", "x": 359, "y": 474}

    dec2 = _run(d.next_action(OBS, {"success": True, "detail": "clicked"}))
    assert dec2.kind == "done" and dec2.text == "Done."

    history = msgs.calls[1]["messages"]
    assistant = history[1]
    assert assistant["role"] == "assistant"
    # thinking passed back unchanged, ahead of the tool_use; tool_use keeps toolset_name
    assert assistant["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig-abc"}
    assert assistant["content"][1] == {"type": "tool_use", "id": "t1", "name": "left_click",
                                       "input": {"coordinate": [359, 474]}, "toolset_name": "computer"}
    result = history[2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1"
    assert result["toolset_name"] == "computer"
    assert "is_error" not in result
    kinds = [c["type"] for c in result["content"]]
    assert kinds == ["text", "text", "image"]               # outcome + tree + fresh screenshot
    assert len(history[2]["content"]) == 1                 # the screen rode inside the result


def test_redacted_thinking_is_passed_back():
    d, msgs = _driver("claude-opus-5-5", [
        [_ns(type="redacted_thinking", data="opaque"), _member("t1", "screenshot")],
        [],
    ])
    _run(d.next_action(OBS, None))
    _run(d.next_action(OBS, {"success": True}))
    assert msgs.calls[1]["messages"][1]["content"][0] == {"type": "redacted_thinking", "data": "opaque"}


def test_every_toolset_tool_use_is_answered_even_if_parallel_slips_through():
    d, msgs = _driver("claude-opus-5-5", [
        [_member("t1", "left_click", coordinate=[359, 474]), _member("t2", "type", text="hi"),
         _ns(type="tool_use", id="t3", name="go_home", input={})],
        [],
    ])
    dec = _run(d.next_action(OBS, None))
    assert dec.model_action == {"op": "tap", "x": 359, "y": 474}       # only the first runs
    _run(d.next_action(OBS, {"success": True}))
    results = [b for b in msgs.calls[1]["messages"][2]["content"] if b["type"] == "tool_result"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2", "t3"]
    assert results[0]["toolset_name"] == "computer" and "is_error" not in results[0]
    assert results[1] == {"type": "tool_result", "tool_use_id": "t2", "is_error": True,
                          "content": "Not executed: one action at a time — re-plan from the screen.",
                          "toolset_name": "computer"}
    # a custom tool's result never carries toolset_name
    assert "toolset_name" not in results[2]


def test_nav_tool_result_on_a_toolset_model_has_no_toolset_name():
    d, msgs = _driver("claude-opus-5-5", [
        [_ns(type="tool_use", id="n1", name="go_back", input={})],
        [],
    ])
    _run(d.next_action(OBS, None))
    _run(d.next_action(OBS, {"success": True}))
    user = msgs.calls[1]["messages"][2]["content"]
    assert user[0]["type"] == "tool_result" and "toolset_name" not in user[0]
    assert [b["type"] for b in user[1:]] == ["text", "image"]           # screen sent after it


def test_legacy_round_trip_history_is_unchanged():
    d, msgs = _driver("claude-opus-4-7", [
        [_ns(type="tool_use", id="t1", name="computer",
             input={"action": "left_click", "coordinate": [359, 474]}),
         _ns(type="tool_use", id="t2", name="computer", input={"action": "type", "text": "x"})],
        [],
    ])
    _run(d.next_action(OBS, None))
    _run(d.next_action(OBS, {"success": True}))
    history = msgs.calls[1]["messages"]
    assert history[1]["content"][0] == {"type": "tool_use", "id": "t1", "name": "computer",
                                        "input": {"action": "left_click", "coordinate": [359, 474]}}
    first, second = history[2]["content"]
    assert set(first) == {"type", "tool_use_id", "content"}
    assert second == {"type": "tool_result", "tool_use_id": "t2",
                      "content": [{"type": "text",
                                   "text": "skipped: one action at a time — re-plan from the screen"}]}


# ── a stop with no action is not success ─────────────────────────────────────────────
@pytest.mark.parametrize("stop", ["max_tokens", "refusal"])
def test_no_action_on_max_tokens_or_refusal_is_a_model_error(stop):
    d, _ = _driver("claude-opus-5-5", [([_ns(type="thinking", thinking="", signature="s")], stop)])
    with pytest.raises(RuntimeError, match=stop):
        _run(d.next_action(OBS, None))


def test_end_turn_with_no_action_is_still_done():
    d, _ = _driver("claude-opus-5-5", [([_ns(type="text", text="All set.")], "end_turn")])
    dec = _run(d.next_action(OBS, None))
    assert dec.kind == "done" and dec.text == "All set."


# ── end-to-end through the loop on a toolset model ───────────────────────────────────
def test_frontier_loop_toolset_model_end_to_end(monkeypatch):
    monkeypatch.setattr(fal, "_retry_max", lambda: 0)
    monkeypatch.setattr(fal, "_retry_backoff_secs", lambda: 0.0)
    monkeypatch.setattr(fal, "_per_action_secs", lambda: 5.0)
    monkeypatch.setattr(fal, "_per_turn_secs", lambda: 5.0)
    monkeypatch.setattr(fal, "_session_base_secs", lambda: 300.0)
    monkeypatch.setattr(fal, "_session_max_secs", lambda: 600.0)
    monkeypatch.setattr(fal, "_max_steps", lambda: 40)
    posted = []

    async def fake_pull(base_url, task_id, operator, timeout):
        return OBS

    async def fake_post(base_url, frame, timeout):
        posted.append(frame)
        return {"msg": "action_result", "success": True}

    monkeypatch.setattr(fal, "_pull_observation", fake_pull)
    monkeypatch.setattr(fal, "_post_action", fake_post)
    # opus-5-5 is hi-res (2576 cap) → a 1080x2400 phone is sent 1:1, coords are device px
    driver, msgs = _driver("claude-opus-5-5", [
        [_ns(type="tool_use", id="t1", name="open_app", input={"package": "com.foo.bar"})],
        [_ns(type="thinking", thinking="", signature="s2"), _member("t2", "left_click", coordinate=[550, 725])],
        [_member("t3", "type", text="hello")],
        [_member("t4", "left_click", coordinate=[550, 1675])],
        [_ns(type="text", text="All set.")],
    ])
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: driver)

    res = _run(fal.run_frontier_loop("http://phone:8765", "log in and submit", "Brandon",
                                     provider="anthropic", model="claude-opus-5-5"))
    assert res.success is True and res.final_text == "All set."
    assert [f["type"] for f in posted] == ["open_app", "element_click", "element_set_text", "element_click"]
    assert posted[1]["resource_id"] == "app:id/field"
    assert posted[2]["resource_id"] == "app:id/field" and posted[2]["text"] == "hello"
    assert posted[3]["resource_id"] == "app:id/submit"
    # every request: toolset, no beta; every tool_use answered in the next user turn
    for i, call in enumerate(msgs.calls):
        assert call["tools"][0]["type"] == TOOLSET and "betas" not in call
        if i:
            prev_ids = [b["id"] for b in call["messages"][-2]["content"] if b["type"] == "tool_use"]
            answered = [b for b in call["messages"][-1]["content"] if b["type"] == "tool_result"]
            assert [b["tool_use_id"] for b in answered] == prev_ids
            for b in answered:
                assert (b.get("toolset_name") == "computer") == (b["tool_use_id"] != "t1")
