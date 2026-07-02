"""M7 provider-agnostic device control — unit tests (fully mocked; no live key/device).

Covers:
  * coordinate adapters — Gemini 0-999 denorm; Anthropic/OpenAI absolute-px downscale+rescale
    round-trip within tolerance; the factory picks the right adapter by provider AND by model
    id; Anthropic hi-res (1568 vs 2576) selection.
  * the FrontierDriver action-normalization for Anthropic (computer + nav custom tools) and
    OpenAI (Responses computer + function nav), incl. the type→last-click snap.
  * run_frontier_loop drives a multi-step task for EACH provider (mocked SDK client), emitting
    correctly-grounded /action frames (abs-px rescaled → element snap).
  * control_device provider selection: explicit override > device default_provider (M3 registry)
    > config default, plus gemma opt-in routing to the on-device control_phone path, and an
    invalid provider.
"""
import asyncio
import base64
import importlib.util
import struct
import types
from pathlib import Path

import pytest

from Orchestrator import frontier_agent_loop as fal
from Orchestrator import frontier_grounding as fg
from Orchestrator.frontier_agent_loop import Decision
from Orchestrator.toolvault.context import ToolContext, ToolResult
from Orchestrator.local_provider.mesh import Node, DeviceResolutionError


def _run(coro):
    return asyncio.run(coro)


def _ns(**kw):
    return types.SimpleNamespace(**kw)


# ── a resolvable device screen (device 1080×2400) + a fake PNG screenshot of that size ─
def _node(node_id, bounds, *, clickable=False, editable=False, resource_id="", role="View"):
    return {"node_id": node_id, "role": role, "text": "", "resource_id": resource_id,
            "bounds": bounds, "clickable": clickable, "editable": editable, "is_password": False}


def _fake_png(width, height):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return sig + ihdr


