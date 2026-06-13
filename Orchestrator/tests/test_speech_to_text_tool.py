"""Hermetic tests for the speech_to_text ToolVault executor (Tasks 13 + 14).

The two HTTP hops (/stt transcribe, /chat/save mint) are factored behind tiny
module-level helpers (_post_stt, _post_chat_save). We monkeypatch those so the
executor's external behavior is exercised without any network / aiohttp.

Coverage:
  * provider + diarize form fields reach /stt (captured via the helper args)
  * diarized response -> message has speaker count + diarized_text; data carries segments
  * mint=true -> a /chat/save POST happens with diarized_text as assistant_response,
    and the success message gains the snap_id
  * mint save-failure -> the tool STILL succeeds (note suffix, not failure)
  * the flat (non-diarized) path stays byte-for-byte compatible
"""
import asyncio
import importlib.util
from pathlib import Path

import pytest

from Orchestrator.toolvault.context import ToolContext, ToolResult

# Load the executor module directly from its file (same mechanism the registry uses).
_EXEC_PATH = (
    Path(__file__).resolve().parents[2]
    / "ToolVault" / "tools" / "speech_to_text" / "executor.py"
)
_spec = importlib.util.spec_from_file_location("stt_tool_executor", _EXEC_PATH)
stt_exec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stt_exec)


_DIARIZED_RESPONSE = {
    "text": "Hello there. General Kenobi.",
    "provider": "elevenlabs",
    "segments": [
        {"speaker": "speaker_0", "start": 0.0, "end": 1.5, "text": "Hello there."},
        {"speaker": "speaker_1", "start": 1.6, "end": 3.0, "text": "General Kenobi."},
    ],
    "speakers": ["speaker_0", "speaker_1"],
    "diarized_text": "[00:00] Speaker 1: Hello there.\n[00:02] Speaker 2: General Kenobi.",
    "events": [],
    "language": "eng",
}


@pytest.fixture
def audio_file(tmp_path):
    """A real (empty) file on disk so the executor's exists() check passes."""
    p = tmp_path / "meeting.wav"
    p.write_bytes(b"RIFFfake")
    return p


def _run(coro):
    return asyncio.run(coro)


def _patch_stt(monkeypatch, response, captured):
    async def fake_post_stt(base_url, audio_file, content_type, provider, diarize):
        captured["stt"] = {
            "base_url": base_url,
            "filename": audio_file.name,
            "content_type": content_type,
            "provider": provider,
            "diarize": diarize,
        }
        return response
    monkeypatch.setattr(stt_exec, "_post_stt", fake_post_stt)


def _patch_chat_save(monkeypatch, response, captured, raise_exc=None):
    async def fake_post_chat_save(base_url, payload):
        captured["chat_save"] = {"base_url": base_url, "payload": payload}
        if raise_exc is not None:
            raise raise_exc
        return response
    monkeypatch.setattr(stt_exec, "_post_chat_save", fake_post_chat_save)


# --------------------------------------------------------------------------- #
# Task 13 — diarization-aware tool
# --------------------------------------------------------------------------- #
def test_provider_and_diarize_form_fields_sent(monkeypatch, audio_file):
    """provider + diarize reach the /stt helper exactly as passed."""
    captured = {}
    _patch_stt(monkeypatch, _DIARIZED_RESPONSE, captured)

    ctx = ToolContext(operator="system")
    result = _run(stt_exec.execute(
        {"audio_path": str(audio_file), "provider": "elevenlabs", "diarize": True}, ctx
    ))

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert captured["stt"]["provider"] == "elevenlabs"
    assert captured["stt"]["diarize"] is True
    assert captured["stt"]["content_type"] == "audio/wav"


def test_diarized_response_message_and_data(monkeypatch, audio_file):
    """Diarized response -> message has speaker count + diarized_text; data is the rich dict."""
    captured = {}
    _patch_stt(monkeypatch, _DIARIZED_RESPONSE, captured)

    ctx = ToolContext(operator="system")
    result = _run(stt_exec.execute(
        {"audio_path": str(audio_file), "provider": "elevenlabs", "diarize": True}, ctx
    ))

    assert "2 speakers" in result.result
    assert "Speaker 1: Hello there." in result.result
    assert "Speaker 2: General Kenobi." in result.result
    # FULL rich dict attached so callers get segments/speakers programmatically.
    assert result.data["segments"] == _DIARIZED_RESPONSE["segments"]
    assert result.data["speakers"] == ["speaker_0", "speaker_1"]


def test_diarized_falls_back_to_text_when_no_diarized_text(monkeypatch, audio_file):
    """If segments exist but diarized_text is missing, fall back to text."""
    response = dict(_DIARIZED_RESPONSE)
    response.pop("diarized_text")
    captured = {}
    _patch_stt(monkeypatch, response, captured)

    ctx = ToolContext(operator="system")
    result = _run(stt_exec.execute(
        {"audio_path": str(audio_file), "provider": "elevenlabs", "diarize": True}, ctx
    ))
    assert result.success is True
    assert "Hello there. General Kenobi." in result.result


