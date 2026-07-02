#!/usr/bin/env python3
"""Miss decomposition probe for a bench arm (WI-6 Phase A / M4b).

For every labeled row an arm MISSED (gold absent from its cached top-10 in
eval/bench_cache.json), re-score the query against the arm's RAW store and
report where the gold actually sat. Separates the two failure modes of the
whole-snapshot baseline:

  * gold raw rank <=10 / <=40  -> the embedding FOUND it; the pipeline
    (recency-in-rank-space after RRF, MMR, keyword displacement) pushed it
    out of the final top-10 ("pipeline displacement").
  * gold raw rank beyond ~200  -> genuine embedding miss (query too generic,
    or content lost to the 10k embed truncation).

Both an UNSCOPED and an operator-SCOPED (allowed_ids = the row's operator,
mirroring retrieve()'s candidate generation) raw ranking are reported, plus a
junk-floor check (gold cosine vs [retrieval].junk_floor) to rule the floor out
as a confound.

Note: the bench caches result IDS only, so query vectors are re-embedded here
via the arm's own provider (identical path to run_bench: get_provider(slug)
.embed(purpose="query"), which applies the Ollama query_instruction prefix
where relevant). ~330 gemini query embeds ~= pennies; qwen is local/free.

Run (from the repo root):
    Orchestrator/venv/bin/python eval/probe_misses.py [--arm gemini2-semantic]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.chdir(REPO)  # Orchestrator.config reads config.ini relative to CWD
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

from Orchestrator.config import CFG  # noqa: E402
from Orchestrator.embeddings.providers import get_provider  # noqa: E402
from Orchestrator.embeddings.store import get_store  # noqa: E402
from Orchestrator.fossils import load_snapshot_index  # noqa: E402
from eval.run_bench import ARMS, K, LABELED, CACHE_PATH, row_key, EMBED_BATCH  # noqa: E402

BUCKETS = ((10, "<=10"), (40, "<=40 (candidate window)"), (200, "41-200"))


def bucket_of(rank: int | None) -> str:
    if rank is None:
        return "not in store"
    for limit, label in BUCKETS:
        if rank <= limit:
            return label
    return ">200 (embedding miss)"


def raw_rank(store, qv_np, gold: str, allowed_ids=None):
    """Gold's 1-based rank + cosine in a raw full-store search (no pipeline)."""
    res = store.search(qv_np, store.count, allowed_ids)
    for i, (sid, cos) in enumerate(res):
        if sid == gold:
            return i + 1, cos
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="gemini2-semantic",
                    choices=[a["name"] for a in ARMS])
    args = ap.parse_args()
    arm = next(a for a in ARMS if a["name"] == args.arm)

    rows = [json.loads(ln) for ln in LABELED.read_text().splitlines() if ln.strip()]
    cache = json.loads(CACHE_PATH.read_text())
    store = get_store(arm["slug"])
    store_ids = store.ids()
    index = load_snapshot_index()
    junk_floor = CFG.getfloat("retrieval", "junk_floor", fallback=0.40)

    misses = []
    for r in rows:
        key = row_key(arm["name"], r)
        if key not in cache:
            raise SystemExit(f"row not in bench cache ({key}) — run eval/run_bench.py first")
        if r["gold_snap_id"] not in cache[key]:
            misses.append(r)
    covered_misses = [r for r in misses if r["gold_snap_id"] in store_ids]
    print(f"[probe] arm={arm['name']} rows={len(rows)} top-{K} misses={len(misses)} "
          f"(gold-in-store: {len(covered_misses)}; uncovered: "
          f"{len(misses) - len(covered_misses)})")

    provider = get_provider(arm["slug"])
    vecs = []
    for i in range(0, len(covered_misses), EMBED_BATCH):
        batch = [r["query"] for r in covered_misses[i:i + EMBED_BATCH]]
        vecs.extend(asyncio.run(provider.embed(batch, "query")))

    unscoped, scoped, below_floor = Counter(), Counter(), 0
    for r, qv in zip(covered_misses, vecs):
        q = np.asarray(qv, dtype=np.float32)
        rank_u, cos = raw_rank(store, q, r["gold_snap_id"])
        allowed = None
        if r["operator"] and r["operator"] != "system":
            allowed = {sid for sid, m in index.items()
                       if m.get("operator") == r["operator"]}
        rank_s, _ = raw_rank(store, q, r["gold_snap_id"], allowed)
        unscoped[bucket_of(rank_u)] += 1
        scoped[bucket_of(rank_s)] += 1
        if cos is not None and cos < junk_floor:
            below_floor += 1

    n = len(covered_misses)

    def report(name: str, c: Counter):
        print(f"\n[{name}] gold raw rank distribution over {n} covered misses:")
        cum = 0
        for label in ("<=10", "<=40 (candidate window)", "41-200",
                      ">200 (embedding miss)", "not in store"):
            cnt = c.get(label, 0)
            cum += cnt
            print(f"  {label:26s} {cnt:4d}  ({cnt / n:6.1%})   cumulative {cum / n:6.1%}")

    report("UNSCOPED raw store search", unscoped)
    report("operator-SCOPED raw search (mirrors retrieve() candidates)", scoped)
    print(f"\n[junk floor] golds with cosine < {junk_floor}: {below_floor}/{n} "
          f"({'no confound' if below_floor == 0 else 'CONFOUND — investigate'})")
    print("\nReading: '<=10'/'<=40' misses were FOUND by the embedding and lost in the")
    print("pipeline (recency boost up to 0.05 vs the 1/60-1/99 ~ 0.00657 RRF span,")
    print("MMR, keyword displacement); '>200' are genuine embedding misses.")


if __name__ == "__main__":
    main()
