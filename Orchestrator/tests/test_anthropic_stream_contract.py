"""Contract tests for the Claude chat paths (audit rank 58).

Recorded Messages API event sequences are fed through
stream_anthropic_with_thinking (a fake httpx stream) and canned responses
through call_anthropic (a fake requests.post). They pin what the paths must do
with each way a Claude turn can end:

  * thinking + tool + text (O02-chat-routes-A-01): the assistant turn replayed
    after a tool call carries every thinking / redacted_thinking block, with
    its signature, in stream order ahead of the text and tool_use blocks. The
    stream path used to drop them (no signature_delta handling at all) and
    put the text first, so the model lost its reasoning between tool calls and
    the next request risked a signature 400.
  * stop_reason "max_tokens" (O02-chat-routes-A-09): the reply ends with a
    visible truncation marker instead of passing for a complete answer, and a
    tool call cut off mid-input is never run.
  * stop_reason "refusal": an error (stream) / a decline notice (non-stream)
    with the stop_details category, never a blank or partial "answer".
  * the 30-iteration tool cap (O02-chat-routes-A-11): the stream still ends
    with content + done; it used to end with neither.
  * a mid-stream error event or a stream that stops before message_delta: an
    error, never a done that marks the partial reply as complete (a client
    still saves content that streamed before the error, with the error).
  * the streaming history merges adjacent same-role turns (O02-chat-routes-A-14).
  * the non-stream call has a (connect, read) timeout that a long xhigh turn
    fits in, and MCP chat_with_context waits at least that long (W21).

Hermetic: no network, the billing state file is a tmp path, web_fetch is a
stub, and uploads resolve under a tmp BLACKBOX_ROOT.
"""
import ast
import asyncio
import copy
import json
from pathlib import Path

import pytest

from Orchestrator import provider_billing as pb
from Orchestrator.utils import paths

MODEL = "claude-opus-5-5"
MSGS = [{"role": "user", "content": "What does example.com say?"}]
_MCP_SERVER = Path(__file__).resolve().parents[2] / "MCP" / "blackbox_mcp_server.py"


# --------------------------------------------------------------------------- #
# Recorded event sequences                                                    #
# --------------------------------------------------------------------------- #

def _message_start():
    return {"type": "message_start", "message": {
        "id": "msg_01", "type": "message", "role": "assistant", "model": MODEL,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 40, "output_tokens": 1}}}


def _thinking(index, text, signature):
    events = [{"type": "content_block_start", "index": index,
               "content_block": {"type": "thinking", "thinking": "", "signature": ""}}]
    if text:
        events.append({"type": "content_block_delta", "index": index,
                       "delta": {"type": "thinking_delta", "thinking": text}})
    events += [
        {"type": "content_block_delta", "index": index,
         "delta": {"type": "signature_delta", "signature": signature}},
        {"type": "content_block_stop", "index": index},
    ]
    return events


def _redacted(index, data):
    return [
        {"type": "content_block_start", "index": index,
         "content_block": {"type": "redacted_thinking", "data": data}},
        {"type": "content_block_stop", "index": index},
    ]


def _text(index, *chunks):
    return ([{"type": "content_block_start", "index": index,
              "content_block": {"type": "text", "text": ""}}]
            + [{"type": "content_block_delta", "index": index,
                "delta": {"type": "text_delta", "text": c}} for c in chunks]
            + [{"type": "content_block_stop", "index": index}])


