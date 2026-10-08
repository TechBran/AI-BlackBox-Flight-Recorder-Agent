"""Anthropic capability sets must track new model families (model-rot guard).

2026-06-11: claude-fable-5 returned 400 "`temperature` is deprecated for this
model" in chat because ANTHROPIC_NO_SAMPLING_MODELS predated the Claude 5
tier. 2026-10-07: the same 400 hit the whole 5.x tier (Opus 5.5, Sonnet 5.5,
Haiku 5.5, Fable 5.1, Opus 5, Sonnet 5) for the same reason — the Portal lists
models live from /v1/models, so a deny-list rots on every release. Sampling is
now an allow-list of legacy families (anthropic_accepts_sampling); unknown
models get no temperature/top_p/top_k. The thinking sets still need extending
when Anthropic ships a new family — a miss there only hides thinking text, it
no longer 400s.
"""
import pytest

from Orchestrator.config import (
    ANTHROPIC_EFFORT_MAP,
    ANTHROPIC_THINKING_DISPLAY_MODELS,
    ANTHROPIC_THINKING_MODELS,
    anthropic_accepts_sampling,
)

# Verified live against /v1/messages on 2026-10-07: each of these returns 400
# "`temperature` is deprecated for this model" for temperature=0.7, and the
# same for top_p / top_k.
REJECTS_SAMPLING = (
    "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5",
    "claude-fable-5-1", "claude-mythos-5-1",
    "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-mythos-5",
    "claude-opus-4-8", "claude-opus-4-7",
)
# Verified live on the same date: these accept temperature/top_p/top_k.
ACCEPTS_SAMPLING = (
    "claude-opus-4-6", "claude-sonnet-4-6",
    "claude-opus-4-5-20251101", "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5-20251001", "claude-sonnet-4-5",
)


@pytest.mark.parametrize("model", REJECTS_SAMPLING)
def test_models_that_reject_sampling_never_get_it(model):
    assert not anthropic_accepts_sampling(model), (
        f"{model} rejects sampling params — chat would 400 on temperature")


@pytest.mark.parametrize("model", ACCEPTS_SAMPLING)
def test_legacy_models_keep_sampling(model):
    assert anthropic_accepts_sampling(model)


@pytest.mark.parametrize("model", (
    "claude-opus-6", "claude-sonnet-6-5", "claude-haiku-6", "claude-fable-7",
    "claude-something-new", "", None,
))
def test_unknown_models_fail_closed(model):
    # The point of the allow-list: a model released after this file was written
    # must not 400 on its first chat message.
    assert not anthropic_accepts_sampling(model)


def test_claude5_tier_in_every_thinking_set():
    for model in (
        "claude-fable-5-1", "claude-mythos-5-1", "claude-fable-5", "claude-mythos-5",
        "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-sonnet-5",
    ):
        assert model in ANTHROPIC_THINKING_MODELS
        assert model in ANTHROPIC_THINKING_DISPLAY_MODELS
        assert model in ANTHROPIC_EFFORT_MAP
    # Haiku 5.5 thinks and needs display="summarized", but runs at the API's
    # default effort on purpose.
    assert "claude-haiku-5-5" in ANTHROPIC_THINKING_MODELS
    assert "claude-haiku-5-5" in ANTHROPIC_THINKING_DISPLAY_MODELS


def test_display_gated_models_also_reject_sampling():
    # The display="omitted"-by-default surface (4.7+) is the same surface that
    # removed temperature/top_p/top_k — these must move together.
    for model in ANTHROPIC_THINKING_DISPLAY_MODELS:
        assert not anthropic_accepts_sampling(model), model


# --------------------------------------------------------------------------- #
# Payload tests: what call_anthropic / stream_anthropic_with_thinking SEND      #
# --------------------------------------------------------------------------- #
# 2026-10-07 (verified live): Opus 4.6 / Sonnet 4.6 accept temperature, but not
# alongside thinking — 400 "`temperature` may only be set to 1 when thinking is
# enabled or in adaptive mode". The chat paths enable adaptive thinking on both,
# so temperature may only go out when thinking does not.

SAMPLING_KEYS = ("temperature", "top_p", "top_k")
PAYLOAD_MODELS = REJECTS_SAMPLING + ACCEPTS_SAMPLING + ("claude-opus-6",)


def _assert_payload_ok(model, payload):
    sent = [k for k in SAMPLING_KEYS if k in payload]
    if "thinking" in payload or not anthropic_accepts_sampling(model):
        assert not sent, f"{model}: sent {sent} with thinking={payload.get('thinking')}"
    else:
        assert sent == ["temperature"], f"{model}: thinking-off legacy model lost temperature"
    if model in ANTHROPIC_THINKING_MODELS:
        assert payload["thinking"]["type"] == "adaptive"


@pytest.fixture
def _cr(monkeypatch):
    import Orchestrator.routes.chat_routes as cr

    monkeypatch.setattr(cr, "ANTHROPIC_API_KEY", "test-anthropic", raising=False)
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(cr, "read_text_safe", lambda *a, **k: "")
    return cr


@pytest.mark.parametrize("model", PAYLOAD_MODELS)
def test_call_anthropic_payload(monkeypatch, _cr, model):
    captured = []

    class _Resp:
        status_code = 200
        text = "fake"

        def json(self):
            return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1}}

    def fake_post(*a, json=None, **k):
        captured.append(json)
        return _Resp()

    monkeypatch.setattr(_cr.requests, "post", fake_post)
    _cr.call_anthropic([{"role": "user", "content": "hi"}], model, operator="test-op")
    assert captured, "call_anthropic never posted"
    _assert_payload_ok(model, captured[0])


@pytest.mark.parametrize("model", PAYLOAD_MODELS)
def test_stream_anthropic_payload(monkeypatch, _cr, model):
    import asyncio

    captured = []

    class _Resp:
        status_code = 400  # stop the generator right after the request goes out

        async def aread(self):
            return b"fake"

    class _StreamCtx:
        async def __aenter__(self):
            return _Resp()

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None, json=None):
            captured.append(json)
            return _StreamCtx()

    monkeypatch.setattr(_cr.httpx, "AsyncClient", _Client)

    async def drain():
        return [ev async for ev in _cr.stream_anthropic_with_thinking(
            [{"role": "user", "content": "hi"}], model, "test-op")]

    asyncio.run(drain())
    assert captured, "stream_anthropic_with_thinking never posted"
    _assert_payload_ok(model, captured[0])
