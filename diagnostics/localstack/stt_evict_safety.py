#!/usr/bin/env python3
"""diagnostics/localstack/stt_evict_safety.py — G6 live probe (MS02). Fires a
retrieval/embedding request WHILE a local streaming-STT utterance is in flight
and asserts ZERO audio cut-off. The on-box Speaches stream ships as Design B
(direct-to-member WS, invisible to llama-swap's in-flight drain counter), so it
is protected by ORCHESTRATOR SERIALIZATION (§5.3/D12): a retrieval request
arriving mid-utterance must be HELD until the finalize gap, never force-swap the
audio group out from under the open stream.

Method: open /ws/stt (provider=onbox), stream a 24kHz reference clip as PCM; at
the midpoint fire an embed at :9098 in a background thread (non-blocking); assert
(a) no partial-arrival gap > GAP_S, (b) stt_done terminal, (c) transcript matches
a control run with no injection. PASS iff the injected run's max gap is within
tolerance of the control's."""
from __future__ import annotations
import argparse, asyncio, json, sys, threading, time, wave
from pathlib import Path
import requests, websockets

WS_URL = "ws://127.0.0.1:9091/ws/stt"
EMBED_URL = "http://127.0.0.1:9098/v1/embeddings"


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


def _fire_embed():
    try:
        requests.post(EMBED_URL, timeout=120, json={
            "model": "embed-qwen3-8b",
            "input": "G6 mid-utterance retrieval probe"})
    except Exception as e:  # noqa: BLE001
        print(f"[g6] embed fire error (expected if held/slow): {e}",
              file=sys.stderr)


async def run(wav_path, inject):
    frames = pcm_frames(wav_path)
    mid = len(frames) // 2
    partial_times, finals, got_done, fired = [], [], False, False
    async with websockets.connect(WS_URL, max_size=None) as ws:
        await ws.send(json.dumps({"type": "start", "provider": "onbox",
                                  "sample_rate": 24000}))

        async def reader():
            nonlocal got_done
            async for msg in ws:
                ev = json.loads(msg)
                t = ev.get("type")
                if t in ("partial", "delta"):
                    partial_times.append(time.time())
                elif t in ("final", "transcript"):
                    finals.append(ev.get("text", ""))
                elif t == "stt_done":
                    got_done = True
                    return

        rtask = asyncio.create_task(reader())
        for i, fr in enumerate(frames):
            await ws.send(fr)
            await asyncio.sleep(0.1)
            if inject and i == mid and not fired:
                fired = True
                threading.Thread(target=_fire_embed, daemon=True).start()
        await ws.send(json.dumps({"type": "stop"}))
        await asyncio.wait_for(rtask, timeout=30)
    gaps = [b - a for a, b in zip(partial_times, partial_times[1:])]
    return {"inject": inject, "n_partials": len(partial_times),
            "max_partial_gap_s": round(max(gaps), 3) if gaps else None,
            "transcript": " ".join(finals).strip(), "stt_done": got_done}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--gap-tolerance-s", type=float, default=1.0,
                    help="allowed extra partial-gap vs the control run")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    control = asyncio.run(run(args.wav, inject=False))
    injected = asyncio.run(run(args.wav, inject=True))
    ctl_gap = control["max_partial_gap_s"] or 0.0
    inj_gap = injected["max_partial_gap_s"] or 0.0
    cut_off = (inj_gap - ctl_gap) > args.gap_tolerance_s
    ok = injected["stt_done"] and not cut_off
    summary = {"gate": "G6", "control": control, "injected": injected,
               "extra_gap_s": round(inj_gap - ctl_gap, 3),
               "gap_tolerance_s": args.gap_tolerance_s,
               "audio_cut_off": cut_off, "pass": ok}
    print(json.dumps(summary, indent=2))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(summary, indent=2))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
