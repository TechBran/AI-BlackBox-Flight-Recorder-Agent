"""ElevenLabs Scribe BATCH transcription: file upload + diarization normalizer.

Scribe's batch response (verified against a live 2-speaker capture, see
``tests/fixtures/elevenlabs_scribe_diarized.json``) has NO ``segments`` field --
diarization is PER-WORD via each word's ``speaker_id``. So this module BUILDS
speaker turns by grouping consecutive words by ``speaker_id``, then exposes:

  normalize_transcript(resp) -> the BlackBox transcript shape (text, language,
      segments, speakers, events, raw words passthrough).
  format_diarized(normalized) -> a human-readable, one-line-per-turn transcript
      with 1-indexed "Speaker N" labels (first-appearance order) + [mm:ss] stamps.
  transcribe_file(path, ...) -> POST a local audio file to /v1/speech-to-text and
      return normalize_transcript(resp).

All HTTP auth + error mapping flow through ``client`` so they exist exactly once;
``transcribe_file`` raises ``RuntimeError(client.map_error(...))`` on any non-2xx
(defensively parsing an error body that may not be JSON).

This module is provider plumbing only -- it does NOT wire any /stt route or the
speech_to_text tool (Tasks 12-14 own that).
"""
from __future__ import annotations

import os

import requests

from Orchestrator import config
from Orchestrator.elevenlabs import client

# Word ``type`` values that carry transcript text (everything else -- e.g.
# "audio_event" -- is a non-speech event we surface separately).
_TEXT_TYPES = ("word", "spacing")

# Minimal extension -> MIME map for the multipart upload. Anything unknown falls
# back to a generic binary type (ElevenLabs sniffs the content regardless).
_MIME_BY_EXT = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
    ".aac": "audio/aac",
}


def _guess_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _MIME_BY_EXT.get(ext, "application/octet-stream")


def normalize_transcript(resp: dict) -> dict:
    """Normalize a raw Scribe batch response into the BlackBox transcript shape.

    Builds speaker segments from per-word ``speaker_id`` (the API has no
    ``segments`` field): consecutive words sharing a ``speaker_id`` form one turn.
    Only ``word``/``spacing`` words contribute to segment text; any other type
    (e.g. ``audio_event``) is collected into ``events`` instead, never leaking
    into a turn's text.
    """
    resp = resp or {}
    words = resp.get("words") or []

    segments: list[dict] = []
    events: list[dict] = []
    speakers: list[str] = []  # insertion order = first-appearance order
    seen_speakers: set[str] = set()

    for w in words:
        wtype = w.get("type")
        if wtype not in _TEXT_TYPES:
            # Non-speech event (audio_event, etc.) -- surface separately, keep
            # it out of every segment's text.
            events.append({
                "type": wtype,
                "start": w.get("start"),
                "end": w.get("end"),
                "text": w.get("text", ""),
            })
            continue

        sid = w.get("speaker_id")
        if sid not in seen_speakers:
            seen_speakers.add(sid)
            speakers.append(sid)

        if not segments or segments[-1]["speaker"] != sid:
            segments.append({
                "speaker": sid,
                "start": w.get("start"),
                "end": w.get("end"),
                "text": w.get("text", ""),
            })
        else:
            segments[-1]["text"] += w.get("text", "")
            segments[-1]["end"] = w.get("end")

    # Trim each turn's accumulated text (leading/trailing spacing words).
    for seg in segments:
        seg["text"] = seg["text"].strip()

    return {
        "text": resp.get("text", ""),
        "language": resp.get("language_code"),
        "language_probability": resp.get("language_probability"),
        "provider": "elevenlabs",
        "duration_secs": resp.get("audio_duration_secs"),
        "words": words,  # raw passthrough
        "segments": segments,
        "speakers": sorted(speakers),
        "events": events,
    }


def _fmt_timestamp(start) -> str:
    """``[mm:ss]`` from a float second offset (0 if missing)."""
    secs = int(start or 0)
    return f"[{secs // 60:02d}:{secs % 60:02d}]"


def format_diarized(normalized: dict) -> str:
    """Human-readable transcript: one line per speaker turn.

    Format: ``[mm:ss] Speaker N: <text>`` where ``speaker_0`` -> "Speaker 1"
    (1-indexed, stable mapping by first-appearance order). With a single speaker
    the labels are dropped and the turns are simply joined into sentences --
    but multi-speaker diarization is the point.
    """
    segments = normalized.get("segments") or []
    speakers = normalized.get("speakers") or []

    # Single-speaker (or none): no labels, just the spoken text.
    if len(speakers) <= 1:
        return " ".join(seg.get("text", "") for seg in segments if seg.get("text")).strip()

    # Map speaker_id -> 1-indexed label by FIRST-APPEARANCE order across turns
    # (not the sorted ``speakers`` list -- the first speaker to talk is Speaker 1).
    label_by_id: dict[str, int] = {}
    lines: list[str] = []
    for seg in segments:
        sid = seg.get("speaker")
        if sid not in label_by_id:
            label_by_id[sid] = len(label_by_id) + 1
        n = label_by_id[sid]
        lines.append(f"{_fmt_timestamp(seg.get('start'))} Speaker {n}: {seg.get('text', '')}")

    return "\n".join(lines)


def transcribe_file(
    path: str,
    *,
    diarize: bool = True,
    language: str | None = None,
    tag_audio_events: bool = True,
    model_id: str | None = None,
) -> dict:
    """POST a local audio file to ``/v1/speech-to-text`` and return the
    normalized transcript.

    multipart ``file`` part + the documented form fields (word-granularity
    timestamps always requested so diarization grouping has per-word spans).
    On any non-2xx the error body is defensively parsed (it may not be JSON)
    and surfaced via ``client.map_error`` as a ``RuntimeError``.
    """
    data = {
        "model_id": model_id or config.ELEVENLABS_STT_FILE_MODEL,
        "diarize": str(diarize).lower(),
        "tag_audio_events": str(tag_audio_events).lower(),
        "timestamps_granularity": "word",
    }
    if language:
        data["language_code"] = language

    with open(path, "rb") as fh:
        resp = requests.post(
            client.BASE_URL + "/v1/speech-to-text",
            headers=client.auth_headers(),
            files={"file": (os.path.basename(path), fh, _guess_mime(path))},
            data=data,
            timeout=120,
        )

    if not (200 <= resp.status_code < 300):
        body = None
        try:
            body = resp.json()
        except Exception:
            body = None  # error body may not be JSON; map_error tolerates None
        raise RuntimeError(client.map_error(resp.status_code, body))

    return normalize_transcript(resp.json())
