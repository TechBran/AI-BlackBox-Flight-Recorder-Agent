"""Portable audio transcode-to-WAV for user-supplied reference clips.

Phone recorders hand us AAC/M4A/3GPP (Brandon's Fold, 2026-07-23), browsers hand
us webm/ogg — but the on-box synthesis stack (qwen-tts Base clone conditioning)
can only decode what libsndfile knows, which excludes AAC. Every stored clone
reference therefore gets normalized to 16-bit mono WAV AT UPLOAD TIME, using the
imageio-ffmpeg BUNDLED ffmpeg binary (pinned pip dep — no system ffmpeg exists
on the boxes, and we never want a customer box to depend on apt state).

TEMP FILES on both sides, NOT pipes (correction, same day): MP4-family
containers keep the moov index atom at the END of the file, so ffmpeg cannot
reliably demux M4A from a non-seekable pipe:0 — the pipe version "succeeded"
with a 78-byte header-only WAV on a real 2MB phone M4A (and that empty output
then destroyed a stored reference in a repair). Input and output are seekable
temp files, and the decoded duration is VERIFIED before returning.
"""
from __future__ import annotations

import os
import subprocess
import tempfile


class TranscodeError(RuntimeError):
    """ffmpeg could not decode/convert the supplied audio (or decoded ~nothing)."""


# A "successful" transcode must contain real audio, not just a WAV header —
# an empty decode masquerading as success is exactly how the 78-byte repair
# disaster happened. 0.25s at 16-bit mono is a conservative floor.
_MIN_DECODED_S = 0.25


def _ffmpeg_exe() -> str:
    # Lazy import: keeps module import light and lets tests monkeypatch.
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def to_wav_pcm16(data: bytes, *, sample_rate: int = 24000, timeout_s: float = 60.0) -> bytes:
    """Transcode ANY ffmpeg-decodable audio (m4a/aac/webm/ogg/mp3/wav/...) to
    16-bit mono WAV at `sample_rate`. Raises TranscodeError with ffmpeg's stderr
    tail on failure OR when the decode yields less than _MIN_DECODED_S of audio
    (honest error surface — 'Clone failed' told nobody anything, 2026-07-23)."""
    if not data:
        raise TranscodeError("empty audio upload")
    src = dst = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(data)
            src = f.name
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            dst = f.name
        cmd = [
            _ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
            "-i", src,
            "-f", "wav", "-ar", str(sample_rate), "-ac", "1", "-c:a", "pcm_s16le",
            dst,
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
        except FileNotFoundError as e:
            raise TranscodeError(f"bundled ffmpeg unavailable: {e}")
        except subprocess.TimeoutExpired:
            raise TranscodeError(f"audio transcode timed out after {timeout_s:.0f}s")
        if p.returncode != 0:
            tail = (p.stderr or b"").decode(errors="replace").strip()[-300:]
            raise TranscodeError(f"could not decode the reference audio: {tail or 'unknown ffmpeg error'}")
        wav = open(dst, "rb").read()
        min_bytes = 44 + int(_MIN_DECODED_S * sample_rate) * 2
        if len(wav) < min_bytes:
            raise TranscodeError(
                f"decode produced only {max(0, (len(wav) - 44)) // 2 / sample_rate:.2f}s of audio "
                f"(< {_MIN_DECODED_S}s) — the clip may be corrupt or in an unsupported stream")
        return wav
    finally:
        for pth in (src, dst):
            if pth:
                try:
                    os.unlink(pth)
                except OSError:
                    pass
