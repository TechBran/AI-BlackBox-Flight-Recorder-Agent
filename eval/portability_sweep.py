#!/usr/bin/env python3
"""Portability sweep — does the fix + AUTO-CALIBRATION generalize across
embedding models x rerankers? (Phase 4)

For each embedding model, embed the ground-truth corpus into its own scratch
store, DERIVE the floors from that model's own score geometry (cos_floor =
gold-cosine p10, the principled percentile noise_probe --calibrate uses;
rerank_floor = gold rerank-score p10 for the reranker), then evaluate:
  * baseline (production config) vs
  * FIXED-with-reranker (gated + preserve + rerank_floor + variable-k) vs
  * FIXED-no-reranker (gated + cos_floor + variable-k)
using ONLY the per-model auto-calibrated floors — no hand-tuned constants.
The claim under test: install -> pick model + reranker -> calibration derives
the floors -> correct results, on any box.

Run:  Orchestrator/venv/bin/python eval/portability_sweep.py
"""
from __future__ import annotations
import json
import statistics as st
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402
from eval.rank_sandbox import (  # noqa: E402
    corpus_store, rank, evaluate, DEFAULT, embed, GT,
)
from Orchestrator.fossils import extract_snapshot_content  # noqa: E402
from Orchestrator import rerank as RR  # noqa: E402

MODELS = ["gemini-embedding-2", "gemini-embedding-001", "qwen3-embedding-0.6b"]


def pctile(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))] if xs else 0.0


def calibrate_cos_floor(store, queries, qvecs, all_sids):
    """Derive cos_floor from THIS model's geometry: p10 of gold cosines (keep
    ~90% of true relevants), and report gold-vs-nongold separation."""
    gold_cos, nongold_cos = [], []
    for q in queries:
        qv = qvecs[q["query"]]
        cos = {sid: c for sid, c, _v in store.search_with_vectors(qv, len(all_sids), None)}
        gset = set(q["gold_sids"])
        for sid in q["gold_sids"]:
            if sid in cos:
                gold_cos.append(cos[sid])
        for sid in all_sids:
            if sid not in gset:
                nongold_cos.append(cos[sid])
    floor = round(pctile(gold_cos, 10), 3)
    return floor, {"gold_p10": round(pctile(gold_cos, 10), 3),
                   "gold_med": round(st.median(gold_cos), 3),
                   "nongold_p90": round(pctile(nongold_cos, 90), 3),
                   "nongold_med": round(st.median(nongold_cos), 3)}


def calibrate_rerank_floor(bodies, queries):
    """Reranker-only (embedding-model-independent): p10 of gold rerank scores."""
    if not RR.available():
        return None
    docs = {sid: extract_snapshot_content(t)[:4096] for sid, t in bodies.items()}
    gold_rr = []
    for q in queries:
        pool = list(q["gold_sids"])
        scores = RR.score(q["query"], [docs[s] for s in pool])
        if scores:
            gold_rr.extend(scores)
    return round(pctile(gold_rr, 10), 4) if gold_rr else None


def main():
    queries = [json.loads(l) for l in (GT / "queries.jsonl").read_text().splitlines() if l.strip()]
    print(f"[portability] {len(queries)} queries, rerank_available={RR.available()}\n")

    rr_floor = None
    print(f"{'model':<24} {'reranker':<8} {'config':<18} {'cosF':>5} {'rrF':>6} "
          f"{'prec':>5} {'recall':>6} {'noise/q':>7} {'deliv':>5}")
    print("-" * 100)
    out = []
    for slug in MODELS:
        try:
            store, bodies, all_sids = corpus_store(slug)
            qvecs = {q["query"]: np.asarray(embed(slug, [q["query"]], "query")[0], dtype=np.float32)
                     for q in queries}
        except Exception as e:
            print(f"{slug:<24} SKIPPED (embed/store failed: {str(e)[:60]})")
            continue
        cos_floor, sep = calibrate_cos_floor(store, queries, qvecs, all_sids)
        if rr_floor is None:
            rr_floor = calibrate_rerank_floor(bodies, queries)  # reranker-independent of model

        configs = [
            ("baseline-prod", "vertex", dict()),
            ("FIXED+rerank", "vertex", dict(keyword_mode="gated", keyword_gate=cos_floor,
                                            semantic_floor=cos_floor, rerank="preserve",
                                            rerank_floor=rr_floor, variable_k=True)),
            ("FIXED-noRerank", "none", dict(rerank="off", keyword_mode="gated",
                                            keyword_gate=cos_floor, semantic_floor=cos_floor,
                                            output_cos_floor=cos_floor, variable_k=True)),
        ]
        for name, rk, over in configs:
            cfg = dict(DEFAULT); cfg.update(over)
            m = evaluate(cfg, store, bodies, all_sids, queries, qvecs)
            out.append({"model": slug, "reranker": rk, "config": name,
                        "cos_floor": cos_floor, "rerank_floor": rr_floor, "sep": sep, **m})
            print(f"{slug:<24} {rk:<8} {name:<18} {cos_floor:>5.2f} "
                  f"{(rr_floor if rr_floor else 0):>6.3f} {m['precision']:>5.2f} "
                  f"{m['recall']:>6.2f} {m['noise_per_q']:>7.2f} {m['avg_delivered']:>5.1f}")
        print(f"    ^ {slug} geometry: gold med={sep['gold_med']} p10={sep['gold_p10']} | "
              f"nongold med={sep['nongold_med']} p90={sep['nongold_p90']}")

    (REPO / "eval" / "results" / "2026-07-07-portability.json").write_text(json.dumps(out, indent=2))
    print(f"\n[done] wrote eval/results/2026-07-07-portability.json")


if __name__ == "__main__":
    main()
