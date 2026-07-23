"""Real-binary tests for audio_transcode (the bundled imageio-ffmpeg exists in
the dev venv). The M4A round-trip is THE regression test for the pipe-vs-moov
disaster (2026-07-23): a real MP4-family file must decode fully, and an
empty/near-empty decode must RAISE, never return a header-only WAV."""
import math
import struct
import wave
import io
import subprocess

import pytest

from Orchestrator.audio_transcode import TranscodeError, to_wav_pcm16, _ffmpeg_exe


def _sine_wav_bytes(seconds=2.0, sr=16000, freq=440.0) -> bytes:
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
    n = int(seconds * sr)
    w.writeframes(b"".join(
        struct.pack("<h", int(20000 * math.sin(2 * math.pi * freq * i / sr)))
        for i in range(n)))
    w.close()
    return buf.getvalue()


def test_wav_to_wav_preserves_duration(tmp_path):
    src = _sine_wav_bytes(seconds=1.5)
    out = to_wav_pcm16(src, sample_rate=24000)
    w = wave.open(io.BytesIO(out))
    assert w.getframerate() == 24000 and w.getnchannels() == 1
    assert abs(w.getnframes() / 24000 - 1.5) < 0.1


def test_m4a_roundtrip_decodes_fully(tmp_path):
    """Encode a real M4A (moov at the file END, like phone recorders) with the
    bundled ffmpeg, then transcode it back — duration must survive."""
    src_wav = tmp_path / "src.wav"; src_wav.write_bytes(_sine_wav_bytes(seconds=2.0))
    m4a = tmp_path / "clip.m4a"
    r = subprocess.run([_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(src_wav), "-c:a", "aac", str(m4a)],
                       capture_output=True, timeout=60)
    if r.returncode != 0:
        pytest.skip(f"bundled ffmpeg lacks aac encoder: {r.stderr[-120:]!r}")
    out = to_wav_pcm16(m4a.read_bytes(), sample_rate=24000)
    w = wave.open(io.BytesIO(out))
    assert abs(w.getnframes() / 24000 - 2.0) < 0.2, "M4A decoded truncated (moov/pipe regression)"


def test_empty_decode_raises_not_headeronly():
    with pytest.raises(TranscodeError):
        to_wav_pcm16(b"\x00" * 2048)  # junk: must raise, never a 44-byte 'success'


def test_empty_input_raises():
    with pytest.raises(TranscodeError):
        to_wav_pcm16(b"")
