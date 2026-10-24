"""Frontier device-control loop reliability (audit O06-frontier-agent-07, W20, rank 64).

* Every /action frame carries an idempotency key (``action_id``) minted once per
  dispatched action and REUSED by every retry of that action, so a retry after a
  lost response can be deduplicated on the device instead of tapping twice.
* Both vision drivers keep at most three screenshots in the request: the
  Gemini driver used to append a PNG per step and the Anthropic driver an image
  block per step, so step 40 carried 40 screenshots.
* The Anthropic driver streams with the model's real output cap and an explicit
  timeout, instead of a non-streaming 4096-token create that ended every
  always-thinking 5.5 turn as model_error once thinking ate the budget.
"""
import asyncio
import base64
import struct
import types as pytypes

import pytest

from Orchestrator import frontier_agent_loop as fal
from Orchestrator.frontier_agent_loop import Decision


def _run(coro):
    return asyncio.run(coro)


def _ns(**kw):
    return pytypes.SimpleNamespace(**kw)


def _fake_png(width, height):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"
    return sig + ihdr


def _node(node_id, bounds, *, clickable=False, editable=False, resource_id=""):
    return {"node_id": node_id, "role": "View", "text": "", "resource_id": resource_id,
            "bounds": bounds, "clickable": clickable, "editable": editable, "is_password": False}


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


def _fast(monkeypatch, retries=0):
    monkeypatch.setattr(fal, "_retry_max", lambda: retries)
    monkeypatch.setattr(fal, "_retry_backoff_secs", lambda: 0.0)
    monkeypatch.setattr(fal, "_per_action_secs", lambda: 5.0)
    monkeypatch.setattr(fal, "_per_turn_secs", lambda: 5.0)
    monkeypatch.setattr(fal, "_session_base_secs", lambda: 300.0)
    monkeypatch.setattr(fal, "_session_max_secs", lambda: 600.0)
    monkeypatch.setattr(fal, "_max_steps", lambda: 40)


class _ScriptDriver:
    def __init__(self, script):
        self._script = list(script)

    async def next_action(self, observation, last_result):
        if not self._script:
            return Decision(kind="done", text="done")
        return Decision(kind="action", model_action=self._script.pop(0))


# ── idempotency key ─────────────────────────────────────────────────────────

def test_every_frame_carries_a_distinct_action_id(monkeypatch):
    _fast(monkeypatch)
    posted = []

    async def pull(base_url, task_id, operator, timeout, headers=None):
        return {k: v for k, v in OBS.items() if k != "screenshot"}

    async def post(base_url, frame, timeout, headers=None):
        posted.append(dict(frame))
        return {"msg": "action_result", "success": True}

    monkeypatch.setattr(fal, "_pull_observation", pull)
    monkeypatch.setattr(fal, "_post_action", post)
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: _ScriptDriver(
        [{"op": "back"}, {"op": "home"}, {"op": "tap", "x": 500, "y": 700}]))
    res = _run(fal.run_frontier_loop("http://phone:8765", "x", "op"))
    assert res.success
    ids = [f["action_id"] for f in posted]
    assert len(ids) == 3 and len(set(ids)) == 3
    assert all(isinstance(i, str) and len(i) >= 16 for i in ids)


def test_a_retried_action_reuses_its_action_id(monkeypatch):
    _fast(monkeypatch, retries=2)
    posted = []

    async def pull(base_url, task_id, operator, timeout, headers=None):
        return {k: v for k, v in OBS.items() if k != "screenshot"}

    async def flaky(base_url, frame, timeout, headers=None):
        posted.append(dict(frame))
        if len(posted) == 1:
            raise ConnectionResetError("response lost after the device acted")
        return {"msg": "action_result", "success": True}

    monkeypatch.setattr(fal, "_pull_observation", pull)
    monkeypatch.setattr(fal, "_post_action", flaky)
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: _ScriptDriver(
        [{"op": "tap", "x": 500, "y": 700}]))
    assert _run(fal.run_frontier_loop("http://phone:8765", "x", "op")).success
    assert len(posted) == 2
    assert posted[0]["action_id"] == posted[1]["action_id"]


def test_press_enter_followon_gets_its_own_action_id(monkeypatch):
    _fast(monkeypatch)
    posted = []

    async def pull(base_url, task_id, operator, timeout, headers=None):
        return {k: v for k, v in OBS.items() if k != "screenshot"}

    async def post(base_url, frame, timeout, headers=None):
        posted.append(dict(frame))
        return {"msg": "action_result", "success": True}

    monkeypatch.setattr(fal, "_pull_observation", pull)
    monkeypatch.setattr(fal, "_post_action", post)
    monkeypatch.setattr(fal, "_make_driver", lambda *a, **k: _ScriptDriver(
        [{"op": "type", "x": 500, "y": 300, "text": "hi", "press_enter": True}]))
    assert _run(fal.run_frontier_loop("http://phone:8765", "x", "op")).success
    assert [f["type"] for f in posted] == ["element_set_text", "press_key"]
    assert posted[0]["action_id"] != posted[1]["action_id"]


