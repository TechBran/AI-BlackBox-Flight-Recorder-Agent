"""gpt-5.6 family reasoning_effort compat (live-reproduced 2026-07-09).

OpenAI's gpt-5.6 family (gpt-5.6-sol/-terra/-luna) applies a non-none
server-side reasoning_effort DEFAULT and rejects every /v1/chat/completions
request carrying function tools (the BlackBox always attaches ToolVault
tools) with:

    400 {"error": {"message": "Function tools with reasoning_effort are not
    supported for gpt-5.6-sol in /v1/chat/completions. To use function
    tools, use /v1/responses or set reasoning_effort to 'none'.",
    "type": "invalid_request_error", "param": "reasoning_effort", ...}}

Fix (both OpenAI functions in chat_routes, per the error's own contract):
  1. Proactive gate — "gpt-5.6" in the model id -> the payload carries an
     explicit reasoning_effort="none" from the first request.
  2. Reactive one-time retry (the stream_options compat idiom from
     stream_custom_with_reasoning) — a 400 whose body mentions
     reasoning_effort, when we sent none, retries ONCE with "none". This
     self-heals FUTURE model families that ship the same server-side
     default before the proactive gate learns their name.

Seam: monkeypatched requests.post on call_openai (the
test_custom_provider_wiring.py fake-response idiom). The streaming twin
(stream_openai_with_reasoning) has no unit harness (established) — it is
live-verified against the real gpt-5.6-sol.
"""
import copy
import json

import pytest
from fastapi import HTTPException


REPLY = "The Orchestrator listens on port 9091."


class _FakeResp:
    """Minimal stand-in for a requests.Response (test_custom_provider_wiring
    pattern). .text is cached like requests' — safe to read repeatedly."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _ok_body():
    return {
        "choices": [{
            "message": {"role": "assistant", "content": REPLY},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


def _reasoning_effort_400_body(model):
    """The EXACT live 400 (model name parameterized)."""
    return {"error": {
        "message": (
            f"Function tools with reasoning_effort are not supported for "
            f"{model} in /v1/chat/completions. To use function tools, use "
            f"/v1/responses or set reasoning_effort to 'none'."
        ),
        "type": "invalid_request_error",
        "param": "reasoning_effort",
        "code": None,
    }}


@pytest.fixture
def openai_env(monkeypatch):
    """chat_routes with a key set, _get_tools stubbed (function tools are
    always attached live — an empty list keeps the payload shape without the
    ToolVault machinery), and a scriptable recording requests.post."""
    from Orchestrator.routes import chat_routes as cr

    monkeypatch.setattr("Orchestrator.routes.chat_routes.OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])

    recorded = {"payloads": [], "responses": []}

    def fake_post(url, headers=None, json=None, timeout=None):
        # Deep-copy: call_openai mutates ONE payload dict across retries, so
        # a captured reference would show later mutations retroactively.
        recorded["payloads"].append(copy.deepcopy(json))
        return recorded["responses"].pop(0)

    monkeypatch.setattr(cr.requests, "post", fake_post)
    return cr, recorded


_MSGS = [{"role": "user", "content": "hi"}]


# --- 1. proactive gate -----------------------------------------------------------

def test_gpt56_payload_proactively_sends_reasoning_effort_none(openai_env):
    """gpt-5.6-sol never even makes the doomed first request: the payload
    carries reasoning_effort='none' from post #1."""
    cr, recorded = openai_env
    recorded["responses"] = [_FakeResp(_ok_body())]

    text, usage = cr.call_openai(_MSGS, "gpt-5.6-sol", operator="TestOp")

    assert len(recorded["payloads"]) == 1
    assert recorded["payloads"][0]["reasoning_effort"] == "none"
    assert text == REPLY
    assert usage["total_tokens"] == 8


# --- 2. reactive one-time retry (self-healing for future families) ----------------

def test_future_family_400_retries_once_with_none(openai_env):
    """A hypothetical gpt-7-nova (no proactive gate) hitting the same
    server-side default: first post 400s mentioning reasoning_effort, the
    retry sends 'none', the reply comes back."""
    cr, recorded = openai_env
    recorded["responses"] = [
        _FakeResp(_reasoning_effort_400_body("gpt-7-nova"), status_code=400),
        _FakeResp(_ok_body()),
    ]

    text, usage = cr.call_openai(_MSGS, "gpt-7-nova", operator="TestOp")

    assert len(recorded["payloads"]) == 2
    assert "reasoning_effort" not in recorded["payloads"][0]  # first attempt untouched
    assert recorded["payloads"][1]["reasoning_effort"] == "none"
    assert text == REPLY


def test_retry_happens_at_most_once(openai_env):
    """Both posts 400: the second failure surfaces the RAW error — the flag
    guarantees no retry loop."""
    cr, recorded = openai_env
    body = _reasoning_effort_400_body("gpt-7-nova")
    recorded["responses"] = [
        _FakeResp(body, status_code=400),
        _FakeResp(body, status_code=400),
    ]

    with pytest.raises(HTTPException) as ei:
        cr.call_openai(_MSGS, "gpt-7-nova", operator="TestOp")

    assert len(recorded["payloads"]) == 2  # exactly one retry, then raise
    assert ei.value.status_code == 400
    assert "reasoning_effort" in ei.value.detail  # raw body, not swallowed


def test_non_reasoning_400_keeps_raw_error_no_retry(openai_env):
    """Any other 400 (e.g. a context error) is byte-identical to today:
    single post, raw HTTPException, no retry."""
    cr, recorded = openai_env
    other = {"error": {"message": "This model's maximum context length is 128000 tokens.",
                       "type": "invalid_request_error", "param": "messages", "code": None}}
    recorded["responses"] = [_FakeResp(other, status_code=400)]

    with pytest.raises(HTTPException) as ei:
        cr.call_openai(_MSGS, "gpt-5.1", operator="TestOp")

    assert len(recorded["payloads"]) == 1  # no retry
    assert ei.value.status_code == 400
    assert "maximum context length" in ei.value.detail


# --- 3. regression pin: non-5.6 models unchanged ----------------------------------

def test_gpt51_payload_has_no_reasoning_effort(openai_env):
    """gpt-5.1 (and every non-5.6 model) sends NO reasoning_effort key —
    the gate must not leak onto models that accept the default."""
    cr, recorded = openai_env
    recorded["responses"] = [_FakeResp(_ok_body())]

    text, _ = cr.call_openai(_MSGS, "gpt-5.1", operator="TestOp")

    assert len(recorded["payloads"]) == 1
    assert "reasoning_effort" not in recorded["payloads"][0]
    assert text == REPLY
