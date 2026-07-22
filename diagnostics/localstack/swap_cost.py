#!/usr/bin/env python3
"""diagnostics/localstack/swap_cost.py — G5 live probe (MS02). Times TWO kinds
of swap through the llama-swap front door:

--scope cross (default): the cross-group first-interaction stall in BOTH
directions by alternating a retrieval-group request (embed) and an audio-group
request (TTS), forcing an exclusive evict+load each turn:
  audio->retrieval : first embed after a TTS = evict(audio)+load(embed-8b)
                     — expect ~6-10s (§5.2/D9). Sequential retrieval (D13) loads
                     ONLY the demanded member (the embedder), not both.
  retrieval->audio : first TTS after an embed = evict(retrieval)+load(speaches+qwen-tts)
                     — expect ~5-8s

--scope intra (D13, NEW): the per-search intra-group swap the sequential
retrieval group pays on EVERY search — embed the query (embedder resident) ->
evict -> cold-load the 8B reranker -> score:
  embed->rerank : first rerank after an embed = evict(embed-8b)+load(rerank-qwen3-8b)
                  — expect ~6-12s (the accepted per-search cost). Corroborates
                  the G2 --measure-swap number.

Run with --cache warm, then again with --cache cold after the caller drops the
page cache: sudo sh -c 'echo 3 > /proc/sys/vm/drop_caches'."""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
import requests

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from diagnostics.localstack.metrics import summarize_latencies  # noqa: E402

BASE = "http://127.0.0.1:9098/v1"


def one_embed():
    t0 = time.time()
    requests.post(f"{BASE}/embeddings", timeout=120, json={
        "model": "embed-qwen3-8b", "input": "cross-group swap probe"}
    ).raise_for_status()
    return time.time() - t0


def one_tts():
    t0 = time.time()
    requests.post(f"{BASE}/audio/speech", timeout=120, json={
        "model": "qwen-tts", "input": "swap probe", "voice": "Vivian",
        "response_format": "wav"}).raise_for_status()
    return time.time() - t0


def one_rerank():
    # D13 intra-group: triggers evict(embed-8b)+load(rerank-qwen3-8b) when the
    # embedder is the currently-resident retrieval member.
    t0 = time.time()
    requests.post(f"{BASE}/rerank", timeout=120, json={
        "model": "rerank-qwen3-8b", "query": "intra-group swap probe",
        "documents": ["a relevant passage about model swapping",
                      "an unrelated decoy about bananas"]}).raise_for_status()
    return time.time() - t0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=5)
    ap.add_argument("--scope", choices=["cross", "intra"], default="cross")
    ap.add_argument("--cache", choices=["warm", "cold"], default="warm")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    if args.scope == "intra":
        # D13: retrieval group is sequential; prime with an embed each iter so
        # the following rerank pays the intra-group evict+cold-load swap.
        e2r = []
        for _ in range(args.iters):
            one_embed()               # embedder resident
            e2r.append(one_rerank())  # evict embedder + cold-load the 8B reranker
        summary = {"gate": "G5", "scope": "intra", "cache": args.cache,
                   "iters": args.iters,
                   "embed_to_rerank_s": summarize_latencies(e2r)}
    else:
        one_tts()  # prime: land in the audio group so the first embed swaps
        a2r, r2a = [], []
        for _ in range(args.iters):
            a2r.append(one_embed())
            r2a.append(one_tts())
        summary = {"gate": "G5", "scope": "cross", "cache": args.cache,
                   "iters": args.iters,
                   "audio_to_retrieval_s": summarize_latencies(a2r),
                   "retrieval_to_audio_s": summarize_latencies(r2a)}
    print(json.dumps(summary, indent=2))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