# ── Anthropic driver: bounded screenshots, streaming, real output cap ───────

class _StreamingAnthropic:
    """AsyncAnthropic stand-in exposing ONLY the streaming surface: a regression to the
    non-streaming create() would raise AttributeError."""

    def __init__(self, script):
        script = list(script)
        outer = self
        self.calls = []

        class _Stream:
            def __init__(self, kw):
                self.kw = kw

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get_final_message(self):
                content = script.pop(0) if script else []
                return _ns(content=content, stop_reason="tool_use" if content else "end_turn")

        class _Messages:
            def stream(self, **kw):
                outer.calls.append(dict(kw, messages=[dict(m) for m in kw["messages"]]))
                return _Stream(kw)

        self.beta = _ns(messages=_Messages())


def _member(tid, name, **inp):
    return _ns(type="tool_use", id=tid, name=name, toolset_name="computer", input=inp)


def _images_in(messages):
    n = 0
    for m in messages:
        for b in m["content"] if isinstance(m["content"], list) else []:
            if b.get("type") == "image":
                n += 1
            elif b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                n += sum(1 for i in b["content"] if i.get("type") == "image")
    return n


def test_anthropic_driver_keeps_at_most_three_screenshots_over_ten_steps():
    script = [[_ns(type="thinking", thinking="", signature=f"s{i}"),
               _member(f"t{i}", "left_click", coordinate=[550, 725])] for i in range(10)]
    client = _StreamingAnthropic(script)
    d = fal.AnthropicDriver("claude-opus-5-5", "do it", "op", CAP, client=client)
    for i in range(10):
        _run(d.next_action(OBS, {"success": True} if i else None))
    counts = [_images_in(c["messages"]) for c in client.calls]
    assert counts[:3] == [1, 2, 3]
    assert max(counts) <= 3
    # Once older screenshots were elided the signed thinking blocks (whose prefix no
    # longer matches) are not replayed; every tool_use is still answered.
    last = client.calls[-1]["messages"]
    assert not any(b.get("type") == "thinking" for m in last for b in m["content"])
    for idx, m in enumerate(last):
        uses = [b["id"] for b in m["content"] if b.get("type") == "tool_use"]
        if uses:
            answered = [b["tool_use_id"] for b in last[idx + 1]["content"]
                        if b.get("type") == "tool_result"]
            assert answered == uses


def test_anthropic_driver_streams_with_the_model_output_cap_and_a_timeout(monkeypatch):
    from Orchestrator.browser.config import anthropic_cu_max_tokens
    monkeypatch.setattr(fal, "_per_turn_secs", lambda: 42.0)
    for model in ("claude-opus-5-5", "claude-sonnet-4-5"):
        client = _StreamingAnthropic([[_member("t1", "left_click", coordinate=[1, 1])]])
        d = fal.AnthropicDriver(model, "do it", "op", CAP, client=client)
        _run(d.next_action(OBS, None))
        call = client.calls[0]
        assert call["max_tokens"] == anthropic_cu_max_tokens(model) > 4096
        assert 0 < call["timeout"] <= 42.0


# ── Gemini mobile driver: bounded screenshots ───────────────────────────────

def test_gemini_driver_keeps_at_most_three_screenshots_over_ten_steps(monkeypatch):
    from google import genai
    from google.genai import types
    sent = []

    class _Models:
        async def generate_content(self, model, contents, config):
            imgs = sum(1 for c in contents for p in (c.parts or [])
                       if getattr(p, "inline_data", None) is not None
                       and (p.inline_data.mime_type or "").startswith("image/"))
            sent.append(imgs)
            content = types.Content(role="model", parts=[
                types.Part.from_function_call(name="click_at", args={"x": 500, "y": 300})])
            return _ns(candidates=[_ns(content=content, finish_reason=None)])

    class _Client:
        def __init__(self, *a, **k):
            self.aio = _ns(models=_Models())

    monkeypatch.setattr(genai, "Client", _Client)
    d = fal.GeminiMobileDriver("gemini-test", "do it", "op", CAP)
    for i in range(10):
        _run(d.next_action(OBS, {"success": True} if i else None))
    assert sent[:3] == [1, 2, 3]
    assert max(sent) <= 3
