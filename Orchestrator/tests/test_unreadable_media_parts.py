"""Audio, video and document parts never reach a provider that cannot read them.

The media auto-route (chat_routes.media_auto_route) moves an audio or video
turn to Google, except on provider "custom" (a LAN llama.cpp server) and,
since a box onboarded with one non-Google key has nothing to switch to, on a
box with no Google key. Those turns stay where they are, so every handler
that can receive them has to say what was attached instead of:

  * forwarding the raw part (stream_custom_with_reasoning,
    stream_xai_with_reasoning, stream_openai_with_reasoning and the call_openai
    / call_xai workers did: llama.cpp 500s on an audio_url part, OpenAI and
    xAI 400 on part types they do not know), or
  * dropping it silently (stream_anthropic_with_thinking kept only image and
    text parts, call_anthropic dropped audio), so the model answered as if
    nothing had been sent.

Android now tags attachments by type (audit W13), so every recorded take is an
audio_url part and every PDF or text file a document_url part; the Portal has
always sent those shapes.
"""
from __future__ import annotations

import asyncio
import base64
import os

import pytest

from Orchestrator.utils import paths

PDF_BYTES = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
RAW_TYPES = {"audio_url", "video_url", "document_url"}


@pytest.fixture
def uploads(tmp_path):
    """A box whose Portal/uploads holds a recording, a clip and documents."""
    root = tmp_path / "box"
    up = root / "Portal" / "uploads"
    up.mkdir(parents=True)
    (up / "take.wav").write_bytes(b"RIFF" + b"\x00" * 40)
    (up / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    (up / "notes.txt").write_bytes("plain notes déjà vu".encode("utf-8"))
    (up / "spec.pdf").write_bytes(PDF_BYTES)
    (up / "brief.docx").write_bytes(b"PK\x03\x04docx")
    (up / "bundle.zip").write_bytes(b"PK\x03\x04zip")
    old = os.environ.get("BLACKBOX_ROOT")
    os.environ["BLACKBOX_ROOT"] = str(root)
    paths.blackbox_root.cache_clear()
    try:
        yield up
    finally:
        if old is None:
            os.environ.pop("BLACKBOX_ROOT", None)
        else:
            os.environ["BLACKBOX_ROOT"] = old
        paths.blackbox_root.cache_clear()


def _part(kind, name):
    return {"type": kind, kind: {"url": f"/ui/uploads/{name}"}}


AUDIO = _part("audio_url", "take.wav")
VIDEO = _part("video_url", "clip.mp4")
TXT = _part("document_url", "notes.txt")
PDF = _part("document_url", "spec.pdf")
DOCX = _part("document_url", "brief.docx")
ZIP = _part("document_url", "bundle.zip")
MISSING = _part("document_url", "gone.pdf")
ALL_PARTS = [AUDIO, VIDEO, TXT, PDF, DOCX, ZIP, MISSING]


def _turn(*parts):
    return [{"role": "system", "content": "system prompt"},
            {"role": "user", "content": [{"type": "text", "text": "what is this?"}, *parts]}]


def _texts(content):
    return "\n".join(p.get("text", "") for p in content if p.get("type") == "text")


# -- the async streams: capture the upstream payload ----------------------------

class _Captured(Exception):
    pass


def _capturing_client(store):
    class FakeStream:
        def __init__(self, payload):
            store["payload"] = payload

        async def __aenter__(self):
            raise _Captured()

        async def __aexit__(self, *a):
            return False

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None, json=None, **kw):
            return FakeStream(json)
    return FakeClient


def _drain(agen):
    async def run():
        try:
            async for _ in agen:
                pass
        except _Captured:
            pass
    asyncio.run(run())


@pytest.fixture
def stream_capture(monkeypatch):
    from Orchestrator.routes import chat_routes as cr
    store = {}
    monkeypatch.setattr(cr.httpx, "AsyncClient", _capturing_client(store))
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(cr, "OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(cr, "XAI_API_KEY", "test-xai-key")
    monkeypatch.setattr(cr, "ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setattr(cr.custom_servers, "resolve_model", lambda m: (
        {"alias": "box", "base_url": "http://127.0.0.1:9/v1", "last_models": ["m"]}, "m"))
    return store


def _stream(name, messages):
    from Orchestrator.routes import chat_routes as cr
    fn, model = {
        "custom": (cr.stream_custom_with_reasoning, "box::m"),
        "xai": (cr.stream_xai_with_reasoning, "grok-4"),
        "openai": (cr.stream_openai_with_reasoning, "gpt-5.5"),
        "anthropic": (cr.stream_anthropic_with_thinking, "claude-opus-4-7"),
    }[name]
    _drain(fn(messages, model, operator="TestOp"))


def _user_content(payload):
    return [m for m in payload["messages"] if m.get("role") == "user"][-1]["content"]