OBS_ABS = {
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


def _fast(monkeypatch):
    monkeypatch.setattr(fal, "_retry_max", lambda: 0)
    monkeypatch.setattr(fal, "_retry_backoff_secs", lambda: 0.0)
    monkeypatch.setattr(fal, "_per_action_secs", lambda: 5.0)
    monkeypatch.setattr(fal, "_per_turn_secs", lambda: 5.0)
    monkeypatch.setattr(fal, "_session_base_secs", lambda: 300.0)
    monkeypatch.setattr(fal, "_session_max_secs", lambda: 600.0)
    monkeypatch.setattr(fal, "_max_steps", lambda: 40)


# ════════════════════════════════════════════════════════════════════════════════════
# 7.1 — coordinate adapters
# ════════════════════════════════════════════════════════════════════════════════════
def test_coordinate_adapter_gemini_denormalizes_0_999():
    a = fg.GeminiCoordinateAdapter()
    assert a.to_device_px(500, 500, 1080, 2400) == (540, 1201)
    assert a.to_device_px(0, 0, 1080, 2400) == (0, 0)
    assert a.to_device_px(999, 999, 1080, 2400) == (1080, 2400)
    # Gemini reasons in 0-999 regardless of image size → no downscale.
    assert a.model_view_dims(1080, 2400) == (1080, 2400)
    assert a.prepare_screenshot(b"rawpng", 1080, 2400) == b"rawpng"


def test_coordinate_adapter_anthropic_downscale_and_rescale_roundtrip():
    a = fg.AnthropicCoordinateAdapter("claude-opus-4-6")          # 1568 long-edge cap
    assert a.max_long_edge == 1568
    dw, dh = a.model_view_dims(1080, 2400)                        # long edge 2400 → 1568
    assert (dw, dh) == (706, 1568)
    # round-trip a batch of device px through the model's downscaled space and back.
    for (px, py) in [(540, 1200), (0, 0), (1080, 2400), (700, 850), (400, 1600)]:
        s = 1568 / 2400
        model_x, model_y = round(px * s), round(py * s)          # what the model would return
        back = a.to_device_px(model_x, model_y, 1080, 2400)
        assert abs(back[0] - px) <= 2 and abs(back[1] - py) <= 2  # within ~2px tolerance
    # clamps on-screen
    assert a.to_device_px(99999, -5, 1080, 2400) == (1080, 0)


def test_coordinate_adapter_anthropic_hires_2576_for_new_models():
    # High-resolution vision (2576px long edge) arrived with the >= 4.7 models; older CU
    # models cap conservatively at 1568 (a smaller cap always works — just more downscale).
    assert fg.AnthropicCoordinateAdapter("claude-opus-4-8").max_long_edge == 2576
    assert fg.AnthropicCoordinateAdapter("claude-opus-4-7").max_long_edge == 2576
    assert fg.AnthropicCoordinateAdapter("claude-fable-5").max_long_edge == 2576
    assert fg.AnthropicCoordinateAdapter("claude-opus-4-6").max_long_edge == 1568
    assert fg.AnthropicCoordinateAdapter("claude-sonnet-4-6").max_long_edge == 1568
    assert fg.AnthropicCoordinateAdapter(None).max_long_edge == 1568


def test_coordinate_adapter_openai_abs_px_roundtrip():
    a = fg.OpenAICoordinateAdapter("gpt-5.5")
    assert a.max_long_edge == 1280
    assert a.model_view_dims(1080, 2400) == (576, 1280)
    s = 1280 / 2400
    px, py = 550, 725
    back = a.to_device_px(round(px * s), round(py * s), 1080, 2400)
    assert abs(back[0] - px) <= 2 and abs(back[1] - py) <= 2


def test_adapter_factory_by_provider_and_by_model():
    # by provider NAME
    assert isinstance(fg.get_coordinate_adapter("gemini"), fg.GeminiCoordinateAdapter)
    assert isinstance(fg.get_coordinate_adapter("gemma"), fg.GeminiCoordinateAdapter)
    assert isinstance(fg.get_coordinate_adapter("claude"), fg.AnthropicCoordinateAdapter)
    assert isinstance(fg.get_coordinate_adapter("anthropic"), fg.AnthropicCoordinateAdapter)
    assert isinstance(fg.get_coordinate_adapter("openai"), fg.OpenAICoordinateAdapter)
    # by MODEL id (keyed off the config CU backend regex)
    assert isinstance(fg.get_coordinate_adapter("claude-opus-4-8"), fg.AnthropicCoordinateAdapter)
    assert isinstance(fg.get_coordinate_adapter("gpt-5.5"), fg.OpenAICoordinateAdapter)
    assert isinstance(fg.get_coordinate_adapter("gemini-2.5-computer-use-preview-10-2025"),
                      fg.GeminiCoordinateAdapter)
    # anything unrecognised → Gemini (resilient default)
    assert isinstance(fg.get_coordinate_adapter("acme-9000"), fg.GeminiCoordinateAdapter)
    # model threaded to the Anthropic adapter (right long-edge)
    assert fg.get_coordinate_adapter("claude", "claude-opus-4-8").max_long_edge == 2576


# ════════════════════════════════════════════════════════════════════════════════════
# I1 — ONE coordinate space: abs-px providers see a11y bounds scaled into the model-view
# space (same space as the downscaled screenshot + their own output coords); Gemini unchanged.
# ════════════════════════════════════════════════════════════════════════════════════
def test_to_model_px_is_inverse_of_to_device_px():
    # abs-px: to_model_px scales device px → model-view px (the inverse of to_device_px)
    a = fg.AnthropicCoordinateAdapter("claude-opus-4-6")     # 1568/2400 long-edge
    s = 1568 / 2400
    assert a.to_model_px(400, 600, 1080, 2400) == (round(400 * s), round(600 * s))
    assert a.to_model_px(1080, 2400, 1080, 2400) == a.model_view_dims(1080, 2400)
    # Gemini/base: identity (model-view == device px for bounds purposes)
    assert fg.GeminiCoordinateAdapter().to_model_px(400, 600, 1080, 2400) == (400, 600)


def test_tree_text_scales_bounds_for_abs_px_but_not_for_gemini():
    anth = fg.AnthropicCoordinateAdapter("claude-opus-4-6")
    s = 1568 / 2400
    txt = fal._tree_text(OBS_ABS, coord_note="px note", adapter=anth, device_wh=(1080, 2400))
    # the field node's full-res bounds 400,600,700,850 are rendered in the DOWNSCALED model-view
    exp = f"{round(400*s)},{round(600*s)},{round(700*s)},{round(850*s)}"
    assert f"bounds={exp}" in txt
    # and the full-res bounds string is NOT shown (nothing the model reads is in device px)
    assert "bounds=400,600,700,850" not in txt
    # the root node spans the full model-view dims
    mvw, mvh = anth.model_view_dims(1080, 2400)
    assert f"bounds=0,0,{mvw},{mvh}" in txt

    # Gemini (no adapter) → bounds stay full-res device px: UNCHANGED / byte-identical
    gtxt = fal._tree_text(OBS_ABS)
    assert "bounds=400,600,700,850" in gtxt
    assert "bounds=0,0,1080,2400" in gtxt

    # CRUCIAL: the model-facing scaling never mutates the observation — internal grounding still
    # sees the UNTOUCHED full-res bounds.
    assert OBS_ABS["ui_tree"][1]["bounds"] == "400,600,700,850"
    assert OBS_ABS["ui_tree"][0]["bounds"] == "0,0,1080,2400"


def test_abs_px_bounds_derived_coord_snaps_to_correct_node():
    # Round-trip: the model reads the SCALED bounds, clicks the CENTER of the field bound, and the
    # grounding (which rescales the model coord via the adapter) snaps back to the SAME node —
    # proving screenshot + bounds + output coordinate are one coherent space (the I1 fix).
    anth = fg.AnthropicCoordinateAdapter("claude-opus-4-6")
    s = 1568 / 2400
    ml, mt, mr, mb = round(400*s), round(600*s), round(700*s), round(850*s)
    cx, cy = (ml + mr) // 2, (mt + mb) // 2               # center of the model-view field bound
    grounded = fal._ground({"op": "tap", "x": cx, "y": cy}, OBS_ABS,
                           OBS_ABS["device_capability"], adapter=anth)
    assert grounded.frame == {"type": "element_click", "resource_id": "app:id/field"}
    # a full-res coord (the pre-fix bug: model clicks from a device-px bound) would rescale
    # off-target — this is exactly what scaling the bounds into model-view space prevents.
    wrong = fal._ground({"op": "tap", "x": 550, "y": 725}, OBS_ABS,
                        OBS_ABS["device_capability"], adapter=anth)
    assert wrong.frame != {"type": "element_click", "resource_id": "app:id/field"}


# ════════════════════════════════════════════════════════════════════════════════════
# 7.3 — provider action-normalization (pure mappers)
# ════════════════════════════════════════════════════════════════════════════════════
def test_anthropic_action_to_op_table():
    n = fal._anthropic_action_to_op
    assert n({"action": "left_click", "coordinate": [12, 34]}, None) == {"op": "tap", "x": 12, "y": 34}
    assert n({"action": "double_click", "coordinate": [1, 2]}, None) == {"op": "tap", "x": 1, "y": 2}
    # type carries no coordinate → reuse last_click
    assert n({"action": "type", "text": "hi"}, (5, 6)) == {"op": "type", "x": 5, "y": 6, "text": "hi"}
    assert n({"action": "key", "text": "Return"}, None) == {"op": "press_key", "key": "enter"}
    assert n({"action": "key", "text": "BackSpace"}, None) == {"op": "press_key", "key": "delete"}
    assert n({"action": "key", "text": "Ctrl+A"}, None)["op"] == "unsupported"
    assert n({"action": "scroll", "scroll_direction": "up"}, None) == {"op": "scroll", "direction": "up"}
    assert n({"action": "left_click_drag", "start_coordinate": [1, 2], "coordinate": [3, 4]}, None) == {
        "op": "drag", "x": 1, "y": 2, "x2": 3, "y2": 4}
    assert n({"action": "screenshot"}, None) == {"op": "wait", "seconds": 0}
    assert n({"action": "frobnicate"}, None)["op"] == "unsupported"


def test_openai_action_to_op_table():
    n = fal._openai_action_to_op
    assert n(_ns(type="click", x=7, y=8, button="left"), None) == {"op": "tap", "x": 7, "y": 8}
    assert n(_ns(type="type", text="hi"), (3, 4)) == {"op": "type", "x": 3, "y": 4, "text": "hi"}
    assert n(_ns(type="scroll", x=1, y=1, scroll_x=0, scroll_y=120), None) == {
        "op": "scroll", "direction": "down"}
    assert n(_ns(type="scroll", x=1, y=1, scroll_x=-40, scroll_y=0), None) == {
        "op": "scroll", "direction": "left"}
    assert n(_ns(type="keypress", keys=["ENTER"]), None) == {"op": "press_key", "key": "enter"}
    assert n(_ns(type="keypress", keys=["CTRL", "A"]), None)["op"] == "unsupported"
    assert n(_ns(type="drag", path=[{"x": 1, "y": 2}, {"x": 3, "y": 4}]), None) == {
        "op": "drag", "x": 1, "y": 2, "x2": 3, "y2": 4}
    assert n(_ns(type="move", x=1, y=1), None) == {"op": "wait", "seconds": 0}
    assert n(_ns(type="wombat"), None)["op"] == "unsupported"


# ── fake SDK clients ─────────────────────────────────────────────────────────────────
class _FakeAnthropicClient:
    """Stand-in for AsyncAnthropic: canned `beta.messages.create` responses (one per turn)."""

    def __init__(self, script):
        script = list(script)

        class _Messages:
            def __init__(self):
                self.calls = []

            async def create(self, **kwargs):
                self.calls.append(kwargs)
                content = script.pop(0)
                return _ns(content=content, stop_reason="tool_use" if content else "end_turn")

        self.beta = _ns(messages=_Messages())


class _FakeOpenAIClient:
    """Stand-in for AsyncOpenAI: canned `responses.create` outputs (one per turn)."""

    def __init__(self, script):
        script = list(script)

        class _Responses:
            def __init__(self):
                self.calls = []

            async def create(self, **kwargs):
                self.calls.append(kwargs)
                rid, output = script.pop(0)
                return _ns(id=rid, output=output, usage=None)

        self.responses = _Responses()


def test_anthropic_driver_parses_a_computer_action():
    client = _FakeAnthropicClient([[_ns(type="tool_use", id="t1", name="computer",
                                         input={"action": "left_click", "coordinate": [359, 474]})]])
    d = fal.AnthropicDriver("claude-opus-4-6", "do it", "Brandon",
                            OBS_ABS["device_capability"], client=client)
    assert d.provider == "anthropic" and isinstance(d.adapter, fg.AnthropicCoordinateAdapter)
    dec = _run(d.next_action(OBS_ABS, None))
    assert dec.kind == "action" and dec.model_action == {"op": "tap", "x": 359, "y": 474}


def test_openai_driver_parses_a_function_nav_call():
    client = _FakeOpenAIClient([("r1", [_ns(type="function_call", call_id="f1",
                                             name="open_app", arguments='{"package":"com.x"}')])])
    d = fal.OpenAIDriver("gpt-5.5", "do it", "Brandon", OBS_ABS["device_capability"], client=client)
    assert d.provider == "openai" and isinstance(d.adapter, fg.OpenAICoordinateAdapter)
    dec = _run(d.next_action(OBS_ABS, None))
    assert dec.model_action == {"op": "open_app", "app": "com.x"}


# ════════════════════════════════════════════════════════════════════════════════════
# 7.3 — run_frontier_loop drives a multi-step task for EACH provider (mocked)
# ════════════════════════════════════════════════════════════════════════════════════
def _wire_recorder(monkeypatch):
    posted = []

    async def fake_pull(base_url, task_id, operator, timeout):
        return OBS_ABS

    async def fake_post(base_url, frame, timeout):
        posted.append(frame)
        return {"msg": "action_result", "success": True}

    monkeypatch.setattr(fal, "_pull_observation", fake_pull)
    monkeypatch.setattr(fal, "_post_action", fake_post)
    return posted


# downscaled coords that land on the field / submit nodes for each provider's long-edge cap.
_ANTH_FIELD = [359, 474]     # 1568/2400 of px(550,725)
_ANTH_SUBMIT = [359, 1094]   # 1568/2400 of px(550,1675)


def test_frontier_loop_anthropic_end_to_end(monkeypatch):
    _fast(monkeypatch)
    posted = _wire_recorder(monkeypatch)
    driver = fal.AnthropicDriver(
        "claude-opus-4-6", "log in and submit", "Brandon", OBS_ABS["device_capability"],
        client=_FakeAnthropicClient([
            [_ns(type="tool_use", id="t1", name="open_app", input={"package": "com.foo.bar"})],
            [_ns(type="tool_use", id="t2", name="computer",
                 input={"action": "left_click", "coordinate": _ANTH_FIELD})],
            [_ns(type="tool_use", id="t3", name="computer", input={"action": "type", "text": "hello"})],
            [_ns(type="tool_use", id="t4", name="computer",
                 input={"action": "left_click", "coordinate": _ANTH_SUBMIT})],
            [_ns(type="text", text="All set.")],
        ]))
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: driver)

    res = _run(fal.run_frontier_loop("http://phone:8765", "log in and submit", "Brandon",
                                     provider="anthropic"))
    assert res.success is True and res.final_text == "All set."
    kinds = [f["type"] for f in posted]
    assert kinds == ["open_app", "element_click", "element_set_text", "element_click"]
    assert posted[0]["package"] == "com.foo.bar"
    assert posted[1]["resource_id"] == "app:id/field"                       # abs-px rescaled → node
    assert posted[2]["resource_id"] == "app:id/field" and posted[2]["text"] == "hello"
    assert posted[3]["resource_id"] == "app:id/submit"
    for f in posted:
        assert f["msg"] == "action" and f["operator"] == "Brandon" and f["task_id"]


