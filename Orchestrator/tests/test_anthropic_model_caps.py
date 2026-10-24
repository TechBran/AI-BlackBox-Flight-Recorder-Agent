"""Anthropic capabilities must track new model releases (model-rot guard).

2026-06-11: claude-fable-5 returned 400 "`temperature` is deprecated for this
model" in chat because ANTHROPIC_NO_SAMPLING_MODELS predated the Claude 5
tier. 2026-10-07: the same 400 hit the whole 5.x tier (Opus 5.5, Sonnet 5.5,
Haiku 5.5, Fable 5.1, Opus 5, Sonnet 5) for the same reason — the Portal lists
models live from /v1/models, so a deny-list rots on every release. Sampling is
now an allow-list of legacy families (anthropic_accepts_sampling); unknown
models get no temperature/top_p/top_k.

The thinking/effort/display tables rotted the same way (O01-core-runtime-07):
an id missing from them ran with no thinking config and no effort pin. They
are now one parser, anthropic_caps(model), that reads family and version off
the id, so a release nobody has seen yet (claude-opus-6) still thinks.
"""
import pytest

from Orchestrator.config import anthropic_accepts_sampling, anthropic_caps

SUMMARIZED = {"type": "adaptive", "display": "summarized"}
ADAPTIVE = {"type": "adaptive"}

# What every known id must get: the exact behaviour of the old tables.
KNOWN_CAPS = (
    ("claude-fable-5-1", SUMMARIZED, "xhigh"),
    ("claude-mythos-5-1", SUMMARIZED, "xhigh"),
    ("claude-fable-5", SUMMARIZED, "xhigh"),
    ("claude-mythos-5", SUMMARIZED, "xhigh"),
    ("claude-opus-5-5", SUMMARIZED, "xhigh"),   # API default is only "medium"
    ("claude-opus-5", SUMMARIZED, "xhigh"),
    ("claude-sonnet-5-5", SUMMARIZED, "high"),
    ("claude-sonnet-5", SUMMARIZED, "high"),
    ("claude-haiku-5-5", SUMMARIZED, None),     # cheap tier keeps the API default
    ("claude-opus-4-8", SUMMARIZED, "xhigh"),
    ("claude-opus-4-7", SUMMARIZED, "xhigh"),
    ("claude-opus-4-6", ADAPTIVE, "high"),      # xhigh arrived with Opus 4.7
    ("claude-sonnet-4-6", ADAPTIVE, "high"),
    # Legacy budget_tokens models: no thinking, as before.
    ("claude-haiku-4-5", None, None),
    ("claude-haiku-4-5-20251001", None, None),
    ("claude-sonnet-4-5-20250929", None, None),
    ("claude-opus-4-5-20251101", None, None),
    ("claude-opus-4-1", None, None),
    ("claude-opus-4-20250514", None, None),
)

# Releases nobody had seen when this was written, plus dated / aliased spellings.
FUTURE_CAPS = (
    ("claude-opus-6", SUMMARIZED, "xhigh"),
    ("claude-opus-5-7", SUMMARIZED, "xhigh"),
    ("claude-sonnet-6-5", SUMMARIZED, "high"),
    ("claude-haiku-6", SUMMARIZED, None),
    ("claude-fable-7", SUMMARIZED, "xhigh"),
    ("claude-opus-4-9", SUMMARIZED, "xhigh"),
    ("claude-opus-4-6-20260301", ADAPTIVE, "high"),
    ("claude-sonnet-5-5-20261101", SUMMARIZED, "high"),
    ("claude-opus-5-5-latest", SUMMARIZED, "xhigh"),
    ("  Claude-Opus-5-5 ", SUMMARIZED, "xhigh"),
    ("claude-newfamily-6", SUMMARIZED, None),    # thinks; effort left to the API
)


@pytest.mark.parametrize("model, thinking, effort", KNOWN_CAPS + FUTURE_CAPS)
def test_anthropic_caps(model, thinking, effort):
    caps = anthropic_caps(model)
    assert caps.thinking == thinking, model
    assert caps.effort == effort, model


@pytest.mark.parametrize("model", (
    "claude-something-new", "claude-3-7-sonnet-20250219", "claude-instant-1",
    "gpt-5.1", "", None,
))
def test_unparseable_ids_send_no_thinking_config(model):
    caps = anthropic_caps(model)
    assert caps.thinking is None and caps.effort is None


def test_caps_never_share_their_thinking_dict():
    # A caller adds to payload["thinking"]; that must not leak into the next call.
    first = anthropic_caps("claude-opus-5-5")
    first.thinking["budget_tokens"] = 1
    assert "budget_tokens" not in anthropic_caps("claude-opus-5-5").thinking


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


def test_display_gated_models_also_reject_sampling():
    # The display="omitted"-by-default surface (4.7+) is the same surface that
    # removed temperature/top_p/top_k — these must move together.
    for model, thinking, _ in KNOWN_CAPS + FUTURE_CAPS:
        if thinking and "display" in thinking:
            assert not anthropic_accepts_sampling(model), model


# --------------------------------------------------------------------------- #
# Payload tests: what call_anthropic / stream_anthropic_with_thinking SEND      #
# --------------------------------------------------------------------------- #
# 2026-10-07 (verified live): Opus 4.6 / Sonnet 4.6 accept temperature, but not
# alongside thinking — 400 "`temperature` may only be set to 1 when thinking is
# enabled or in adaptive mode". The chat paths enable adaptive thinking on both,
# so temperature may only go out when thinking does not.

SAMPLING_KEYS = ("temperature", "top_p", "top_k")
PAYLOAD_MODELS = REJECTS_SAMPLING + ACCEPTS_SAMPLING + ("claude-opus-6", "claude-sonnet-6-5")


def _assert_payload_ok(model, payload):
    sent = [k for k in SAMPLING_KEYS if k in payload]
    if "thinking" in payload or not anthropic_accepts_sampling(model):
        assert not sent, f"{model}: sent {sent} with thinking={payload.get('thinking')}"
    else:
        assert sent == ["temperature"], f"{model}: thinking-off legacy model lost temperature"
    # Both paths send exactly what anthropic_caps decides.
    caps = anthropic_caps(model)
    assert payload.get("thinking") == caps.thinking, model
    assert payload.get("output_config", {}).get("effort") == caps.effort, model


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