@pytest.mark.parametrize("provider", ["custom", "xai", "openai", "anthropic"])
@pytest.mark.parametrize("part", ALL_PARTS, ids=lambda p: p[p["type"]]["url"].rsplit("/", 1)[1])
def test_streams_never_forward_or_drop_a_media_part(uploads, stream_capture, provider, part):
    _stream(provider, _turn(part))
    content = _user_content(stream_capture["payload"])
    assert not RAW_TYPES & {p.get("type") for p in content}, content
    # The model is told something about the attachment: a note, its text, or
    # (Claude, a PDF) the document itself.
    extra = [p for p in content if p.get("text") != "what is this?"]
    assert extra, f"{provider} dropped {part} without a trace"


@pytest.mark.parametrize("provider", ["custom", "xai", "openai", "anthropic"])
def test_streams_say_what_was_attached(uploads, stream_capture, provider):
    _stream(provider, _turn(AUDIO, VIDEO, TXT, DOCX, ZIP))
    text = _texts(_user_content(stream_capture["payload"]))
    assert "Audio file attached: /ui/uploads/take.wav" in text
    assert "Video file attached: /ui/uploads/clip.mp4" in text
    assert "[Document content: notes.txt]\n\nplain notes déjà vu" in text
    assert "brief.docx" in text
    # An upload this loader refuses for its type keeps the refusal's reason.
    assert "an attached file was not loaded" in text and ".zip" in text


def test_anthropic_stream_sends_a_pdf_as_a_document_block(uploads, stream_capture):
    _stream("anthropic", _turn(PDF))
    docs = [p for p in _user_content(stream_capture["payload"]) if p.get("type") == "document"]
    assert docs == [{"type": "document", "source": {
        "type": "base64", "media_type": "application/pdf",
        "data": base64.b64encode(PDF_BYTES).decode()}}]


@pytest.mark.parametrize("provider", ["custom", "xai", "openai"])
def test_other_streams_note_a_pdf_instead(uploads, stream_capture, provider):
    _stream(provider, _turn(PDF))
    text = _texts(_user_content(stream_capture["payload"]))
    assert "spec.pdf" in text


@pytest.mark.parametrize("provider", ["custom", "xai", "openai"])
def test_image_and_text_parts_are_unchanged(uploads, stream_capture, provider):
    data_url = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
    image = {"type": "image_url", "image_url": {"url": data_url}}
    _stream(provider, _turn(image))
    assert _user_content(stream_capture["payload"]) == [
        {"type": "text", "text": "what is this?"}, image]


# -- the non-stream workers (/chat, cron, SMS) ----------------------------------

@pytest.fixture
def post_capture(monkeypatch):
    from Orchestrator.routes import chat_routes as cr
    store = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        store["payload"] = json
        raise _Captured()

    monkeypatch.setattr(cr.requests, "post", fake_post)
    monkeypatch.setattr(cr, "_get_tools", lambda *a, **k: [])
    monkeypatch.setattr(cr, "OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(cr, "XAI_API_KEY", "test-xai-key")
    monkeypatch.setattr(cr, "ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setattr(cr.custom_servers, "resolve_model", lambda m: (
        {"alias": "box", "base_url": "http://127.0.0.1:9/v1", "last_models": ["m"]}, "m"))
    return store


@pytest.mark.parametrize("provider", ["openai", "xai", "anthropic", "custom"])
def test_workers_note_an_audio_part(uploads, post_capture, provider):
    """call_custom already turned audio into a note; the other three forwarded
    it raw (OpenAI, xAI) or dropped it (Anthropic). They now receive audio on a
    box with no Google key, where media_auto_route keeps the turn."""
    from Orchestrator.routes import chat_routes as cr
    fn, model = {"openai": (cr.call_openai, "gpt-5.5"), "xai": (cr.call_xai, "grok-4"),
                 "anthropic": (cr.call_anthropic, "claude-opus-4-7"),
                 "custom": (cr.call_custom, "box::m")}[provider]
    with pytest.raises(_Captured):
        fn(_turn(AUDIO), model, operator="TestOp")
    content = _user_content(post_capture["payload"])
    assert "audio_url" not in {p.get("type") for p in content}
    assert "Audio file attached: /ui/uploads/take.wav" in _texts(content)


def test_the_helper_leaves_other_parts_to_the_caller():
    from Orchestrator.routes.chat_routes import _unreadable_attachment_parts
    assert _unreadable_attachment_parts({"type": "text", "text": "x"}) is None
    assert _unreadable_attachment_parts({"type": "image_url", "image_url": {"url": "/x.png"}}) is None
    assert _unreadable_attachment_parts("not a part") is None
    # A malformed media part still yields a note, never an exception.
    assert _unreadable_attachment_parts({"type": "audio_url"})[0]["type"] == "text"
    assert _unreadable_attachment_parts({"type": "document_url", "document_url": None})[0]["type"] == "text"
