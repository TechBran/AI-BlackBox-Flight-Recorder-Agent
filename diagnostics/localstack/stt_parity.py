#!/usr/bin/env python3
"""diagnostics/localstack/stt_parity.py — G4 live probe (MS02). Streams the
same 24kHz reference clip through /ws/stt for one provider and records
first-partial latency, final-transcript latency, and the transcript. Run once
per provider and diff the two JSONs:
  --provider onbox : on-box Speaches (:9098, the new localstack STT)
  --provider local : the gemma-box custom-server Speaches (today's path)
The full Portal + Android mic-flow parity is a manual device step (house rule).
Reference clip must be 24kHz / 16-bit / mono PCM WAV (see the G4 runbook)."""
from __future__ import annotations
import argparse, asyncio, json, sys, time, wave
from pathlib import Path
import websockets

WS_URL = "ws://127.0.0.1:9091/ws/stt"


def pcm_frames(wav_path, frame_ms=100):
    with wave.open(str(wav_path), "rb") as w:
        assert w.getframerate() == 24000 and w.getsampwidth() == 2 \
            and w.getnchannels() == 1, "clip must be 24kHz/16-bit/mono PCM"
        n = int(w.getframerate() * frame_ms / 1000)
        frames = []
        while True:
            data = w.readframes(n)
            if not data:
                break
            frames.append(data)
        return frames


async def run(provider, wav_path):
    frames = pcm_frames(wav_path)
    first_partial = final_at = None
    finals, got_done = [], False
    async with websockets.connect(WS_URL, max_size=None) as ws:
        await ws.send(json.dumps({"type": "start", "provider": provider,
                                  "sample_rate": 24000}))
        t0 = time.time()

        async def reader():
            nonlocal first_partial, final_at, got_done
            async for msg in ws:
                ev = json.loads(msg)
                t = ev.get("type")
                if t in ("partial", "delta") and first_partial is None:
                    first_partial = time.time() - t0
                elif t in ("final", "transcript"):
                    finals.append(ev.get("text", ""))
                    final_at = time.time() - t0
                elif t == "stt_done":
                    got_done = True
                    return

        rtask = asyncio.create_task(reader())
        for fr in frames:
            await ws.send(fr)
            await asyncio.sleep(0.1)  # ~real-time pacing
        await ws.send(json.dumps({"type": "stop"}))
        await asyncio.wait_for(rtask, timeout=30)
    return {"provider": provider,
            "first_partial_s": round(first_partial, 3) if first_partial is not None else None,
            "final_s": round(final_at, 3) if final_at is not None else None,
            "transcript": " ".join(finals).strip(), "stt_done": got_done}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, choices=["onbox", "local"])
    ap.add_argument("--wav", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    out = asyncio.run(run(args.provider, args.wav))
    print(json.dumps(out, indent=2))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