def _oa_cc(cid, action):
    return _ns(type="computer_call", call_id=cid, action=action, pending_safety_checks=[])


def test_frontier_loop_openai_end_to_end(monkeypatch):
    _fast(monkeypatch)
    posted = _wire_recorder(monkeypatch)
    # OpenAI long-edge 1280 → 1280/2400 of px(550,725)=(293,387), px(550,1675)=(293,894)
    driver = fal.OpenAIDriver(
        "gpt-5.5", "log in and submit", "Brandon", OBS_ABS["device_capability"],
        client=_FakeOpenAIClient([
            ("r1", [_ns(type="function_call", call_id="f1", name="open_app",
                        arguments='{"package":"com.foo.bar"}')]),
            ("r2", [_oa_cc("c2", _ns(type="click", x=293, y=387, button="left"))]),
            ("r3", [_oa_cc("c3", _ns(type="type", text="hello"))]),
            ("r4", [_oa_cc("c4", _ns(type="click", x=293, y=894, button="left"))]),
            ("r5", [_ns(type="message", content=[_ns(type="output_text", text="Done.")])]),
        ]))
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: driver)

    res = _run(fal.run_frontier_loop("http://phone:8765", "log in and submit", "Brandon",
                                     provider="openai"))
    assert res.success is True and res.final_text == "Done."
    kinds = [f["type"] for f in posted]
    assert kinds == ["open_app", "element_click", "element_set_text", "element_click"]
    assert posted[1]["resource_id"] == "app:id/field"
    assert posted[2]["resource_id"] == "app:id/field" and posted[2]["text"] == "hello"
    assert posted[3]["resource_id"] == "app:id/submit"