# --------------------------------------------------------------------------- #
# Back-compat — flat Whisper path unchanged
# --------------------------------------------------------------------------- #
def test_flat_path_byte_for_byte_compatible(monkeypatch, audio_file):
    """No provider / no diarize / no mint -> original flat behavior."""
    captured = {}
    _patch_stt(monkeypatch, {"text": "plain transcript"}, captured)

    ctx = ToolContext(operator="system")
    result = _run(stt_exec.execute({"audio_path": str(audio_file)}, ctx))

    assert result.success is True
    assert result.result == "Transcription: plain transcript"
    assert result.data == {"text": "plain transcript"}
    # No provider/diarize passed -> helper received None for both.
    assert captured["stt"]["provider"] is None
    assert captured["stt"]["diarize"] is None


def test_missing_audio_path():
    ctx = ToolContext(operator="system")
    result = _run(stt_exec.execute({}, ctx))
    assert result.success is False
    assert "audio_path is required" in result.result


def test_nonexistent_audio_file():
    ctx = ToolContext(operator="system")
    result = _run(stt_exec.execute({"audio_path": "/nope/missing.wav"}, ctx))
    assert result.success is False
    assert "Audio file not found" in result.result


# --------------------------------------------------------------------------- #
# Task 14 — mint a diarized transcript as a snapshot
# --------------------------------------------------------------------------- #
def test_mint_posts_chat_save_with_diarized_text(monkeypatch, audio_file):
    """mint=true -> /chat/save POST carries diarized_text as assistant_response;
    message gains the snap_id."""
    captured = {}
    _patch_stt(monkeypatch, _DIARIZED_RESPONSE, captured)
    _patch_chat_save(monkeypatch, {"success": True, "snap_id": "SNAP-20260613-9999"}, captured)

    ctx = ToolContext(operator="system")
    result = _run(stt_exec.execute(
        {"audio_path": str(audio_file), "provider": "elevenlabs", "diarize": True, "mint": True},
        ctx,
    ))

    assert result.success is True
    assert "(saved as SNAP-20260613-9999)" in result.result
    payload = captured["chat_save"]["payload"]
    assert payload["operator"] == "system"
    assert payload["assistant_response"] == _DIARIZED_RESPONSE["diarized_text"]
    assert payload["user_message"] == "Transcribed audio: meeting.wav"
    assert payload["model"] == "elevenlabs-scribe-v2"
    assert payload["tokens"] == {"prompt": 0, "completion": 0}


def test_mint_save_failure_is_non_fatal(monkeypatch, audio_file):
    """If /chat/save raises, the tool STILL succeeds (note suffix, not failure)."""
    captured = {}
    _patch_stt(monkeypatch, _DIARIZED_RESPONSE, captured)
    _patch_chat_save(monkeypatch, None, captured, raise_exc=RuntimeError("boom"))

    ctx = ToolContext(operator="system")
    result = _run(stt_exec.execute(
        {"audio_path": str(audio_file), "provider": "elevenlabs", "diarize": True, "mint": True},
        ctx,
    ))

    assert result.success is True
    assert "snapshot save failed: boom" in result.result
    # The diarized transcript still made it into the message.
    assert "2 speakers" in result.result


def test_mint_skipped_when_not_requested(monkeypatch, audio_file):
    """No mint param -> /chat/save is never called."""
    captured = {}
    _patch_stt(monkeypatch, _DIARIZED_RESPONSE, captured)
    _patch_chat_save(monkeypatch, {"snap_id": "SNAP-NOPE"}, captured)

    ctx = ToolContext(operator="system")
    result = _run(stt_exec.execute(
        {"audio_path": str(audio_file), "provider": "elevenlabs", "diarize": True}, ctx
    ))
    assert result.success is True
    assert "chat_save" not in captured
    assert "saved as" not in result.result


def test_mint_on_flat_path_too(monkeypatch, audio_file):
    """mint works on the flat path as well (transcript = text)."""
    captured = {}
    _patch_stt(monkeypatch, {"text": "flat words"}, captured)
    _patch_chat_save(monkeypatch, {"snap_id": "SNAP-FLAT-1"}, captured)

    ctx = ToolContext(operator="bob")
    result = _run(stt_exec.execute({"audio_path": str(audio_file), "mint": True}, ctx))

    assert result.success is True
    assert "(saved as SNAP-FLAT-1)" in result.result
    assert captured["chat_save"]["payload"]["assistant_response"] == "flat words"
    assert captured["chat_save"]["payload"]["operator"] == "bob"
