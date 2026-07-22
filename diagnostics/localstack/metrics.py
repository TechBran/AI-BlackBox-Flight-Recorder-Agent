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


def wav_duration_seconds(wav: bytes) -> float:
    """Duration of a PCM WAV from its RIFF header = data_bytes / byte_rate.
    Raises ValueError on a non-PCM-WAV blob. Used for TTS RTF."""
    if len(wav) < 44 or wav[0:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE blob")
    pos, byte_rate, data_size = 12, None, None
    while pos + 8 <= len(wav):
        cid = wav[pos:pos + 4]
        (csize,) = struct.unpack_from("<I", wav, pos + 4)
        body = pos + 8
        if cid == b"fmt ":
            _fmt, _ch, _sr, byte_rate, _ba, _bits = struct.unpack_from(
                "<HHIIHH", wav, body)
        elif cid == b"data":
            data_size = csize
            break
        pos = body + csize + (csize & 1)  # chunks are word-aligned
    if not byte_rate or data_size is None:
        raise ValueError("missing fmt/data chunk")
    return data_size / float(byte_rate)


def rtf(wall_seconds: float, audio_seconds: float) -> float:
    """Real-time factor: <1.0 = faster than real time."""
    if audio_seconds <= 0:
        raise ValueError("audio_seconds must be > 0")
    return wall_seconds / audio_seconds


def summarize_latencies(samples) -> dict:
    """min/median/max over a non-empty list of latency seconds."""
    s = list(samples)
    if not s:
        raise ValueError("no samples")
    return {"n": len(s), "min_s": round(min(s), 3),
            "median_s": round(median(s), 3), "max_s": round(max(s), 3)}
