"""ElevenLabs audio-transform utilities: Voice Changer + Voice Isolator.

Two SYNCHRONOUS binary-audio endpoints that take an existing recording and return
a transformed one (seconds, no task queue):

- ``change_voice`` — POST /v1/speech-to-speech/{voice_id} (multipart): re-voices a
  recording into a target ElevenLabs voice, preserving the original delivery/emotion.
- ``isolate`` — POST /v1/audio-isolation (multipart): strips background noise,
  isolating the voice (also useful to clean a noisy sample before cloning).

Both read the file bytes and send them as a multipart part. The part field name
(``audio``) was confirmed against the live API. All auth + error mapping flow
through ``client`` so they exist exactly once. Provider plumbing only — no route
or tool wiring here.
"""
from __future__ import annotations

import os

import requests

from Orchestrator.elevenlabs import client

# Extension -> MIME for the multipart upload (mirrors elevenlabs/stt.py). Unknown
# extensions fall back to a generic binary type (ElevenLabs sniffs content anyway).
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


def _guess_mime(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    return _MIME_BY_EXT.get(ext, "application/octet-stream")


def _parse_body(resp: requests.Response) -> dict | None:
    """Defensively parse an error body that may not be JSON (map_error tolerates None)."""
    try:
        return resp.json()
    except Exception:
        return None


def change_voice(
    audio_path: str,
    target_voice_id: str,
    *,
    output_format: str | None = None,
) -> bytes:
    """POST /v1/speech-to-speech/{target_voice_id} (multipart) and return audio ``bytes``.

    ``target_voice_id`` may carry the ``elevenlabs:`` prefix; it is stripped here so
    the RAW id reaches the API path. ``output_format`` is sent as a query param only
    when provided. The original recording's delivery/emotion is preserved in the
    re-voiced output.

    Any non-2xx raises ``RuntimeError(client.map_error(...))`` with the error body
    defensively parsed (it may not be JSON).
    """
    # The API path wants the RAW id; tolerate ids that already lack the prefix.
    raw_voice_id = target_voice_id.split("elevenlabs:")[-1]

    with open(audio_path, "rb") as fh:
        audio_bytes = fh.read()

    params: dict = {}
    if output_format:
        params["output_format"] = output_format

    resp = requests.post(
        f"{client.BASE_URL}/v1/speech-to-speech/{raw_voice_id}",
        headers=client.auth_headers(),
        params=params,
        files={"audio": (os.path.basename(audio_path), audio_bytes, _guess_mime(audio_path))},
        timeout=120,
    )
    if 200 <= resp.status_code < 300:
        return resp.content

    raise RuntimeError(client.map_error(resp.status_code, _parse_body(resp)))


def isolate(audio_path: str) -> bytes:
    """POST /v1/audio-isolation (multipart) and return the cleaned audio ``bytes``.

    Reads ``audio_path`` and sends it as the ``audio`` multipart part; the API
    returns the same recording with background noise stripped.

    Any non-2xx raises ``RuntimeError(client.map_error(...))`` with the error body
    defensively parsed (it may not be JSON).
    """
    with open(audio_path, "rb") as fh:
        audio_bytes = fh.read()

    resp = requests.post(
        f"{client.BASE_URL}/v1/audio-isolation",
        headers=client.auth_headers(),
        files={"audio": (os.path.basename(audio_path), audio_bytes, _guess_mime(audio_path))},
        timeout=120,
    )
    if 200 <= resp.status_code < 300:
        return resp.content

    raise RuntimeError(client.map_error(resp.status_code, _parse_body(resp)))
