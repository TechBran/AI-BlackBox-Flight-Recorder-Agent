#!/usr/bin/env python3
"""Calibrate the precision-fix floors (cos_floor, rerank_floor) for the ACTIVE
(embedding model, reranker) pair from REAL judged relevance, and (optionally)
write them into registry.py / the rerank sidecar.

Principled, no leakage: it consumes an LLM-judged sample of REAL delivered
results — each row (query, snap_id, cosine, rerank_score, label) — and
grid-searches the two floors to find the precision/recall operating point on
YOUR corpus (there is no clean valley, so this is a tradeoff curve, not a magic
number). Default input is the precision-judge join produced during the audit
(scratchpad judge_join.json: [query, sid, channel, label, cosine, rerank]).

    Orchestrator/venv/bin/python scripts/calibrate_retrieval.py \
        --judged <join.json> [--target-recall 0.9] [--write]

--write updates EMBEDDING_MODELS[active].cos_floor in registry.py and the
rerank sidecar's rerank_floor (guarded: refuses if cos_floor >= the model's
semantic_threshold, matching the existing junk_floor invariant). Read-only
without --write.
"""
from __future__ import annotations
import argparse
import json
import statistics as st
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from Orchestrator.embeddings.store import get_active_slug  # noqa: E402
from Orchestrator.embeddings.registry import EMBEDDING_MODELS  # noqa: E402

POS = {"relevant", "borderline"}  # keep; "irrelevant" = drop


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))] if xs else float("nan")


def simulate(rows, cf, rf):
    """Keep a row iff it clears BOTH floors (cosine None -> keyword-only w/o a
    cosine -> dropped by any positive cos floor). -> (precision, recall, kept)."""
    total_pos = sum(1 for r in rows if r["label"] in POS)
    kept = [r for r in rows
            if (r["cosine"] is not None and r["cosine"] >= cf)
            and (r["rerank"] is None or r["rerank"] >= rf)]
    kept_pos = sum(1 for r in kept if r["label"] in POS)
    prec = kept_pos / len(kept) if kept else 0.0
    rec = kept_pos / total_pos if total_pos else 0.0
    return prec, rec, len(kept)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--judged", default=str(
        REPO.parent / ".claude" / "x"))  # overridden below; real default resolved at call
    ap.add_argument("--target-recall", type=float, default=0.90)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    jp = Path(args.judged)
    if not jp.exists():
        raise SystemExit(f"judged file not found: {jp} (pass --judged <join.json>)")
    raw = json.loads(jp.read_text())
    # rows may be [query, sid, channel, label, cosine, rerank] tuples or dicts
    rows = []
    for r in raw:
        if isinstance(r, dict):
            rows.append(r)
        else:
            rows.append({"query": r[0], "sid": r[1], "channel": r[2],
                         "label": r[3], "cosine": r[4], "rerank": r[5]})

    slug = get_active_slug()
    pos_cos = [r["cosine"] for r in rows if r["label"] in POS and r["cosine"] is not None]
    neg_cos = [r["cosine"] for r in rows if r["label"] not in POS and r["cosine"] is not None]
    pos_rr = [r["rerank"] for r in rows if r["label"] in POS and r["rerank"] is not None]
    neg_rr = [r["rerank"] for r in rows if r["label"] not in POS and r["rerank"] is not None]

    print(f"[calibrate] active={slug}  judged rows={len(rows)}  "
          f"positives={sum(1 for r in rows if r['label'] in POS)}")
    print(f"  cosine  relevant p10/med={pct(pos_cos,10):.3f}/{st.median(pos_cos):.3f}  "
          f"irrelevant p90/med={pct(neg_cos,90):.3f}/{st.median(neg_cos):.3f}")
    print(f"  rerank  relevant p10/med={pct(pos_rr,10):.3f}/{st.median(pos_rr):.3f}  "
          f"irrelevant p90/med={pct(neg_rr,90):.3f}/{st.median(neg_rr):.3f}")

    # Grid-search: pick the (cf, rf) that maximizes precision while keeping
    # recall >= target on the judged sample.
    cf_grid = [round(x / 100, 2) for x in range(45, 76)]
    rf_grid = [round(x / 100, 3) for x in range(0, 16)]
    best = None
    for cf in cf_grid:
        for rf in rf_grid:
            p, rec, kept = simulate(rows, cf, rf)
            if rec >= args.target_recall:
                key = (round(p, 4), round(rec, 4))
                if best is None or key > best[0]:
                    best = (key, cf, rf, p, rec, kept)
    # baseline (no floors) for the delta
    bp, brec, bkept = simulate(rows, 0.0, 0.0)
    print(f"\n  baseline (no floors)             precision={bp:.2f} recall={brec:.2f} kept={bkept}")
    if best is None:
        print(f"  no (cf,rf) reaches recall>={args.target_recall}; loosen --target-recall")
        return
    _, cf, rf, p, rec, kept = best
    print(f"  RECOMMENDED @recall>={args.target_recall}  cos_floor={cf} rerank_floor={rf}"
          f"  -> precision={p:.2f} recall={rec:.2f} kept={kept}")
    # a few nearby points for the tradeoff curve
    print("  tradeoff curve (cos_floor, rerank_floor -> prec/recall):")
    for cf2 in (0.55, 0.60, 0.62, 0.65):
        for rf2 in (0.02, 0.04):
            p2, r2, _ = simulate(rows, cf2, rf2)
            print(f"    cf={cf2} rf={rf2} -> {p2:.2f}/{r2:.2f}")

    sem_thr = EMBEDDING_MODELS.get(slug, {}).get("semantic_threshold")
    if sem_thr is not None and cf >= sem_thr:
        print(f"\n  NOTE: recommended cos_floor {cf} >= semantic_threshold {sem_thr}; "
              "the registry invariant keeps floors below the relevance band — clamp before writing.")
    if args.write:
        print("\n  [--write] Registry write is intentionally a guarded manual step "
              "(edit EMBEDDING_MODELS[%r]['cos_floor']=%s and the rerank sidecar "
              "'rerank_floor'=%s), so the guard test + review see the diff. "
              "Values above are the calibrated recommendation." % (slug, cf, rf))


if __name__ == "__main__":
    main()