def test_frontier_loop_provider_param_threads_to_make_driver(monkeypatch):
    _fast(monkeypatch)
    _wire_recorder(monkeypatch)
    seen = {}

    class _FakeDriver(fal.FrontierDriver):
        provider = "openai"
        adapter = fg.OpenAICoordinateAdapter("gpt-5.5")

        async def next_action(self, observation, last_result):
            return Decision(kind="done", text="ok")

    def fake_make(provider, model, task, operator, capability):
        seen.update(provider=provider, model=model)
        return _FakeDriver()

    monkeypatch.setattr(fal, "_make_driver", fake_make)
    res = _run(fal.run_frontier_loop("http://phone:8765", "x", "Brandon", provider="openai"))
    assert res.success is True
    assert seen["provider"] == "openai"
    # provider-aware model default: OpenAI → the configured OpenAI frontier model (not the Gemini one)
    assert seen["model"] == fal._default_model_for_provider("openai")


# ════════════════════════════════════════════════════════════════════════════════════
# M3 — vision-first providers degrade to Gemini on a capture-less (XR) device
# ════════════════════════════════════════════════════════════════════════════════════
# A capture-less device: no screenshot capability and no "screenshot" key on the wire.
OBS_NOSHOT = {
    "msg": "observation",
    "ui_tree": [
        _node(0, "0,0,1080,2400", clickable=True, resource_id="root"),
        _node(1, "400,600,700,850", clickable=True, editable=True, resource_id="app:id/field"),
    ],
    "device_capability": {"formFactor": "xr_headset", "hasScreenshot": False,
                          "supportsCoordinateGesture": False, "displayId": 0},
    "timestamp": 1,
}


