#!/usr/bin/env python3
"""MANUAL GPU smoke for the qwen-tts variant manager — MS02 only, gate G3.

Loads each of the three real variants in turn through the SAME VariantManager
the server uses, synthesizes a short clip, and reports RTF + wall time + the
model's output sample rate. It exercises FREE-BEFORE-LOAD across the two
transitions and prints nvidia-smi free VRAM before/after each load so a reviewer
can confirm the old variant's VRAM was reclaimed before the next allocation. It
also measures streaming first-packet latency + streaming RTF for the CustomVoice
hot path (two-phase emit), which INFORMS the streaming-default decision (§7).

NOT a pytest test (real checkpoints + CUDA). Run on MS02 with the qwen venv:

    QWEN_TTS_MODEL_DIR=/path/to/qwen3-tts \
    PYTHONPATH=/path/to/LocalModels \
      ${QWEN_TTS_VENV}/bin/python -m qwen_tts_server.smoke_gpu

The Base variant needs a real ~3s reference clip: set QWEN_TTS_SMOKE_REF to a
wav path; otherwise a 3s clip is cut from the fork's bundled sample if present.

G3 read: BATCH RTF < ~0.9 clears the batch tier; the STREAMING RTF is the gate
that picks the streaming default (1.7B if < 0.9, else 0.6B-CustomVoice — §5.4).
"""
import asyncio
import os
import subprocess
import time

from qwen_tts_server import settings
from qwen_tts_server.variant_manager import VariantManager

TEXT = ("The quick brown fox jumps over the lazy dog near the riverbank at dawn, "
        "for a real timing sample that runs a little long on purpose.")
_FORK_REF = "/tmp/qwen-fork/kuklina-1.wav"
_REF_3S = "/tmp/qwen_tts_smoke_ref_3s.wav"


def _free_mb():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            text=True,
        )
        return int(out.strip().splitlines()[0])
    except Exception as exc:
        return f"n/a ({exc})"


def _ensure_ref():
    """Return a real ~3s reference wav path for the Base zero-shot clone, or None
    if none can be produced (Base then reports the missing-ref error instead of
    silently passing)."""
    env = os.environ.get("QWEN_TTS_SMOKE_REF")
    if env and os.path.exists(env):
        return env
    src = _FORK_REF if os.path.exists(_FORK_REF) else None
    if not src:
        return None
    try:
        import soundfile as sf
        data, sr = sf.read(src)
        if getattr(data, "ndim", 1) > 1:
            data = data[:, 0]
        sf.write(_REF_3S, data[: int(3.0 * sr)], sr)
        return _REF_3S
    except Exception as exc:
        print(f"  (could not build 3s ref: {exc})")
        return None


async def _run():
    mgr = VariantManager()
    ref = _ensure_ref()
    plan = [
        (settings.VARIANT_CUSTOM_VOICE, {"preset": settings.PRESET_VOICES[0]}),
        (settings.VARIANT_BASE, {"ref_audio": ref}),
        (settings.VARIANT_VOICE_DESIGN,
         {"design_params": {"instruct": "A calm, warm, friendly narrator with a bright tone."}}),
    ]
    for variant, kwargs in plan:
        print(f"\n=== {variant} ===")
        print(f"free VRAM before load: {_free_mb()} MiB")
        t0 = time.perf_counter()
        try:
            pcm, sr = await mgr.synthesize_full(variant, TEXT, **kwargs)
        except Exception as exc:
            print(f"  synth failed: {exc}")
            print(f"  free VRAM after:  {_free_mb()} MiB (variant resident)")
            continue
        wall = time.perf_counter() - t0
        seconds_audio = (len(pcm) / 2) / float(sr or 1)
        rtf = wall / seconds_audio if seconds_audio else float("inf")
        print(f"  sample_rate (from model): {sr} Hz  (MUST NOT be assumed 24000)")
        print(f"  audio: {seconds_audio:.2f}s  wall: {wall:.2f}s  RTF: {rtf:.2f}")
        print(f"  free VRAM after load: {_free_mb()} MiB (variant resident)")

    # -- streaming first-packet + streaming RTF (CustomVoice hot path) --------
    print("\n=== stream custom_voice (two-phase emit) ===")
    t0 = time.perf_counter()
    sr, aiter = await mgr.stream_true(settings.VARIANT_CUSTOM_VOICE, TEXT,
                                      preset=settings.PRESET_VOICES[0])
    first_ms = None
    total = 0
    async for chunk in aiter:
        total += len(chunk)
        if first_ms is None:
            first_ms = (time.perf_counter() - t0) * 1000.0
    wall = time.perf_counter() - t0
    audio_s = (total / 2) / float(sr or 1)
    srtf = wall / audio_s if audio_s else float("inf")
    fp = f"{first_ms:.0f} ms" if first_ms is not None else "n/a"
    print(f"  sr={sr} first-packet: {fp}  audio: {audio_s:.2f}s  wall: {wall:.2f}s  streaming RTF: {srtf:.2f}")

    print("\nG3 note: STREAMING RTF < ~0.9 => that variant streams; else 0.6B "
          "streaming / 1.7B batch split (§5.4).")


if __name__ == "__main__":
    asyncio.run(_run())
