"""One media auto-route for every chat path.

Audit W12 / rank 68: an audio or video attachment switched the turn to Google
(Gemini reads both) only on the non-stream worker (/chat, SMS). Both
/chat/stream paths - the Portal, Android and cron all post there - sent the
part to a provider that cannot read it, so an OpenAI or Anthropic turn with a
recording 400'd or answered without hearing it. chat_routes.media_auto_route
is now the one rule both paths apply: audio/video -> google unless the
provider reads them already or is "custom" (a LAN llama.cpp server has no
audio/video path and a custom-only box has no Google key, so the switch would
fail the turn instead of replying), and a PDF on OpenAI -> google.

The switch also needs a Google key. Onboarding accepts any one LLM key, so a
box set up with only an Anthropic, OpenAI or xAI key has nothing to switch to:
moving the turn there ended it in "GOOGLE_API_KEY not set" where the requested
provider would have answered. Such a turn stays put and its handler turns the
attachment into a note (test_unreadable_media_parts.py).
"""
from __future__ import annotations

import json

import pytest

from Orchestrator import config

AUDIO = {"type": "audio_url", "audio_url": {"url": "/ui/uploads/clip.wav"}}
VIDEO = {"type": "video_url", "video_url": {"url": "/ui/uploads/clip.mp4"}}
PDF = {"type": "document_url", "document_url": {"url": "/ui/uploads/spec.PDF"}}
TXT = {"type": "document_url", "document_url": {"url": "/ui/uploads/notes.txt"}}
IMAGE = {"type": "image_url", "image_url": {"url": "/ui/uploads/cat.png"}}


def _msgs(*parts):
    return [{"role": "assistant", "content": "earlier"},
            {"role": "user", "content": [{"type": "text", "text": "what is this?"}, *parts]}]


@pytest.fixture(autouse=True)
def google_key(monkeypatch):
    """A Google key is configured unless a test removes it: the handlers the
    switch hands a turn to (call_gemini, stream_gemini_with_thinking) check
    chat_routes.GOOGLE_API_KEY, and so does the switch."""
    from Orchestrator.routes import chat_routes as cr
    monkeypatch.setattr(cr, "GOOGLE_API_KEY", "test-google-key")


@pytest.fixture
def no_google_key(monkeypatch):
    from Orchestrator.routes import chat_routes as cr
    monkeypatch.setattr(cr, "GOOGLE_API_KEY", "")


@pytest.mark.parametrize("provider", ["anthropic", "openai", "xai", "computer-use"])
@pytest.mark.parametrize("part", [AUDIO, VIDEO])
def test_audio_and_video_switch_to_google(provider, part):
    from Orchestrator.routes.chat_routes import media_auto_route
    assert media_auto_route(_msgs(part), provider) == ("google", True)


@pytest.mark.parametrize("provider", ["google", "gemini", "custom"])
def test_providers_that_read_media_or_are_custom_stay(provider):
    from Orchestrator.routes.chat_routes import media_auto_route
    assert media_auto_route(_msgs(AUDIO, VIDEO), provider) == (provider, False)


def test_pdf_moves_only_openai():
    from Orchestrator.routes.chat_routes import media_auto_route
    assert media_auto_route(_msgs(PDF), "openai") == ("google", True)
    assert media_auto_route(_msgs(PDF), "anthropic") == ("anthropic", False)
    assert media_auto_route(_msgs(TXT), "openai") == ("openai", False)


@pytest.mark.parametrize("provider, part", [
    ("anthropic", AUDIO), ("anthropic", VIDEO), ("openai", AUDIO), ("xai", VIDEO),
    ("computer-use", AUDIO), ("openai", PDF)])
def test_without_a_google_key_the_turn_stays(no_google_key, provider, part):
    from Orchestrator.routes.chat_routes import media_auto_route
    assert media_auto_route(_msgs(part), provider) == (provider, False)


def test_text_and_images_never_switch():
    from Orchestrator.routes.chat_routes import media_auto_route
    assert media_auto_route(_msgs(IMAGE), "anthropic") == ("anthropic", False)
    assert media_auto_route([{"role": "user", "content": "hi"}], "openai") == ("openai", False)
    assert media_auto_route([], "xai") == ("xai", False)
    # Malformed parts from a client never raise.
    junk = [{"role": "user", "content": ["text", None, {"type": "document_url"}]}]
    assert media_auto_route(junk, "openai") == ("openai", False)


# -- /chat/stream ---------------------------------------------------------------

