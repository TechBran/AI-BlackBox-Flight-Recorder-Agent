"""P1.8 — native input/output transcription; Whisper hop = fallback only.

Google's Live API transcribes both sides in-session (inputAudioTranscription /
outputAudioTranscription setup fields; serverContent.inputTranscription /
.outputTranscription objects with a .text field). The post-hoc Whisper
/stt/json hop stays as fallback ONLY — this removes the /stt/json quota
dependency from the voice path (box STT was silently dead on quota Jul 08).
"""
import json
from unittest.mock import AsyncMock

import pytest

import Orchestrator.routes.gemini_live_routes as glr
from Orchestrator.tests.gemini_live_fakes import (
    FakeGeminiWS,
    make_session,
    stub_fossil_context,
)


@pytest.fixture
def no_fossils(monkeypatch):
    stub_fossil_context(monkeypatch)


@pytest.mark.asyncio
async def test_setup_enables_native_transcription(no_fossils):
    session = make_session(gemini_ws=FakeGeminiWS())
    await glr.configure_gemini_session(session, "test_operator", "Orus")
    setup = json.loads(session.gemini_ws.send.await_args.args[0])["setup"]
    assert setup["inputAudioTranscription"] == {}
    assert setup["outputAudioTranscription"] == {}


@pytest.mark.asyncio
async def test_input_transcription_accumulates_and_flushes_on_turn_complete():
    session = make_session()

    await glr.handle_gemini_message(
        session, {"serverContent": {"inputTranscription": {"text": "hello "}}}
    )
    await glr.handle_gemini_message(
        session, {"serverContent": {"inputTranscription": {"text": "world"}}}
    )
    assert session.native_transcription_active is True
    assert session.input_transcript_buffer == "hello world"
    deltas = session.portal_ws.frames("user_transcript_delta")
    assert [d["data"] for d in deltas] == ["hello ", "world"]

    await glr.handle_gemini_message(session, {"serverContent": {"turnComplete": True}})

    user_turns = [m for m in session.conversation if m["role"] == "user"]
    assert [m["content"] for m in user_turns] == ["hello world"]
    finals = session.portal_ws.frames("user_transcript")
    assert [f["data"] for f in finals] == ["hello world"]
    assert session.input_transcript_buffer == ""


@pytest.mark.asyncio
async def test_output_transcription_feeds_assistant_transcript():
    session = make_session()

    await glr.handle_gemini_message(
        session, {"serverContent": {"outputTranscription": {"text": "Sure, "}}}
    )
    await glr.handle_gemini_message(
        session, {"serverContent": {"outputTranscription": {"text": "done."}}}
    )
    deltas = session.portal_ws.frames("transcript_delta")
    assert [d["data"] for d in deltas] == ["Sure, ", "done."]

    await glr.handle_gemini_message(session, {"serverContent": {"turnComplete": True}})

    assistant = [m for m in session.conversation if m["role"] == "assistant"]
    assert [m["content"] for m in assistant] == ["Sure, done."]


@pytest.mark.asyncio
async def test_whisper_skipped_when_native_transcription_active(monkeypatch):
    session = make_session()
    session.native_transcription_active = True
    session.user_audio_buffer = ["QUJD"]
    whisper = AsyncMock(return_value="whisper says hi")
    monkeypatch.setattr(glr, "transcribe_user_audio", whisper)

    await glr.handle_portal_message(session, {"type": "audio_commit"})

    whisper.assert_not_awaited()
    assert session.user_audio_buffer == []


@pytest.mark.asyncio
async def test_whisper_fallback_when_no_native_transcription(monkeypatch):
    session = make_session()
    session.user_audio_buffer = ["QUJD"]
    whisper = AsyncMock(return_value="fallback transcript")
    monkeypatch.setattr(glr, "transcribe_user_audio", whisper)

    await glr.handle_portal_message(session, {"type": "audio_commit"})

    whisper.assert_awaited_once()
    assert [m["content"] for m in session.conversation if m["role"] == "user"] == [
        "fallback transcript"
    ]