def _tool_use(index, tool_id, name, *json_chunks, stop=True):
    events = [{"type": "content_block_start", "index": index,
               "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}}]
    events += [{"type": "content_block_delta", "index": index,
                "delta": {"type": "input_json_delta", "partial_json": c}} for c in json_chunks]
    if stop:
        events.append({"type": "content_block_stop", "index": index})
    return events


def _end(stop_reason, stop_details=None):
    delta = {"stop_reason": stop_reason, "stop_sequence": None}
    if stop_details is not None:
        delta["stop_details"] = stop_details
    return [{"type": "message_delta", "delta": delta, "usage": {"output_tokens": 50}},
            {"type": "message_stop"}]


FETCH = ('{"url": "https://exa', 'mple.com/"}')

THINK_TEXT_TOOL = ([_message_start()]
                   + _thinking(0, "The user wants the page. I will fetch it.", "sig-one")
                   + _text(1, "Fetching ", "it now.")
                   + _tool_use(2, "toolu_01", "web_fetch", *FETCH)
                   + _end("tool_use"))
THINK_ANSWER = ([_message_start()]
                + _thinking(0, "The page says hello.", "sig-two")
                + _text(1, "The page says ", "hello.")
                + _end("end_turn"))
TOOL_ONLY = [_message_start()] + _tool_use(0, "toolu_loop", "web_fetch", *FETCH) + _end("tool_use")


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #

class _ScriptedStream:
    """httpx.AsyncClient stand-in: one recorded event list per request (the last
    one repeats), and a deep copy of every request body sent."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.sent = []
        self.client_kwargs = []

    def factory(self):
        outer = self

        class _Resp:
            status_code = 200

            def __init__(self, events):
                self._lines = []
                for e in events:
                    self._lines += [f"event: {e['type']}", f"data: {json.dumps(e)}", ""]

            async def aread(self):
                return b""

            async def aiter_lines(self):
                for line in self._lines:
                    yield line

        class _Ctx:
            def __init__(self, events):
                self._events = events

            async def __aenter__(self):
                return _Resp(self._events)

            async def __aexit__(self, *exc):
                return False

        class _Client:
            def __init__(self, *a, **kw):
                outer.client_kwargs.append(kw)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def stream(self, method, url, headers=None, json=None):
                outer.sent.append(copy.deepcopy(json))
                script = outer.scripts.pop(0) if len(outer.scripts) > 1 else outer.scripts[0]
                return _Ctx(script)

        return _Client


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = "fake"

    def json(self):
        return self._payload


@pytest.fixture
def cr(tmp_path, monkeypatch):
    import Orchestrator.routes.chat_routes as cr

    monkeypatch.setattr(cr, "ANTHROPIC_API_KEY", "test-anthropic", raising=False)
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(cr, "read_text_safe", lambda *a, **k: "")
    monkeypatch.setattr(cr, "perform_web_fetch", lambda url, max_chars=80000: f"page text of {url}")
    monkeypatch.setattr(pb, "STATE_PATH", tmp_path / "provider_billing_state.json")
    pb._reset_cache()
    root = tmp_path / "box"
    (root / "Portal" / "uploads").mkdir(parents=True)
    monkeypatch.setenv("BLACKBOX_ROOT", str(root))
    paths.blackbox_root.cache_clear()
    yield cr
    paths.blackbox_root.cache_clear()
    pb._reset_cache()


def _stream(cr, monkeypatch, *scripts, messages=MSGS):
    fake = _ScriptedStream(*scripts)
    monkeypatch.setattr(cr.httpx, "AsyncClient", fake.factory())

    async def drain():
        return [ev async for ev in cr.stream_anthropic_with_thinking(messages, MODEL, "test-op")]

    return asyncio.run(drain()), fake


def _types(events):
    return [e["type"] for e in events]


# --------------------------------------------------------------------------- #
# Stream: thinking blocks survive the tool loop                               #
# --------------------------------------------------------------------------- #

def test_stream_replays_thinking_with_its_signature_in_stream_order(cr, monkeypatch):
    events, fake = _stream(cr, monkeypatch, THINK_TEXT_TOOL, THINK_ANSWER)

    assert len(fake.sent) == 2
    assistant, tool_turn = fake.sent[1]["messages"][-2:]
    assert assistant == {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "The user wants the page. I will fetch it.",
         "signature": "sig-one"},
        {"type": "text", "text": "Fetching it now."},
        {"type": "tool_use", "id": "toolu_01", "name": "web_fetch",
         "input": {"url": "https://example.com/"}},
    ]}
    assert tool_turn["role"] == "user"
    assert tool_turn["content"][0]["tool_use_id"] == "toolu_01"
    assert "page text of https://example.com/" in tool_turn["content"][0]["content"]
    assert events[-1] == {"type": "done", "data": {
        "thinking": "The page says hello.", "content": "The page says hello."}}


def test_stream_replays_redacted_and_repeated_thinking_blocks_verbatim(cr, monkeypatch):
    first = ([_message_start()]
             + _redacted(0, "EmwKAhgBEgy-opaque-redacted")
             + _thinking(1, "first thought", "sig-a")
             + _thinking(2, "second thought", "sig-b")
             + _thinking(3, "", "sig-empty")   # display "omitted": empty text, still a block
             + _tool_use(4, "toolu_02", "web_fetch", *FETCH)
             + _end("tool_use"))
    events, fake = _stream(cr, monkeypatch, first, THINK_ANSWER)

    replay = fake.sent[1]["messages"][-2]["content"]
    assert replay == [
        {"type": "redacted_thinking", "data": "EmwKAhgBEgy-opaque-redacted"},
        {"type": "thinking", "thinking": "first thought", "signature": "sig-a"},
        {"type": "thinking", "thinking": "second thought", "signature": "sig-b"},
        {"type": "thinking", "thinking": "", "signature": "sig-empty"},
        {"type": "tool_use", "id": "toolu_02", "name": "web_fetch",
         "input": {"url": "https://example.com/"}},
    ]
    assert events[-1]["type"] == "done"


# --------------------------------------------------------------------------- #
# Stream: every stop reason ends the turn honestly                            #
# --------------------------------------------------------------------------- #

def test_stream_marks_a_max_tokens_reply_as_truncated(cr, monkeypatch):
    script = [_message_start()] + _text(0, "Here is the long ", "answer") + _end("max_tokens")
    events, _ = _stream(cr, monkeypatch, script)

    done = events[-1]
    assert done["type"] == "done"
    assert done["data"]["content"].startswith("Here is the long answer")
    assert "truncated" in done["data"]["content"].lower()
    # The marker also streamed, so the bubble shows it before done replaces it.
    streamed = "".join(e["data"] for e in events if e["type"] == "content")
    assert streamed == done["data"]["content"]


def test_stream_never_runs_a_tool_call_cut_off_by_max_tokens(cr, monkeypatch):
    calls = []
    monkeypatch.setattr(cr, "perform_web_fetch", lambda *a, **k: calls.append(a) or "x")
    script = ([_message_start()] + _text(0, "Let me fetch.")
              + _tool_use(1, "toolu_cut", "web_fetch", '{"url": "https://exa', stop=False)
              + _end("max_tokens"))
    events, fake = _stream(cr, monkeypatch, script)

    assert len(fake.sent) == 1 and not calls
    assert events[-1]["type"] == "done"
    assert "truncated" in events[-1]["data"]["content"].lower()


def test_stream_refusal_is_an_error_with_its_category(cr, monkeypatch):
    script = ([_message_start()] + _text(0, "Sure, here")
              + _end("refusal", {"type": "refusal", "category": "cyber",
                                 "explanation": "This request was declined by a safety classifier."}))
    events, _ = _stream(cr, monkeypatch, script)

    assert "done" not in _types(events), "a refusal must not end in a done for the partial reply"
    err = events[-1]
    assert err["type"] == "error"
    assert "declined" in err["data"] and "cyber" in err["data"]
    assert "safety classifier" in err["data"]
    assert err["error_detail"]["kind"] == "refusal"
    assert err["error_detail"]["reason"] == "cyber"


def test_stream_refusal_without_details_is_still_an_error(cr, monkeypatch):
    events, _ = _stream(cr, monkeypatch, [_message_start()] + _end("refusal"))
    assert _types(events)[-1] == "error"
    assert "declined" in events[-1]["data"]


def test_stream_ends_with_content_and_done_after_the_tool_cap(cr, monkeypatch):
    events, fake = _stream(cr, monkeypatch, TOOL_ONLY)

    assert len(fake.sent) == 30, "the tool loop must stop at its cap"
    assert events[-1]["type"] == "done"
    assert "30 rounds of tool calls" in events[-1]["data"]["content"]
    assert events[-2]["type"] == "content"


def test_stream_error_event_is_an_error_not_a_done(cr, monkeypatch):
    script = ([_message_start()] + _text(0, "Half an ans")[:2]
              + [{"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}])
    events, _ = _stream(cr, monkeypatch, script)
    assert "done" not in _types(events)
    assert events[-1]["type"] == "error" and "Overloaded" in events[-1]["data"]


def test_stream_that_stops_before_message_delta_is_an_error(cr, monkeypatch):
    script = [_message_start()] + _text(0, "Half an answer")
    events, _ = _stream(cr, monkeypatch, script)
    assert "done" not in _types(events)
    assert events[-1]["type"] == "error"


def test_stream_exception_is_logged_with_its_traceback(cr, monkeypatch, capsys):
    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise RuntimeError("connection reset by test")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(cr.httpx, "AsyncClient", _Boom)

    async def drain():
        return [ev async for ev in cr.stream_anthropic_with_thinking(MSGS, MODEL, "test-op")]

    events = asyncio.run(drain())
    assert events[-1]["type"] == "error" and "connection reset by test" in events[-1]["data"]
    assert "Traceback" in capsys.readouterr().out


def test_stream_history_alternates_roles(cr, monkeypatch):
    history = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": ""},  # empty turn: dropped
        {"role": "user", "content": [  # image-only turn whose upload is gone: dropped
            {"type": "image_url", "image_url": {"url": "/ui/uploads/gone.png"}}]},
        {"role": "user", "content": "second question"},
    ]
    _, fake = _stream(cr, monkeypatch, THINK_ANSWER, messages=history)

    roles = [m["role"] for m in fake.sent[0]["messages"]]
    assert all(a != b for a, b in zip(roles, roles[1:])), roles
    assert roles[0] == "user"
    sent_text = json.dumps(fake.sent[0]["messages"])
    assert "first question" in sent_text and "second question" in sent_text


def test_stream_sends_what_anthropic_caps_decides(cr, monkeypatch):
    from Orchestrator.config import anthropic_caps

    _, fake = _stream(cr, monkeypatch, THINK_ANSWER)
    caps = anthropic_caps(MODEL)
    assert fake.sent[0]["thinking"] == caps.thinking
    assert fake.sent[0]["output_config"] == {"effort": caps.effort}
    assert fake.sent[0]["max_tokens"] == 64000


# --------------------------------------------------------------------------- #
# Non-stream call_anthropic                                                   #
# --------------------------------------------------------------------------- #

def _post_script(cr, monkeypatch, *replies):
    replies = list(replies)
    sent = []

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        sent.append({"json": copy.deepcopy(json), "timeout": timeout})
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        return _FakeResp(reply)

    monkeypatch.setattr(cr.requests, "post", fake_post)
    return sent


def _reply(blocks, stop_reason, **extra):
    return {"content": blocks, "stop_reason": stop_reason,
            "usage": {"input_tokens": 3, "output_tokens": 5}, **extra}


def test_call_anthropic_replays_thinking_blocks_unchanged(cr, monkeypatch):
    first = [{"type": "thinking", "thinking": "plan", "signature": "sig-n1"},
             {"type": "redacted_thinking", "data": "opaque"},
             {"type": "tool_use", "id": "toolu_n1", "name": "web_fetch",
              "input": {"url": "https://example.com/"}}]
    sent = _post_script(cr, monkeypatch, _reply(first, "tool_use"),
                        _reply([{"type": "text", "text": "done"}], "end_turn"))
    text, *_ = cr.call_anthropic(MSGS, MODEL, operator="test-op")

    assert text == "done"
    assert sent[1]["json"]["messages"][-2] == {"role": "assistant", "content": first}


def test_call_anthropic_marks_a_max_tokens_reply_as_truncated(cr, monkeypatch):
    _post_script(cr, monkeypatch, _reply([{"type": "text", "text": "Partial answer"}], "max_tokens"))
    text, *_ = cr.call_anthropic(MSGS, MODEL, operator="test-op")
    assert text.startswith("Partial answer")
    assert "truncated" in text.lower()


def test_call_anthropic_refusal_returns_the_decline_not_the_partial(cr, monkeypatch):
    _post_script(cr, monkeypatch, _reply(
        [{"type": "text", "text": "Sure, the first step is"}], "refusal",
        stop_details={"type": "refusal", "category": "bio", "explanation": "Declined by policy."}))
    text, *_ = cr.call_anthropic(MSGS, MODEL, operator="test-op")
    assert "declined" in text and "bio" in text and "Declined by policy." in text
    assert "first step" not in text


def test_call_anthropic_says_so_after_the_tool_cap(cr, monkeypatch):
    sent = _post_script(cr, monkeypatch, _reply(
        [{"type": "text", "text": "Checking again."},
         {"type": "tool_use", "id": "toolu_n2", "name": "web_fetch",
          "input": {"url": "https://example.com/"}}], "tool_use"))
    text, *_ = cr.call_anthropic(MSGS, MODEL, operator="test-op")
    assert len(sent) == 30
    assert "30 rounds of tool calls" in text


def test_call_anthropic_has_a_connect_read_timeout_long_enough_for_xhigh(cr, monkeypatch):
    sent = _post_script(cr, monkeypatch, _reply([{"type": "text", "text": "ok"}], "end_turn"))
    cr.call_anthropic(MSGS, MODEL, operator="test-op")
    connect, read = sent[0]["timeout"]
    # A non-stream read gets no bytes until the whole message is done; 16K
    # tokens at xhigh effort outran the old flat 200 s (W21).
    assert 0 < connect <= 60
    assert read >= 600
    assert sent[0]["json"]["max_tokens"] == 16000


def _mcp_chat_timeout_default():
    tree = ast.parse(_MCP_SERVER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "getenv"
                and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "BLACKBOX_MCP_CHAT_TIMEOUT"):
            return float(node.args[1].value)
    raise AssertionError("MCP server has no BLACKBOX_MCP_CHAT_TIMEOUT knob")


def test_mcp_chat_poll_outlasts_one_non_stream_call(cr):
    # chat_with_context polls the /chat task; giving up before one Claude call
    # can finish reports a timeout for a reply that then arrives unseen.
    connect, read = cr.ANTHROPIC_NONSTREAM_TIMEOUT
    assert _mcp_chat_timeout_default() >= connect + read
