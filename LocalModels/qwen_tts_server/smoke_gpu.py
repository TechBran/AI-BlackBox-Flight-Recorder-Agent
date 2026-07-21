#!/usr/bin/env python3
"""MANUAL GPU smoke for the qwen-tts variant manager — MS02 only, gate G3.

Loads each of the three real variants in turn through the SAME VariantManager
the server uses, synthesizes a short clip, and reports RTF + wall time + the
model's output sample rate. It exercises FREE-BEFORE-LOAD across the two
transitions and prints nvidia-smi free VRAM before/after each load so a reviewer
can confirm the old variant's VRAM was reclaimed before the next allocation.

NOT a pytest test (real checkpoints + CUDA). Run on MS02 with the qwen venv:

    QWEN_TTS_MODEL_DIR=/path/to/qwen3-tts \
      ${QWEN_TTS_VENV}/bin/python -m qwen_tts_server.smoke_gpu

Feeds G3: RTF < ~0.9 -> 1.7B streams; else 0.6B streaming / 1.7B batch split.
Planning-time expectation (§5.4): 1.7B streaming near-certainly FAILS <0.9 on the
2000 Ada, so the streaming default is 0.6B-CustomVoice, 1.7B is the batch tier.
"""
import asyncio
import subprocess
import time

from qwen_tts_server import settings
from qwen_tts_server.variant_manager import VariantManager

TEXT = "The quick brown fox jumps over the lazy dog, twice, for a real timing sample."


def _free_mb():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            text=True,
        )
        return int(out.strip().splitlines()[0])
    except Exception as exc:
        return f"n/a ({exc})"


async def _run():
    mgr = VariantManager()
    plan = [
        (settings.VARIANT_CUSTOM_VOICE, {"preset": settings.PRESET_VOICES[0]}),
        (settings.VARIANT_BASE, {"ref_audio": None}),           # supply a real ~3s ref path on MS02
        (settings.VARIANT_VOICE_DESIGN, {"design_params": None}),  # supply a real design on MS02
    ]
    for variant, kwargs in plan:
        print(f"\n=== {variant} ===")
        print(f"free VRAM before load: {_free_mb()} MiB")
        t0 = time.perf_counter()
        try:
            pcm, sr = await mgr.synthesize_full(variant, TEXT, **kwargs)
        except Exception as exc:
            print(f"  synth failed (expected until real refs/design supplied): {exc}")
            print(f"  free VRAM after:  {_free_mb()} MiB (variant resident)")
            continue
        wall = time.perf_counter() - t0
        seconds_audio = (len(pcm) / 2) / float(sr or 1)
        rtf = wall / seconds_audio if seconds_audio else float("inf")
        print(f"  sample_rate (from model): {sr} Hz  (MUST NOT be assumed 24000)")
        print(f"  audio: {seconds_audio:.2f}s  wall: {wall:.2f}s  RTF: {rtf:.2f}")
        print(f"  free VRAM after load: {_free_mb()} MiB (variant resident)")
    print("\nG3 note: RTF < ~0.9 => that variant streams; else batch tier (§5.4).")


if __name__ == "__main__":
    asyncio.run(_run())