def test_capture_less_fallback_pure():
    noshot = {"hasScreenshot": False}
    withshot = {"hasScreenshot": True}
    gem_model = fal._default_model_for_provider("gemini")
    # vision-first providers → degrade to Gemini + Gemini's default model
    assert fal._capture_less_fallback("openai", "gpt-5.5", noshot) == ("gemini", gem_model)
    assert fal._capture_less_fallback("claude", "claude-opus-4-8", noshot) == ("gemini", gem_model)
    assert fal._capture_less_fallback("anthropic", "m", noshot)[0] == "gemini"
    assert fal._capture_less_fallback("gpt", "m", noshot)[0] == "gemini"
    # a device WITH a screenshot → unchanged
    assert fal._capture_less_fallback("openai", "gpt-5.5", withshot) == ("openai", "gpt-5.5")
    # missing hasScreenshot defaults to True (assume capture) → unchanged
    assert fal._capture_less_fallback("openai", "gpt-5.5", {}) == ("openai", "gpt-5.5")
    # gemini/gemma are never rerouted, even on a capture-less device
    assert fal._capture_less_fallback("gemini", "g", noshot) == ("gemini", "g")
    assert fal._capture_less_fallback("gemma", "g", noshot) == ("gemma", "g")


def test_capture_less_device_openai_falls_back_to_gemini_in_loop(monkeypatch):
    # M3 end-to-end: provider=openai on a capture-less device builds the GEMINI driver — not an
    # empty-screenshot vision loop. The fallback happens before _make_driver is called.
    _fast(monkeypatch)

    async def fake_pull(base_url, task_id, operator, timeout):
        return OBS_NOSHOT

    async def fake_post(base_url, frame, timeout):
        return {"msg": "action_result", "success": True}

    monkeypatch.setattr(fal, "_pull_observation", fake_pull)
    monkeypatch.setattr(fal, "_post_action", fake_post)
    seen = {}

    class _FakeGeminiDriver(fal.FrontierDriver):
        provider = "gemini"
        adapter = fg.GeminiCoordinateAdapter()

        async def next_action(self, observation, last_result):
            return Decision(kind="done", text="ok")

    def fake_make(provider, model, task, operator, capability):
        seen.update(provider=provider, model=model)
        return _FakeGeminiDriver()

    monkeypatch.setattr(fal, "_make_driver", fake_make)
    res = _run(fal.run_frontier_loop("http://xr:8765", "search", "Brandon", provider="openai"))
    assert res.success is True
    assert seen["provider"] == "gemini"                          # degraded, not "openai"
    assert seen["model"] == fal._default_model_for_provider("gemini")


