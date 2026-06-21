#!/usr/bin/env python3
"""Measure a sensible semantic_threshold for the ACTIVE embedding model.

Embeds a fixed query set, runs store.search, and reports the score
distribution of the top-k so an operator can pick a floor that keeps strong
matches well clear of the cut. Read-only; no store writes.
"""
import sys, statistics
sys.path.insert(0, ".")
from Orchestrator.embeddings import search as S
from Orchestrator.embeddings.store import get_active_slug

QUERIES = [
    "embeddings model switch reembed snapshot volume",
    "nav2 costmap inflation tuning robot",
    "voice agent streaming speech to text",
    "control phone on-device gemma delegate",
    "google workspace docs sheets integration",
    "memory leak restart oom recycle",
    "android audiorecord release race crash",
    "tts voice catalog openai gemini elevenlabs",
    "tailscale security perimeter operator auth",
    "checkpoint mint auto embedding searchable",
]

def main():
    slug = get_active_slug()
    store = S.get_active_store()
    print(f"active model: {slug}  store.count={store.count}")
    all_top = []
    worst_top10 = []
    for q in QUERIES:
        qv = S.generate_embedding_sync(q, purpose="query")
        if not qv:
            print(f"  EMBED FAILED: {q!r}")
            continue
        hits = store.search(qv, 10, None)
        scores = [s for _, s in hits]
        all_top += scores
        if scores:
            worst_top10.append(min(scores))
        print(f"  {q[:40]:40} top1={scores[0]:.4f} top10={scores[-1]:.4f}")
    if all_top:
        print(f"\nAll top-10 scores: min={min(all_top):.4f} "
              f"p10={statistics.quantiles(all_top, n=10)[0]:.4f} "
              f"median={statistics.median(all_top):.4f} max={max(all_top):.4f}")
        print(f"Worst per-query top-10 floor: min={min(worst_top10):.4f}")
        print(f"SUGGESTED threshold = worst_top10_min - 0.05 = {min(worst_top10) - 0.05:.4f} "
              f"(keep strong matches clear of the cut; never above p10)")

if __name__ == "__main__":
    main()
