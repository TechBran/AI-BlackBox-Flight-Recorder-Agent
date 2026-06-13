"""The ElevenLabs branch of file_transcribe.transcribe_bytes() returns a STRING.

This is the contract guard: every existing str-contract caller (/stt/json,
Gemini Live, /stt/translate) must keep getting a flat transcript string when
ElevenLabs is the resolved provider — the rich diarized dict from the provider
module must be reduced to normalized["text"] here, never leaked upward.
"""
from unittest.mock import patch

from Orchestrator.stt import file_transcribe as ft


def _fake_normalized(text="hello from scribe"):
    """A minimal ElevenLabs-normalized dict (the shape transcribe_bytes returns)."""
    return {
        "text": text,
        "language": "eng",
        "provider": "elevenlabs",
        "segments": [{"speaker": "speaker_0", "start": 0.0, "end": 1.0, "text": text}],
        "speakers": ["speaker_0"],
        "events": [],
        "words": [],
    }


def test_elevenlabs_branch_returns_flat_string():
    """provider=elevenlabs → the FLAT normalized['text'], not the rich dict."""
    with patch("Orchestrator.elevenlabs.stt.transcribe_bytes",
               return_value=_fake_normalized("hello from scribe")) as m:
        result = ft.transcribe_bytes(b"x", "audio/wav", provider="elevenlabs")

    assert isinstance(result, str)
    assert result == "hello from scribe"
    # Diarization is OFF on this str-contract path (rich payload only on /stt).
    m.assert_called_once()
    assert m.call_args.kwargs.get("diarize") is False


def test_elevenlabs_branch_strips_text():
    """The returned text is stripped (matches OpenAI/Google branches)."""
    with patch("Orchestrator.elevenlabs.stt.transcribe_bytes",
               return_value=_fake_normalized("  spaced  ")):
        assert ft.transcribe_bytes(b"x", "audio/wav", provider="elevenlabs") == "spaced"


def test_elevenlabs_branch_passes_filename_through():
    """The upload filename reaches the provider (so MIME is guessed correctly)."""
    with patch("Orchestrator.elevenlabs.stt.transcribe_bytes",
               return_value=_fake_normalized()) as m:
        ft.transcribe_bytes(b"x", "audio/wav", provider="elevenlabs", filename="clip.mp3")
    args, kwargs = m.call_args
    # transcribe_bytes(audio_bytes, filename, diarize=False)
    assert args[0] == b"x"
    assert args[1] == "clip.mp3"
