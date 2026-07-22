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
(a) no partial-arrival stall > tolerance, (b) no cut-off — a trailing/total
truncation shows up as FEWER partials, so the injected run must retain the
control's partial count (and never collapse to <2 partials), (c) stt_done
terminal, (d) transcript matches the control run with no injection. PASS iff all
hold. The pure decision math (partial-gap + cut-off + transcript equality) lives
in the helpers below and is unit-tested in
Orchestrator/tests/test_stt_evict_safety.py — no GPU needed."""
from __future__ import annotations
import argparse, asyncio, contextlib, io, json, sys, threading, time, wave
from pathlib import Path
import requests, websockets

from diagnostics.localstack.metrics import parse_wav_header

WS_URL = "ws://127.0.0.1:9091/ws/stt"
EMBED_URL = "http://127.0.0.1:9098/v1/embeddings"

# The frame size and the send-loop pacing MUST stay in lockstep for real-time
# streaming — one constant drives both so they can never desync.
FRAME_MS = 100
FRAME_S = FRAME_MS / 1000.0
STT_DONE_TIMEOUT_S = 30
# A full clip streamed end-to-end must yield at least this many partials; fewer
# means the stream was cut off before it could emit measurable progress.
MIN_PARTIALS = 2
# The injected run must retain at least this fraction of the control's partial
# count — a trailing/total truncation drops partials, it does not widen a gap.
PARTIAL_RETENTION_FRAC = 0.5


def pcm_frames(wav_path, frame_ms=FRAME_MS):
    raw = Path(wav_path).read_bytes()
    hdr = parse_wav_header(raw)  # shared RIFF-walk: single source for format
    assert hdr["sample_rate"] == 24000 and hdr["bits"] == 16 \
        and hdr["channels"] == 1, "clip must be 24kHz/16-bit/mono PCM"
    with wave.open(io.BytesIO(raw), "rb") as w:
        n = int(w.getframerate() * frame_ms / 1000)
        frames = []
        while True:
            data = w.readframes(n)
            if not data:
                break
            frames.append(data)
        return frames


def partial_gaps(partial_times):
    """Inter-partial arrival gaps (seconds) from arrival timestamps."""
    return [b - a for a, b in zip(partial_times, partial_times[1:])]


def max_gap(partial_times):
    """Largest inter-partial gap in seconds, or None when <2 partials arrived
    (no gap is measurable). None is a SIGNAL, not a zero — the decision layer
    treats it as a cut-off, never coerces it to 0.0."""
    gaps = partial_gaps(partial_times)
    return round(max(gaps), 3) if gaps else None


def normalize_transcript(text):
    """Case/whitespace-insensitive transcript form for control-vs-injected
    equality."""
    return " ".join((text or "").lower().split())


def decide(control, injected, gap_tolerance_s, min_partials=MIN_PARTIALS,
           partial_retention_frac=PARTIAL_RETENTION_FRAC):
    """Pure PASS/FAIL decision for the G6 probe. Detects audio cut-off three
    ways — because a trailing/total truncation produces FEWER partials, not a
    wider inter-partial gap — and requires transcript equality with the control.

    - insufficient_partials: injected run has <min_partials (0/1 partials means
      an empty gap list -> a None max gap; the old code coerced that to 0.0 and
      green-lit total silence — this closes that hole).
    - partial_count_drop: injected retained <retention_frac of control partials.
    - inter_partial_stall: a measured gap exceeded control + tolerance.

    Returns a dict merged into the summary; ``pass`` requires stt_done AND no
    cut-off AND transcript match."""
    ctl_n = control.get("n_partials", 0)
    inj_n = injected.get("n_partials", 0)
    ctl_gap = control.get("max_partial_gap_s")
    inj_gap = injected.get("max_partial_gap_s")

    reasons = []
    if inj_n < min_partials:
        reasons.append("insufficient_partials")
    if ctl_n and inj_n < ctl_n * partial_retention_frac:
        reasons.append("partial_count_drop")
    extra_gap_s = None
    if inj_gap is not None:
        extra_gap_s = round(inj_gap - (ctl_gap or 0.0), 3)
        if extra_gap_s > gap_tolerance_s:
            reasons.append("inter_partial_stall")
    cut_off = bool(reasons)

    transcript_match = (normalize_transcript(control.get("transcript")) ==
                        normalize_transcript(injected.get("transcript")))

    ok = bool(injected.get("stt_done") and not cut_off and transcript_match)
    return {"audio_cut_off": cut_off, "cut_off_reasons": reasons,
            "extra_gap_s": extra_gap_s, "transcript_match": transcript_match,
            "pass": ok}


def _fire_embed():
    try:
        requests.post(EMBED_URL, timeout=120, json={
            "model": "embed-qwen3-8b",
            "input": "G6 mid-utterance retrieval probe"})
    except Exception as e:  # noqa: BLE001
        print(f"[g6] embed fire error (expected if held/slow): {e}",
              file=sys.stderr)


async def run(wav_path, inject):
    frames = pcm_frames(wav_path, frame_ms=FRAME_MS)
    mid = len(frames) // 2
    partial_times, finals, got_done = [], [], False
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
            await asyncio.sleep(FRAME_S)
            if inject and i == mid:
                threading.Thread(target=_fire_embed, daemon=True).start()
        await ws.send(json.dumps({"type": "stop"}))
        # A stream that never terminates must yield a clean FAIL summary, not an
        # uncaught TimeoutError traceback: cancel the reader, leave got_done False.
        try:
            await asyncio.wait_for(rtask, timeout=STT_DONE_TIMEOUT_S)
        except asyncio.TimeoutError:
            rtask.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await rtask
            print(f"[g6] stt_done never arrived within {STT_DONE_TIMEOUT_S}s "
                  f"(inject={inject}) — reporting FAIL", file=sys.stderr)
    return {"inject": inject, "n_partials": len(partial_times),
            "max_partial_gap_s": max_gap(partial_times),
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
    verdict = decide(control, injected, args.gap_tolerance_s)
    summary = {"gate": "G6", "control": control, "injected": injected,
               "gap_tolerance_s": args.gap_tolerance_s, **verdict}
    print(json.dumps(summary, indent=2))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(summary, indent=2))
    return 0 if verdict["pass"] else 2


if __name__ == "__main__":
    sys.exit(main())
