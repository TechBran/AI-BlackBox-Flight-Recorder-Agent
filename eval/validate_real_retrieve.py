#!/usr/bin/env python3
"""M5 acceptance gate: drive the REAL Orchestrator.retrieval.retrieve() over the
ground-truth corpus, baseline (flags off) vs FIXED (flags on), and measure true
precision/recall. Unlike eval/rank_sandbox.py (a re-implementation), this runs
the ACTUAL production function — the only redirection is the two seams that
would otherwise read the live volume (the keyword lane + the rerank passage
decode), pointed at the corpus. Uses the store= / query_vector= eval seam.

Run:  Orchestrator/venv/bin/python eval/validate_real_retrieve.py
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.chdir(REPO)
sys.path.insert(0, str(REPO))
for ln in (REPO / ".env").read_text().splitlines() if (REPO / ".env").exists() else []:
    if "=" in ln and not ln.strip().startswith("#"):
        k, _, v = ln.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import numpy as np  # noqa: E402
import Orchestrator.retrieval as retrieval  # noqa: E402
from Orchestrator.config import CFG  # noqa: E402
from eval.rank_sandbox import corpus_store, keyword_rank, embed, GT  # noqa: E402

SLUG = "gemini-embedding-2"


def pin(**kv):
    if not CFG.has_section("retrieval"):
        CFG.add_section("retrieval")
    for k, v in kv.items():
        CFG.set("retrieval", k, str(v))


def evaluate(label, queries, store, qvecs, k=10):
    tot = {"prec": 0.0, "recall": 0.0, "noise": 0.0, "deliv": 0.0, "decoy": 0.0}
    for q in queries:
        gold = set(q["gold_sids"])
        hardneg = set(q["hard_negative_sids"])
        res = retrieval.retrieve(q["query"], operator="", k=k, store=store,
                                 query_vector=list(qvecs[q["query"]]))
        ids = [sid for sid, _ in res]
        rel = [s for s in ids if s in gold]
        tot["prec"] += len(rel) / len(ids) if ids else 0
        tot["recall"] += len(rel) / len(gold) if gold else 0
        tot["noise"] += len([s for s in ids if s not in gold])
        tot["decoy"] += len([s for s in ids if s in hardneg])
        tot["deliv"] += len(ids)
    n = len(queries)
    print(f"  {label:<34} prec={tot['prec']/n:.2f} recall={tot['recall']/n:.2f} "
          f"noise/q={tot['noise']/n:.2f} decoy/q={tot['decoy']/n:.2f} deliv={tot['deliv']/n:.1f}")
    return {kk: tot[kk] / n for kk in tot}


def main():
    queries = [json.loads(l) for l in (GT / "queries.jsonl").read_text().splitlines() if l.strip()]
    store, bodies, all_sids = corpus_store(SLUG)
    qvecs = {q["query"]: np.asarray(embed(SLUG, [q["query"]], "query")[0], dtype=np.float32)
             for q in queries}

    # Redirect ONLY the two live-volume seams to the corpus (everything else is
    # the real retrieve()). index carries the sid so decode can resolve it.
    idx = {sid: {"operator": "test", "timestamp": "2026-06-01T00:00:00Z", "_sid": sid}
           for sid in all_sids}
    retrieval.load_snapshot_index = lambda: idx
    retrieval.keyword_retrieve_ids_for_operator = lambda vol, qy, n, op: keyword_rank(bodies, qy, n)
    retrieval._decode_snapshot_text = lambda meta: bodies.get(meta.get("_sid")) if meta else None
    retrieval._age_days = lambda ts, now: 0.0  # recency off for a clean read

    print(f"[M5] REAL retrieve() over {len(all_sids)} GT snapshots, {len(queries)} queries, "
          f"rerank_enabled(live)={__import__('Orchestrator.rerank', fromlist=['is_enabled']).is_enabled()}\n")

    saved = {o: (CFG.get("retrieval", o) if CFG.has_option("retrieval", o) else None)
             for o in ("keyword_mode", "rerank_relevance", "rerank_floor",
                       "output_cos_floor", "min_results")}
    try:
        pin(keyword_mode="fused", rerank_relevance="rankspace", rerank_floor="0.0",
            output_cos_floor="0.0", min_results="0")
        base = evaluate("baseline (flags off = production)", queries, store, qvecs)

        pin(keyword_mode="gated", rerank_relevance="preserve", rerank_floor="0.03",
            output_cos_floor="0.62", min_results="1")
        fixed = evaluate("FIXED (gated+preserve+floors)", queries, store, qvecs)

        pin(rerank_floor="0.083")
        fixed2 = evaluate("FIXED (rerank_floor=0.083)", queries, store, qvecs)
    finally:
        for o, prev in saved.items():
            if prev is None:
                CFG.remove_option("retrieval", o)
            else:
                CFG.set("retrieval", o, prev)

    (REPO / "eval" / "results" / "2026-07-07-m5-real-retrieve.json").write_text(
        json.dumps({"baseline": base, "fixed_0.03": fixed, "fixed_0.083": fixed2}, indent=2))
    print(f"\n[done] baseline prec {base['prec']:.2f}/rec {base['recall']:.2f} -> "
          f"fixed prec {fixed['prec']:.2f}/rec {fixed['recall']:.2f}. "
          f"wrote eval/results/2026-07-07-m5-real-retrieve.json")


if __name__ == "__main__":
    main()
