#!/usr/bin/env python3
"""
file_transcribe.py - Multi-provider file (batch) speech-to-text.

Exposes transcribe_bytes() which resolves an STT provider (OpenAI or Google
Chirp 2) and delegates to the matching helper. The OpenAI path posts the audio
to the transcriptions endpoint; the Google path uses the speech_v2 SDK (lazily
imported so this module loads even when google-cloud-speech is not installed).
"""
from __future__ import annotations

import json

import requests

from Orchestrator import config
from Orchestrator.stt.resolve import resolve_stt_provider


def transcribe_bytes(audio_bytes: bytes, content_type: str, *, provider: str = None,
                     filename: str = "audio.webm") -> str:
    """Transcribe raw audio bytes using the resolved (or explicit) STT provider.

    Returns the transcript string (stripped). Raises RuntimeError if no provider
    is configured/available.
    """
    provider = provider or resolve_stt_provider()
    if not provider:
        raise RuntimeError("no STT provider configured")
    if provider == "google":
        return _google_transcribe(audio_bytes, content_type, filename)
    return _openai_transcribe(audio_bytes, content_type, filename)


def _openai_transcribe(audio_bytes: bytes, content_type: str, filename: str) -> str:
    """Transcribe via OpenAI's transcriptions endpoint (multipart upload)."""
    api_key = (config.OPENAI_API_KEY or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    files = {"file": (filename, audio_bytes, content_type)}
    data = {"model": config.STT_OPENAI_FILE}
    r = requests.post(
        config.OPENAI_STT_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        data=data,
        files=files,
        timeout=60,
    )
    if r.status_code != 200:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"OpenAI STT error: {detail}")
    try:
        j = r.json()
        return (j.get("text") or "").strip()
    except Exception:
        return r.text.strip()


def _google_transcribe(audio_bytes: bytes, content_type: str, filename: str) -> str:
    """Transcribe via Google Cloud Speech-to-Text v2 (Chirp 2). Lazy SDK import."""
    from google.api_core.client_options import ClientOptions
    from google.cloud.speech_v2 import SpeechClient
    from google.cloud.speech_v2.types import (
        AutoDetectDecodingConfig,
        RecognitionConfig,
        RecognizeRequest,
    )

    creds_path = config.GOOGLE_APPLICATION_CREDENTIALS
    with open(creds_path, "r") as f:
        project_id = json.load(f).get("project_id")

    region = config.STT_GOOGLE_REGION
    client = SpeechClient(
        client_options=ClientOptions(api_endpoint=f"{region}-speech.googleapis.com")
    )

    recognizer = f"projects/{project_id}/locations/{region}/recognizers/_"
    rec_config = RecognitionConfig(
        auto_decoding_config=AutoDetectDecodingConfig(),
        language_codes=["en-US"],
        model=config.STT_GOOGLE_MODEL,
    )
    request = RecognizeRequest(
        recognizer=recognizer,
        config=rec_config,
        content=audio_bytes,
    )
    response = client.recognize(request=request)
    for result in response.results:
        if result.alternatives:
            return (result.alternatives[0].transcript or "").strip()
    return ""
