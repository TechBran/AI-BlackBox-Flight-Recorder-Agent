"""Hermetic tests for the ElevenLabs Scribe batch transcription module.

Diarization is the centerpiece: the Scribe batch API has NO ``segments`` field,
so ``normalize_transcript`` must BUILD speaker segments by grouping consecutive
words by ``speaker_id``. These tests assert against a REAL response captured live
(``fixtures/elevenlabs_scribe_diarized.json``) -- the expected values below are
ground truth from a genuine 2-speaker sample, not a mock-of-a-mock.

The HTTP path (``transcribe_file``) is exercised with ``requests.post``
monkeypatched -- no live network is ever touched here. (The end-to-end live
diarization proof lived in a throwaway smoke script, run once and deleted.)
"""
import json
from pathlib import Path

import pytest

from Orchestrator.elevenlabs import client as el
from Orchestrator.elevenlabs import stt

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "elevenlabs_scribe_diarized.json"


@pytest.fixture
def scribe_fixture() -> dict:
    """The REAL Scribe batch response captured live (2 speakers, 3 turns)."""
    return json.loads(_FIXTURE_PATH.read_text())


# --- normalize_transcript against the REAL fixture ----------------------------

def test_normalize_top_level_fields(scribe_fixture):
    norm = stt.normalize_transcript(scribe_fixture)
    # text is the fixture's full transcript, verbatim.
    assert norm["text"] == scribe_fixture["text"]
    # language is the ISO 639-3 code straight from the API ("eng", NOT "en").
    assert norm["language"] == "eng"
    assert norm["provider"] == "elevenlabs"
    # duration is a real number passed through from audio_duration_secs.
    assert isinstance(norm["duration_secs"], (int, float))
    assert norm["duration_secs"] == pytest.approx(9.4165)
    # language_probability passed through.
    assert norm["language_probability"] == pytest.approx(0.9523448944091797)
    # raw words passthrough is the untouched list.
    assert norm["words"] == scribe_fixture["words"]


def test_normalize_builds_three_segments_from_per_word_speaker_id(scribe_fixture):
    norm = stt.normalize_transcript(scribe_fixture)
    segs = norm["segments"]
    # Grouping consecutive words by speaker_id yields exactly 3 turns.
    assert len(segs) == 3

    # Turn 0: speaker_0, text stripped of leading/trailing whitespace.
    assert segs[0]["speaker"] == "speaker_0"
    assert segs[0]["text"] == "Hey, did you finish the quarterly report?"

    # Turn 1: speaker_1.
    assert segs[1]["speaker"] == "speaker_1"
    assert segs[1]["text"] == "Yes, I sent it this morning. Check your inbox."

    # Turn 2: speaker_0 again (a NEW turn, proving consecutive-grouping not by-speaker-bucket).
    assert segs[2]["speaker"] == "speaker_0"
    assert segs[2]["text"] == "Great, thanks. Let's review it at 3 o'clock."

    # Each segment carries numeric start/end spanning its words.
    assert segs[0]["start"] == pytest.approx(0.079)
    assert segs[0]["end"] <= segs[1]["start"]
    for s in segs:
        assert isinstance(s["start"], (int, float))
        assert isinstance(s["end"], (int, float))
        assert s["end"] >= s["start"]


def test_normalize_distinct_sorted_speakers(scribe_fixture):
    norm = stt.normalize_transcript(scribe_fixture)
    assert norm["speakers"] == ["speaker_0", "speaker_1"]


def test_normalize_events_empty_when_no_audio_events(scribe_fixture):
    norm = stt.normalize_transcript(scribe_fixture)
    # This live sample contained no audio events.
    assert norm["events"] == []


# --- format_diarized against the REAL fixture ---------------------------------

def test_format_diarized_multi_speaker_lines(scribe_fixture):
    norm = stt.normalize_transcript(scribe_fixture)
    out = stt.format_diarized(norm)
    assert isinstance(out, str)

    lines = [ln for ln in out.splitlines() if ln.strip()]
    # Three turns -> three non-empty lines.
    assert len(lines) == 3

    # 1-indexed speaker labels by first-appearance order.
    assert "Speaker 1:" in out  # speaker_0
    assert "Speaker 2:" in out  # speaker_1

    # First line is speaker_0's first turn at timestamp [00:00].
    assert lines[0].startswith("[00:00]")
    assert "Speaker 1:" in lines[0]

    # speaker_0's text appears AFTER the "Speaker 1:" label (label precedes text).
    idx_label = out.index("Speaker 1:")
    idx_text = out.index("Hey, did you finish the quarterly report?")
    assert idx_text > idx_label

    # speaker_1 maps to "Speaker 2".
    idx_s2 = out.index("Speaker 2:")
    idx_s2_text = out.index("Yes, I sent it this morning.")
    assert idx_s2_text > idx_s2


# --- synthetic: single speaker ------------------------------------------------

def test_normalize_single_speaker():
    resp = {
        "language_code": "eng",
        "language_probability": 0.99,
        "audio_duration_secs": 2.0,
        "text": "Hello there friend.",
        "words": [
            {"text": "Hello", "start": 0.0, "end": 0.3, "type": "word", "speaker_id": "speaker_0"},
            {"text": " ", "start": 0.3, "end": 0.4, "type": "spacing", "speaker_id": "speaker_0"},
            {"text": "there", "start": 0.4, "end": 0.7, "type": "word", "speaker_id": "speaker_0"},
            {"text": " ", "start": 0.7, "end": 0.8, "type": "spacing", "speaker_id": "speaker_0"},
            {"text": "friend.", "start": 0.8, "end": 1.2, "type": "word", "speaker_id": "speaker_0"},
        ],
    }
    norm = stt.normalize_transcript(resp)
    assert len(norm["segments"]) == 1
    assert norm["segments"][0]["speaker"] == "speaker_0"
    assert norm["segments"][0]["text"] == "Hello there friend."
    assert norm["speakers"] == ["speaker_0"]
    assert norm["events"] == []