@pytest.fixture
def stream_fakes(monkeypatch):
    from Orchestrator.routes import chat_routes as cr
    seen = {}

    def fake_bsc(messages, operator, provider="openai", window_guard_tokens=None):
        seen["context_provider"] = provider
        return list(messages), {"recent": [], "keyword": [], "semantic": [], "checkpoint": []}, {}

    def recorder(name):
        async def fake(messages, model, operator=None, **kw):
            seen["stream"], seen["model"] = name, model
            yield {"type": "content", "data": "ok"}
        return fake

    monkeypatch.setattr(cr, "build_streaming_context", fake_bsc)
    for attr, name in [("stream_anthropic_with_thinking", "anthropic"),
                       ("stream_gemini_with_thinking", "gemini"),
                       ("stream_openai_with_reasoning", "openai"),
                       ("stream_xai_with_reasoning", "xai"),
                       ("stream_custom_with_reasoning", "custom"),
                       ("stream_computer_use", "computer-use"),
                       ("stream_gemini_computer_use", "computer-use"),
                       ("stream_openai_computer_use", "computer-use")]:
        monkeypatch.setattr(cr, attr, recorder(name))
    return seen


def _stream_start(text):
    for block in text.split("\n\n"):
        if block.startswith("event: stream_start"):
            return json.loads(block.split("data: ", 1)[1])
    raise AssertionError(f"no stream_start in {text[:300]!r}")


@pytest.mark.parametrize("provider", ["anthropic", "openai", "xai"])
def test_stream_routes_audio_to_gemini_with_its_default_model(stream_fakes, app_client, provider):
    r = app_client.post("/chat/stream", json={
        "messages": _msgs(AUDIO), "provider": provider, "model": "some-model-of-that-provider",
        "operator": "TestOp"})
    assert r.status_code == 200
    start = _stream_start(r.text)
    assert start["provider"] == "google"
    assert start["model"] == config.resolve_default_model("google", None)
    assert stream_fakes["stream"] == "gemini"
    # The context (and its window guard) is built for the provider that runs.
    assert stream_fakes["context_provider"] == "google"


@pytest.mark.parametrize("provider", ["anthropic", "openai", "xai"])
def test_stream_without_a_google_key_answers_on_the_requested_provider(
        stream_fakes, app_client, no_google_key, provider):
    """Before, this turn moved to Gemini and ended in 'GOOGLE_API_KEY not set'."""
    r = app_client.post("/chat/stream", json={
        "messages": _msgs(AUDIO), "provider": provider, "model": "", "operator": "TestOp"})
    assert r.status_code == 200
    start = _stream_start(r.text)
    assert start["provider"] == provider
    assert start["model"] == config.resolve_default_model(provider, None)
    assert stream_fakes["stream"] == provider
    assert stream_fakes["context_provider"] == provider
    assert "GOOGLE_API_KEY not set" not in r.text
    assert 'data: "ok"' in r.text


def test_stream_keeps_custom_with_audio(stream_fakes, app_client):
    r = app_client.post("/chat/stream", json={
        "messages": _msgs(AUDIO), "provider": "custom", "model": "", "operator": "TestOp"})
    assert r.status_code == 200
    assert _stream_start(r.text)["provider"] == "custom"
    assert stream_fakes["stream"] == "custom"


def test_stream_keeps_a_computer_use_session_with_video(stream_fakes, app_client):
    """A CU turn drives a desktop session; it is not re-routed to a chat model."""
    r = app_client.post("/chat/stream", json={
        "messages": _msgs(VIDEO), "provider": "computer-use", "operator": "TestOp"})
    assert r.status_code == 200
    assert _stream_start(r.text)["provider"] == "computer-use"
    assert stream_fakes["stream"] == "computer-use"


def test_stream_without_media_is_unchanged(stream_fakes, app_client):
    r = app_client.post("/chat/stream", json={
        "messages": _msgs(IMAGE), "provider": "anthropic", "model": "", "operator": "TestOp"})
    assert _stream_start(r.text)["provider"] == "anthropic"
    assert stream_fakes["stream"] == "anthropic"
    assert stream_fakes["model"] == config.ANTHROPIC_MODEL_DEFAULT


def test_both_paths_use_the_one_helper():
    """No second copy of the detection: tasks.py and the stream route call it."""
    import inspect
    import Orchestrator.tasks as tasks
    from Orchestrator.routes import chat_routes as cr
    worker = inspect.getsource(tasks.process_chat_task)
    stream = inspect.getsource(cr._resolve_stream_request)
    assert "media_auto_route(" in worker and "media_auto_route(" in stream
    assert '== "audio_url"' not in worker and "has_audio" not in worker
