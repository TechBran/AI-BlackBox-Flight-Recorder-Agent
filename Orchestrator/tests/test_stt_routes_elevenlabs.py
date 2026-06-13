"""/stt route: provider=elevenlabs + diarize=true rich payload, and back-compat.

Two contracts under test:
  1. provider=elevenlabs & diarize=true → a RICH response (top-level flat `text`
     for back-compat + segments/speakers/diarized_text/events/language).
  2. NO provider/diarize → the response is STILL just {"text": ...} (the existing
     flat path is untouched).

The provider HTTP is never hit: the ElevenLabs provider's transcribe_bytes (rich
path) and file_transcribe.transcribe_bytes (flat path) are both monkeypatched.
sync_embeddings is mocked before app construction so the startup hook spawns no
network call (mirrors test_toolvault_routes).
"""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    with patch("Orchestrator.toolvault.embeddings.sync_embeddings") as m_emb:
        m_emb.return_value = {"x": {"vector": [0.1]}}
        from Orchestrator.app import app
        with TestClient(app) as c:
            yield c


def _fake_rich():
    """A fake normalized dict: 2 speakers, 2 segments (what Scribe diarized gives)."""
    return {
        "text": "Hello there. General Kenobi.",
        "language": "eng",
        "language_probability": 0.98,
        "provider": "elevenlabs",
        "duration_secs": 3.0,
        "words": [],
        "segments": [
            {"speaker": "speaker_0", "start": 0.0, "end": 1.0, "text": "Hello there."},
            {"speaker": "speaker_1", "start": 1.2, "end": 2.5, "text": "General Kenobi."},
        ],
        "speakers": ["speaker_0", "speaker_1"],
        "events": [],
    }


def test_stt_elevenlabs_diarized_returns_rich_payload(client):
    """provider=elevenlabs + diarize=true → rich body with top-level flat text."""
    with patch("Orchestrator.elevenlabs.stt.transcribe_bytes",
               return_value=_fake_rich()) as m:
        resp = client.post(
            "/stt",
            files={"file": ("meeting.wav", b"RIFFfake-wav", "audio/wav")},
            data={"provider": "elevenlabs", "diarize": "true"},
        )

    assert resp.status_code == 200
    body = resp.json()

    # BACK-COMPAT: flat transcript still at the top level.
    assert body["text"] == "Hello there. General Kenobi."
    assert body["provider"] == "elevenlabs"

    # Diarization payload.
    assert len(body["segments"]) == 2
    assert body["speakers"] == ["speaker_0", "speaker_1"]
    assert isinstance(body["diarized_text"], str)
    # format_diarized produces 1-indexed "Speaker N:" labels for multi-speaker.
    assert "Speaker 1:" in body["diarized_text"]
    assert "Speaker 2:" in body["diarized_text"]
    assert body["events"] == []
    assert body["language"] == "eng"

    # The provider was asked WITH diarization on, and got the upload bytes+name.
    m.assert_called_once()
    args, kwargs = m.call_args
    assert args[0] == b"RIFFfake-wav"
    assert args[1] == "meeting.wav"
    assert kwargs.get("diarize") is True


def test_stt_no_provider_stays_flat_backcompat(client):
    """No provider/diarize → response is ONLY {"text": ...} (existing path)."""
    with patch("Orchestrator.stt.file_transcribe.transcribe_bytes",
               return_value="hello") as m:
        resp = client.post(
            "/stt",
            files={"file": ("clip.webm", b"webm-bytes", "audio/webm")},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"text": "hello"}  # exactly one field, nothing diarized
    # provider passed through as None (resolver decides downstream).
    m.assert_called_once()
    assert m.call_args.kwargs.get("provider") is None


def test_stt_elevenlabs_without_diarize_stays_flat(client):
    """provider=elevenlabs but diarize defaulting false → flat path (no rich body).

    Proves the rich branch requires BOTH provider==elevenlabs AND diarize=true;
    a bare provider hint still routes through the str-contract transcribe_bytes.
    """
    with patch("Orchestrator.stt.file_transcribe.transcribe_bytes",
               return_value="flat scribe text") as m:
        resp = client.post(
            "/stt",
            files={"file": ("clip.wav", b"RIFFx", "audio/wav")},
            data={"provider": "elevenlabs"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"text": "flat scribe text"}
    assert m.call_args.kwargs.get("provider") == "elevenlabs"
