"""Portable audio transcode-to-WAV for user-supplied reference clips.

Phone recorders hand us AAC/M4A/3GPP (Brandon's Fold, 2026-07-23), browsers hand
us webm/ogg — but the on-box synthesis stack (qwen-tts Base clone conditioning)
can only decode what libsndfile knows, which excludes AAC. Every stored clone
reference therefore gets normalized to 16-bit mono WAV AT UPLOAD TIME, using the
imageio-ffmpeg BUNDLED ffmpeg binary (pinned pip dep — no system ffmpeg exists
on the boxes, and we never want a customer box to depend on apt state).

Pipes only (pipe:0 -> pipe:1): no temp files, no shell, bounded runtime.
"""
from __future__ import annotations

import subprocess


class TranscodeError(RuntimeError):
    """ffmpeg could not decode/convert the supplied audio."""


def _ffmpeg_exe() -> str:
    # Lazy import: keeps module import light and lets tests monkeypatch.
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def to_wav_pcm16(data: bytes, *, sample_rate: int = 24000, timeout_s: float = 60.0) -> bytes:
    """Transcode ANY ffmpeg-decodable audio (m4a/aac/webm/ogg/mp3/wav/...) to
    16-bit mono WAV at `sample_rate`. Raises TranscodeError with ffmpeg's stderr
    tail on failure (honest error surface — 'Clone failed' told nobody anything,
    2026-07-23)."""
    if not data:
        raise TranscodeError("empty audio upload")
    cmd = [
        _ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0",
        "-f", "wav", "-ar", str(sample_rate), "-ac", "1", "-c:a", "pcm_s16le",
        "pipe:1",
    ]
    try:
        p = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout_s)
    except FileNotFoundError as e:
        raise TranscodeError(f"bundled ffmpeg unavailable: {e}")
    except subprocess.TimeoutExpired:
        raise TranscodeError(f"audio transcode timed out after {timeout_s:.0f}s")
    if p.returncode != 0 or not p.stdout:
        tail = (p.stderr or b"").decode(errors="replace").strip()[-300:]
        raise TranscodeError(f"could not decode the reference audio: {tail or 'unknown ffmpeg error'}")
    return p.stdout