def test_capture_less_gemini_is_not_rerouted_in_loop(monkeypatch):
    # Gemini on a capture-less device stays Gemini (tree-first path is capture-independent).
    _fast(monkeypatch)

    async def fake_pull(base_url, task_id, operator, timeout):
        return OBS_NOSHOT

    async def fake_post(base_url, frame, timeout):
        return {"msg": "action_result", "success": True}

    monkeypatch.setattr(fal, "_pull_observation", fake_pull)
    monkeypatch.setattr(fal, "_post_action", fake_post)
    seen = {}

    class _FakeGeminiDriver(fal.FrontierDriver):
        provider = "gemini"
        adapter = fg.GeminiCoordinateAdapter()

        async def next_action(self, observation, last_result):
            return Decision(kind="done", text="ok")

    monkeypatch.setattr(fal, "_make_driver",
                        lambda provider, *a, **k: seen.update(provider=provider) or _FakeGeminiDriver())
    res = _run(fal.run_frontier_loop("http://xr:8765", "search", "Brandon", provider="gemini"))
    assert res.success is True and seen["provider"] == "gemini"


# ════════════════════════════════════════════════════════════════════════════════════
# 7.4 / 7.5 — control_device provider selection + gemma routing
# ════════════════════════════════════════════════════════════════════════════════════
_EXEC_PATH = (Path(__file__).resolve().parents[2]
              / "ToolVault" / "tools" / "control_device" / "executor.py")
