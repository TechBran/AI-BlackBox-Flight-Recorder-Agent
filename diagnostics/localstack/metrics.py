#!/usr/bin/env python3
"""diagnostics/localstack/metrics.py — pure measurement helpers for the
local-model benchmark gates (G1/G3/G5). No I/O, no hardware; unit-tested in
Orchestrator/tests/test_localstack_metrics.py. The live probes import these."""
from __future__ import annotations
import struct
from statistics import median


def parse_nvidia_smi_used_mib(text: str) -> int:
    """First GPU line of `nvidia-smi --query-gpu=memory.used
    --format=csv,noheader,nounits` -> used MiB. Raises ValueError if no
    numeric line is present."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        return int(line.split(",")[0].strip())
    raise ValueError(f"no GPU memory line in: {text!r}")


def parse_wav_header(wav: bytes) -> dict:
    """Walk the RIFF chunks of a PCM WAV and return the real header fields:
    {sample_rate, byte_rate, channels, bits, data_bytes, duration_seconds}.
    Robust to a non-canonical layout (e.g. a JUNK/LIST chunk before ``fmt ``)
    because it walks chunk-by-chunk rather than assuming fixed offsets. Raises
    ValueError on a non-PCM-WAV blob. Single source for both duration and
    sample-rate so callers never hardcode a byte offset."""
    if len(wav) < 44 or wav[0:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE blob")
    pos = 12
    channels = sample_rate = byte_rate = bits = None
    data_size = None
    while pos + 8 <= len(wav):
        cid = wav[pos:pos + 4]
        (csize,) = struct.unpack_from("<I", wav, pos + 4)
        body = pos + 8
        if cid == b"fmt ":
            _fmt, channels, sample_rate, byte_rate, _ba, bits = \
                struct.unpack_from("<HHIIHH", wav, body)
        elif cid == b"data":
            data_size = csize
            break
        pos = body + csize + (csize & 1)  # chunks are word-aligned
    if not byte_rate or data_size is None:
        raise ValueError("missing fmt/data chunk")
    return {"sample_rate": sample_rate, "byte_rate": byte_rate,
            "channels": channels, "bits": bits, "data_bytes": data_size,
            "duration_seconds": data_size / float(byte_rate)}


def wav_duration_seconds(wav: bytes) -> float:
    """Duration of a PCM WAV from its RIFF header = data_bytes / byte_rate.
    Raises ValueError on a non-PCM-WAV blob. Used for TTS RTF."""
    return parse_wav_header(wav)["duration_seconds"]


def rtf(wall_seconds: float, audio_seconds: float) -> float:
    """Real-time factor: <1.0 = faster than real time."""
    if audio_seconds <= 0:
        raise ValueError("audio_seconds must be > 0")
    return wall_seconds / audio_seconds


def _percentile(sorted_s, p: float) -> float:
    """Linear-interpolation percentile (numpy default) over a pre-sorted,
    non-empty list. p in [0, 100]."""
    if len(sorted_s) == 1:
        return sorted_s[0]
    k = (len(sorted_s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_s) - 1)
    return sorted_s[lo] + (sorted_s[hi] - sorted_s[lo]) * (k - lo)


def summarize_latencies(samples) -> dict:
    """min/median/max + p90/p95/p99 percentiles over a non-empty list of
    latency seconds. The percentiles are what the G-gate criteria report."""
    s = sorted(samples)
    if not s:
        raise ValueError("no samples")
    return {"n": len(s), "min_s": round(min(s), 3),
            "median_s": round(median(s), 3), "max_s": round(max(s), 3),
            "p90_s": round(_percentile(s, 90), 3),
            "p95_s": round(_percentile(s, 95), 3),
            "p99_s": round(_percentile(s, 99), 3)}