# --- synthetic: audio event word goes to events, NOT segment text -------------

def test_audio_event_word_separated_into_events():
    resp = {
        "language_code": "eng",
        "audio_duration_secs": 3.0,
        "text": "That was funny",
        "words": [
            {"text": "That", "start": 0.0, "end": 0.3, "type": "word", "speaker_id": "speaker_0"},
            {"text": " ", "start": 0.3, "end": 0.4, "type": "spacing", "speaker_id": "speaker_0"},
            {"text": "was", "start": 0.4, "end": 0.7, "type": "word", "speaker_id": "speaker_0"},
            {"text": " ", "start": 0.7, "end": 0.8, "type": "spacing", "speaker_id": "speaker_0"},
            {"text": "funny", "start": 0.8, "end": 1.2, "type": "word", "speaker_id": "speaker_0"},
            {"text": "(laughter)", "start": 1.2, "end": 2.0, "type": "audio_event", "speaker_id": "speaker_0"},
        ],
    }
    norm = stt.normalize_transcript(resp)

    # The audio-event word IS in events.
    assert len(norm["events"]) == 1
    ev = norm["events"][0]
    assert ev["type"] == "audio_event"
    assert ev["text"] == "(laughter)"
    assert ev["start"] == pytest.approx(1.2)
    assert ev["end"] == pytest.approx(2.0)

    # The audio-event word is NOT in any segment's text.
    joined = " ".join(s["text"] for s in norm["segments"])
    assert "(laughter)" not in joined
    assert norm["segments"][0]["text"] == "That was funny"
    # speakers derived only from word/spacing words.
    assert norm["speakers"] == ["speaker_0"]


# --- transcribe_file HTTP path (monkeypatched requests.post) ------------------

class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_transcribe_file_success_returns_normalized(monkeypatch, tmp_path, scribe_fixture):
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _FakeResp(200, scribe_fixture)

    monkeypatch.setattr(stt.requests, "post", fake_post)
    monkeypatch.setattr(el, "auth_headers", lambda key=None: {"xi-api-key": "xi-fake"})

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFFfake-wav-bytes")

    result = stt.transcribe_file(str(audio))

    # Proves it returns the NORMALIZED dict (not the raw response).
    assert result == stt.normalize_transcript(scribe_fixture)
    assert result["provider"] == "elevenlabs"
    assert result["speakers"] == ["speaker_0", "speaker_1"]
    assert len(result["segments"]) == 3

    # It hit the batch STT endpoint with the auth header.
    assert captured["url"].endswith("/v1/speech-to-text")
    assert captured["kwargs"]["headers"] == {"xi-api-key": "xi-fake"}

    # multipart file + the documented form fields.
    assert "file" in captured["kwargs"]["files"]
    data = captured["kwargs"]["data"]
    assert data["model_id"] == "scribe_v2"  # config default
    assert data["diarize"] == "true"
    assert data["tag_audio_events"] == "true"
    assert data["timestamps_granularity"] == "word"
    # language omitted when not provided.
    assert "language_code" not in data


def test_transcribe_file_passes_options(monkeypatch, tmp_path, scribe_fixture):
    captured = {}

    def fake_post(url, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeResp(200, scribe_fixture)

    monkeypatch.setattr(stt.requests, "post", fake_post)
    monkeypatch.setattr(el, "auth_headers", lambda key=None: {"xi-api-key": "xi-fake"})

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"fake-mp3")

    stt.transcribe_file(
        str(audio), diarize=False, language="eng",
        tag_audio_events=False, model_id="scribe_v2_custom",
    )
    data = captured["kwargs"]["data"]
    assert data["model_id"] == "scribe_v2_custom"
    assert data["diarize"] == "false"
    assert data["tag_audio_events"] == "false"
    assert data["language_code"] == "eng"
    # mp3 mime guessed from extension.
    fname, _fh, mime = captured["kwargs"]["files"]["file"]
    assert fname == "clip.mp3"
    assert mime == "audio/mpeg"


def test_transcribe_file_401_raises_mapped_auth_error(monkeypatch, tmp_path):
    def fake_post(url, **kwargs):
        return _FakeResp(401, {"detail": {"status": "auth_error"}})

    monkeypatch.setattr(stt.requests, "post", fake_post)
    monkeypatch.setattr(el, "auth_headers", lambda key=None: {"xi-api-key": "xi-bad"})

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFFfake")

    with pytest.raises(RuntimeError) as exc:
        stt.transcribe_file(str(audio))
    # The mapped, human-readable auth message -- proves map_error was used.
    assert "auth" in str(exc.value).lower()
    assert "ElevenLabs" in str(exc.value)


def test_transcribe_file_non_json_error_body_is_tolerated(monkeypatch, tmp_path):
    # A non-2xx response whose body is NOT JSON must still map cleanly (no crash).
    def fake_post(url, **kwargs):
        return _FakeResp(500, ValueError("not json"))

    monkeypatch.setattr(stt.requests, "post", fake_post)
    monkeypatch.setattr(el, "auth_headers", lambda key=None: {"xi-api-key": "xi-fake"})

    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFFfake")

    with pytest.raises(RuntimeError) as exc:
        stt.transcribe_file(str(audio))
    assert "ElevenLabs error 500" in str(exc.value)
