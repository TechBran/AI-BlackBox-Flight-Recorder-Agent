"""Anthropic CU thinking-signature round-trip (M4, 2026-07-23).

Caught LIVE by the M0 click battery: claude-sonnet-5 (adaptive thinking) emits
a `thinking` block even when the request sets no thinking param. The driver
replayed it into the next request WITHOUT its `signature`, and the API 400'd
("messages.1.content.0.thinking.signature: Field required") — every multi-step
CU run died after the FIRST action while the click itself landed 1.0px true.

Contract under test: the driver captures signature_delta and round-trips the
signed thinking block; an unsigned thinking block is dropped from history
(unreplayable), never sent back bare.
"""
import json
import sys

import pytest

from Orchestrator.browser.session_manager import ComputerUseSession


def _sse(events):
    return ["data: " + json.dumps(e) for e in events]


def _thinking_tool_turn(signed: bool):
    events = [
        {"type": "content_block_start", "content_block": {"type": "thinking"}},
        {"type": "content_block_delta",
         "delta": {"type": "thinking_delta", "thinking": "I will click A."}},
    ]
    if signed:
        events.append({"type": "content_block_delta",
                       "delta": {"type": "signature_delta",
                                 "signature": "sig-ABC123"}})
    events += [
        {"type": "content_block_stop"},
        {"type": "content_block_start",
         "content_block": {"type": "tool_use", "id": "tu_1", "name": "computer"}},
        {"type": "content_block_delta",
         "delta": {"type": "input_json_delta",
                   "partial_json": "{\"action\": \"screenshot\"}"}},
        {"type": "content_block_stop"},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
         "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    return _sse(events)


END_TURN = _sse([
    {"type": "content_block_start", "content_block": {"type": "text"}},
    {"type": "content_block_delta",
     "delta": {"type": "text_delta", "text": "done"}},
    {"type": "content_block_stop"},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
     "usage": {"input_tokens": 1, "output_tokens": 1}},
])


class _ScriptedHTTPX:
    """httpx stand-in that replays one scripted SSE body per API call."""

    class TimeoutException(Exception):
        pass

    class ConnectError(Exception):
        pass

    def __init__(self, api_calls, turns):
        self._api_calls = api_calls
        self._turns = turns

    def AsyncClient(self, **kwargs):
        api_calls, turns = self._api_calls, self._turns

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
                # DEEP copy: the driver mutates the shared history list after
                # this call; recording by reference would retro-contaminate
                # earlier records with later state (the wire saw a snapshot).
                import copy as _copy
                api_calls.append({"payload": _copy.deepcopy(json)})
                return _Ctx(turns[min(len(api_calls) - 1, len(turns) - 1)])

        return _Client()


@pytest.fixture
def cu_env(monkeypatch):
    monkeypatch.setattr(ComputerUseSession, "capture_screenshot_bytes",
                        lambda self: b"\x89PNG-fake" * 30)
    monkeypatch.setattr(
        "Orchestrator.browser.screenshot.save_screenshot_to_uploads",
        lambda png, ident, step: "/ui/uploads/x.png")
    from Orchestrator.routes import chat_routes

    async def _no_save(*a, **k):
        return None
    monkeypatch.setattr(chat_routes, "_cu_save_to_blackbox", _no_save)
    return ComputerUseSession("sig-test-op")


async def _run(session, monkeypatch, turns):
    api_calls = []
    monkeypatch.setitem(sys.modules, "httpx",
                        _ScriptedHTTPX(api_calls, turns))
    from Orchestrator.browser.driver_anthropic import run_anthropic_cu_loop
    history = [{"role": "user", "content": [{"type": "text", "text": "click A"}]}]
    await run_anthropic_cu_loop(session, history, "sys", [], {},
                                "claude-sonnet-5", "sig-test-op", "click A")
    return api_calls


def _assistant_blocks(payload, block_type):
    return [b for m in payload["messages"]
            if m.get("role") == "assistant" and isinstance(m.get("content"), list)
            for b in m["content"] if b.get("type") == block_type]


@pytest.mark.asyncio
async def test_signed_thinking_round_trips_with_signature(cu_env, monkeypatch):
    api_calls = await _run(cu_env, monkeypatch,
                           [_thinking_tool_turn(signed=True), END_TURN])
    assert len(api_calls) == 2  # step 1 (thinking+tool) then step 2 (end_turn)
    blocks = _assistant_blocks(api_calls[1]["payload"], "thinking")
    assert blocks, "the signed thinking block must be replayed"
    assert blocks[0]["signature"] == "sig-ABC123"
    assert blocks[0]["thinking"] == "I will click A."


@pytest.mark.asyncio
async def test_unsigned_thinking_is_dropped_not_replayed_bare(cu_env, monkeypatch):
    api_calls = await _run(cu_env, monkeypatch,
                           [_thinking_tool_turn(signed=False), END_TURN])
    assert len(api_calls) == 2
    payload = api_calls[1]["payload"]
    assert _assistant_blocks(payload, "thinking") == []  # bare block would 400
    # The tool_use/tool_result exchange itself survives intact.
    tool_uses = _assistant_blocks(payload, "tool_use")
    assert tool_uses and tool_uses[0]["id"] == "tu_1"
