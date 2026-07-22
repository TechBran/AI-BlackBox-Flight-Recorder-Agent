#!/usr/bin/env python3
"""diagnostics/localstack/tts_rtf.py — G3 live probe (MS02). Measures, against
the on-box qwen-tts member through the llama-swap front door
(127.0.0.1:9098/v1/audio/speech):
  * RTF per voice = wall / audio_seconds (metrics.wav_duration_seconds)
  * streaming first-packet latency (stream=true; time to first audio byte)
  * output sample-rate READ FROM the returned WAV (never hardcode 24k; §5.4)
Variant-transition PEAK VRAM is a separate step (vram.py wrapping a
CustomVoice->Base->VoiceDesign sweep). The <0.9 RTF result INFORMS the
streaming-default decision (§7); it is not a hard pass/fail — exit 0 always."""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
import requests

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from diagnostics.localstack.metrics import (  # noqa: E402
    parse_wav_header, rtf, summarize_latencies)

BASE = "http://127.0.0.1:9098/v1"
# model="qwen-tts" routes to the member; the VARIANT is inferred from the
# voice (preset -> CustomVoice hot path; clone/design slugs -> Base/VoiceDesign).
CASES = [("qwen-tts", "Vivian", "customvoice-Vivian"),
         ("qwen-tts", "Dylan", "customvoice-Dylan")]


def measure_batch(model, voice, text):
    t0 = time.time()
    r = requests.post(f"{BASE}/audio/speech", timeout=300, json={
        "model": model, "input": text, "voice": voice,
        "response_format": "wav", "stream": False})
    r.raise_for_status()
    wall, wav = time.time() - t0, r.content
    hdr = parse_wav_header(wav)  # duration AND sample_rate from the real header
    audio_s = hdr["duration_seconds"]
    return {"wall_s": round(wall, 3), "audio_s": round(audio_s, 3),
            "rtf": round(rtf(wall, audio_s), 3),
            "sample_rate": hdr["sample_rate"], "bytes": len(wav)}


def measure_first_packet(model, voice, text):
    t0 = time.time()
    with requests.post(f"{BASE}/audio/speech", stream=True, timeout=300, json={
            "model": model, "input": text, "voice": voice,
            "response_format": "wav", "stream": True}) as r:
        r.raise_for_status()
        for chunk in r.iter_content(chunk_size=1024):
            if chunk:
                return round(time.time() - t0, 3)
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--gate-rtf", type=float, default=0.9)
    ap.add_argument("--text", default="The quick brown fox jumps over the "
                    "lazy dog near the riverbank at dawn.")
    args = ap.parse_args(argv)
    results = []
    for model, voice, label in CASES:
        row = measure_batch(model, voice, args.text)
        row.update({"label": label, "model": model, "voice": voice,
                    "first_packet_s": measure_first_packet(model, voice, args.text)})
        print(json.dumps(row))
        results.append(row)
    worst = max(r["rtf"] for r in results)
    fp = [r["first_packet_s"] for r in results if r["first_packet_s"] is not None]
    summary = {"gate": "G3", "gate_rtf": args.gate_rtf, "worst_rtf": worst,
               "streams_faster_than_realtime": worst < args.gate_rtf,
               "recommend_streaming_variant":
                   "1.7B" if worst < args.gate_rtf else "0.6B-CustomVoice",
               "first_packet_latency": summarize_latencies(fp) if fp else None,
               "cases": results}
    print(json.dumps(summary, indent=2))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