_spec = importlib.util.spec_from_file_location("control_device_executor_m7", _EXEC_PATH)
cd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cd)

NODE = Node(hostname="brandon-fold6", dns_name="brandon-fold6.tailnet-abc.ts.net",
            ip="100.88.0.7", online=True, os="android")
CTX = ToolContext(operator="Brandon", base_url="http://localhost:9091")


def _patch_resolve(monkeypatch, node=NODE):
    monkeypatch.setattr(cd.mesh, "resolve_device", lambda **kw: node)


def _capture_loop(monkeypatch):
    seen = {}

    async def fake_loop(*, device_base_url, task, operator, model, capability, provider):
        seen.update(provider=provider, model=model, task=task)
        return fal.FrontierResult(True, "done", steps=1, device=device_base_url)

    monkeypatch.setattr(cd.frontier_agent_loop, "run_frontier_loop", fake_loop)
    return seen


def test_control_device_uses_config_default_provider(monkeypatch):
    _patch_resolve(monkeypatch)
    monkeypatch.setattr(cd.mesh, "default_provider_for_node", lambda *a, **k: None)
    seen = _capture_loop(monkeypatch)
    res = _run(cd.execute({"task": "open maps"}, CTX))
    assert res.success is True
    assert seen["provider"] == cd._config_default_provider()      # box default
    assert res.data["provider"] == seen["provider"]


def test_control_device_reads_device_default_provider(monkeypatch):
    # M7 7.5: the persisted-but-unconsumed M3 default_provider is now LIVE.
    _patch_resolve(monkeypatch)
    monkeypatch.setattr(cd.mesh, "default_provider_for_node", lambda *a, **k: "openai")
    seen = _capture_loop(monkeypatch)
    res = _run(cd.execute({"task": "open maps"}, CTX))
    assert res.success is True and seen["provider"] == "openai"


def test_control_device_explicit_provider_overrides_device_default(monkeypatch):
    _patch_resolve(monkeypatch)
    monkeypatch.setattr(cd.mesh, "default_provider_for_node", lambda *a, **k: "gemini")
    seen = _capture_loop(monkeypatch)
    res = _run(cd.execute({"task": "open maps", "provider": "claude"}, CTX))
    assert res.success is True and seen["provider"] == "claude"    # explicit wins over device default


def test_control_device_gemma_routes_to_on_device_control_phone(monkeypatch):
    # M7 7.4: gemma routes to the on-device Gemma path (control_phone), not the cloud loop.
    import Orchestrator.toolvault.registry as _reg
    _patch_resolve(monkeypatch)
    called = {}

    async def fake_control_phone(params, ctx):
        called.update(params=params, operator=ctx.operator)
        return ToolResult(True, "Gemma did it on the phone.", data={"phase": "done"})

    monkeypatch.setattr(_reg, "get_executor",
                        lambda name: fake_control_phone if name == "control_phone" else None)
    # the loop must NOT be called on the gemma path
    async def boom(**kw):
        raise AssertionError("cloud loop must not run for provider=gemma")
    monkeypatch.setattr(cd.frontier_agent_loop, "run_frontier_loop", boom)

    res = _run(cd.execute({"task": "open maps", "provider": "gemma"}, CTX))
    assert res.success is True and res.result == "Gemma did it on the phone."
    assert res.data["provider"] == "gemma"                         # tagged for the caller
    assert called["operator"] == "Brandon"


def test_control_device_gemma_via_device_default(monkeypatch):
    import Orchestrator.toolvault.registry as _reg
    _patch_resolve(monkeypatch)
    monkeypatch.setattr(cd.mesh, "default_provider_for_node", lambda *a, **k: "gemma")
    called = {}

    async def fake_control_phone(params, ctx):
        called["hit"] = True
        return ToolResult(True, "on-device", data={})

    monkeypatch.setattr(_reg, "get_executor", lambda name: fake_control_phone)
    res = _run(cd.execute({"task": "x"}, CTX))
    assert called.get("hit") is True and res.data["provider"] == "gemma"


def test_control_device_rejects_unknown_provider(monkeypatch):
    _patch_resolve(monkeypatch)
    res = _run(cd.execute({"task": "x", "provider": "acme"}, CTX))
    assert res.success is False and res.data["error_kind"] == "invalid_argument"


def test_control_device_rejects_bad_device_default_provider(monkeypatch):
    # M7-M4: a bad RESOLVED provider (a hypothetical bad device default_provider) reads as
    # invalid_argument uniformly — same as an explicit bad param — instead of a downstream
    # config_error. Belt-and-suspenders (the registry already sanitizes the device default).
    _patch_resolve(monkeypatch)
    monkeypatch.setattr(cd.mesh, "default_provider_for_node", lambda *a, **k: "bogus")
    res = _run(cd.execute({"task": "x"}, CTX))
    assert res.success is False and res.data["error_kind"] == "invalid_argument"
    assert res.data["provider"] == "bogus"


def test_control_device_resolution_error_still_surfaces(monkeypatch):
    def _raise(**kw):
        raise DeviceResolutionError("no_device", "nothing online")
    monkeypatch.setattr(cd.mesh, "resolve_device", _raise)
    res = _run(cd.execute({"task": "x", "provider": "claude"}, CTX))
    assert res.success is False and res.data["error_kind"] == "no_device"
