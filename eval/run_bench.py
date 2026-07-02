#!/usr/bin/env python3
"""Retrieval bench through the FULL retrieve() pipeline (WI-6 Phase A / M4).

For every row of eval/labeled_set.jsonl, runs
    retrieve(query, operator=row.operator, k=10,
             store=<arm store>, query_vector=<arm-model query embed>,
             include_keyword=<arm setting>)
so each arm is scored by the production ranking core (junk floor -> RRF ->
recency tie-break -> MMR), not a side-channel cosine loop. The store and
query_vector eval seams (M4 Task 4.1/4.3) address the arm's candidate store
and embed the query with THAT arm's model (retrieve() would otherwise embed
with the ACTIVE model — wrong for a non-active arm like qwen).

Default arms (whole-snapshot baselines — these numbers GATE the M6 chunk swap):
  * gemini2-hybrid    — gemini-embedding-2 ACTIVE store, hybrid (keyword on)
  * gemini2-semantic  — same store, semantic-only (the phone-profile shape)
  * qwen06-semantic   — qwen3-embedding-0.6b store, semantic-only

Coverage: a gold snapshot missing from an arm's store ("gold-uncovered") can
never be retrieved by that arm. The qwen store is known to be ~600 snapshots
behind the index — we do NOT heal it here (POST /embeddings/migrate would
auto-cutover the active model: FORBIDDEN). Metrics are reported BOTH ways:
overall (uncovered rows counted as misses) and covered-only, clearly labeled.

Everything is READ-ONLY against the ledger/stores and artifacts-cached per
(arm, query, operator) in eval/bench_cache.json so re-runs are cheap.

Run (from the repo root):
    Orchestrator/venv/bin/python eval/run_bench.py [--out-date 2026-07-02]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.chdir(REPO)  # Orchestrator.config reads config.ini relative to CWD
sys.path.insert(0, str(REPO))

from Orchestrator.retrieval import retrieve  # noqa: E402
from Orchestrator.embeddings.providers import get_provider  # noqa: E402
from Orchestrator.embeddings.store import get_store, get_active_slug  # noqa: E402
from Orchestrator.fossils import load_snapshot_index  # noqa: E402

LABELED = REPO / "eval" / "labeled_set.jsonl"
CACHE_PATH = REPO / "eval" / "bench_cache.json"
RESULTS_DIR = REPO / "eval" / "results"
K = 10
EMBED_BATCH = 16

ARMS = [
    {"name": "gemini2-hybrid", "slug": "gemini-embedding-2", "include_keyword": True},
    {"name": "gemini2-semantic", "slug": "gemini-embedding-2", "include_keyword": False},
    {"name": "qwen06-semantic", "slug": "qwen3-embedding-0.6b", "include_keyword": False},
]


def row_key(arm_name: str, row: dict) -> str:
    h = hashlib.sha1(f"{row['query']}\x00{row['operator']}".encode()).hexdigest()
    return f"{arm_name}|{h}|k={K}"


def load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text())
    return {}


def save_cache(cache: dict) -> None:
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache))
    os.replace(tmp, CACHE_PATH)


def embed_queries(slug: str, texts: list) -> list:
    """Arm-model query vectors via the production provider layer (its retry,
    truncation, and the Ollama query_instruction prefix all apply)."""
    provider = get_provider(slug)
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        out.extend(asyncio.run(provider.embed(batch, "query")))
    return out


def run_arm(arm: dict, rows: list, cache: dict) -> dict:
    """-> {row_key: [snap_id, ...]} for every labeled row, filling the cache."""
    store = get_store(arm["slug"])
    pending = [r for r in rows if row_key(arm["name"], r) not in cache]
    print(f"[{arm['name']}] store={arm['slug']} rows={len(rows)} "
          f"cached={len(rows) - len(pending)} to-run={len(pending)}")
    if pending:
        t0 = time.time()
        vecs = embed_queries(arm["slug"], [r["query"] for r in pending])
        print(f"[{arm['name']}] embedded {len(vecs)} queries in {time.time() - t0:.1f}s")
        for n, (r, qv) in enumerate(zip(pending, vecs), 1):
            results = retrieve(
                r["query"], operator=r["operator"], k=K,
                include_keyword=arm["include_keyword"],
                store=store, query_vector=qv,
            )
            cache[row_key(arm["name"], r)] = [sid for sid, _score in results]
            if n % 25 == 0 or n == len(pending):
                save_cache(cache)
                print(f"[{arm['name']}] {n}/{len(pending)} "
                      f"({(time.time() - t0) / n:.2f}s/row)")
    return {row_key(arm["name"], r): cache[row_key(arm["name"], r)] for r in rows}


def metrics(rows: list, ranked: dict, arm_name: str, covered_ids: set) -> dict:
    """recall@1/3/5/10 + MRR, overall and covered-only, stratified."""
    def rank_of(r):
        ids = ranked[row_key(arm_name, r)]
        return ids.index(r["gold_snap_id"]) + 1 if r["gold_snap_id"] in ids else None

    def agg(subset):
        n = len(subset)
        if n == 0:
            return {"n": 0}
        ranks = [rank_of(r) for r in subset]
        out = {"n": n}
        for k in (1, 3, 5, 10):
            out[f"recall@{k}"] = round(sum(1 for x in ranks if x and x <= k) / n, 4)
        out["mrr"] = round(sum(1.0 / x for x in ranks if x) / n, 4)
        return out

    covered_rows = [r for r in rows if r["gold_snap_id"] in covered_ids]
    strata = {}
    for field in ("length_band", "position_third"):
        strata[field] = {
            str(val): agg([r for r in rows if r.get(field) == val])
            for val in sorted({r.get(field) for r in rows}, key=str)
        }
    holdout = [r for r in rows if r["source"] == "holdout"]
    return {
        "overall": agg(rows),                     # uncovered gold counted as miss
        "covered_only": agg(covered_rows),        # rows whose gold IS in the store
        "gold_uncovered": len(rows) - len(covered_rows),
        "strata": strata,
        "holdout": {
            "n": len(holdout),
            "hits@10": sum(
                1 for r in holdout
                if r["gold_snap_id"] in ranked[row_key(arm_name, r)]
            ),
            "detail": {
                r["gold_snap_id"]: (rank_of(r) or "miss") for r in holdout
            },
        },
    }


def fmt_md(report: dict) -> str:
    lines = [
        f"# Whole-snapshot retrieval baselines — {report['date']} (WI-6 Phase A / M4)",
        "",
        f"Labeled set: {report['n_rows']} rows "
        f"({report['n_generated']} random-span generated + {report['n_holdout']} human holdout); "
        f"index={report['index_size']} snapshots; active model={report['active_slug']}; "
        f"k={K}; full retrieve() pipeline via the M4 store/query_vector eval seam.",
        "",
        "`overall` counts gold-uncovered rows (gold missing from the arm's store) as "
        "misses; `covered-only` restricts to rows the arm could possibly retrieve. "
        "The qwen store is ~600 snapshots behind the index by design (no healing "
        "here — migrate auto-cutover is forbidden pre-M6).",
        "",
        "## Overall",
        "",
        "| arm | n | gold-uncovered | r@1 | r@3 | r@5 | r@10 | MRR | r@10 (covered) | MRR (covered) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, m in report["arms"].items():
        o, c = m["overall"], m["covered_only"]
        lines.append(
            f"| {name} | {o['n']} | {m['gold_uncovered']} | {o['recall@1']} | {o['recall@3']} "
            f"| {o['recall@5']} | {o['recall@10']} | {o['mrr']} | {c.get('recall@10', '—')} "
            f"| {c.get('mrr', '—')} |"
        )
    for field, label in (("length_band", "length band"), ("position_third", "span position")):
        lines += ["", f"## By {label} (overall: recall@10 / MRR / n)", ""]
        vals = sorted({v for m in report["arms"].values() for v in m["strata"][field]})
        lines.append("| arm | " + " | ".join(vals) + " |")
        lines.append("|---|" + "---|" * len(vals))
        for name, m in report["arms"].items():
            cells = []
            for v in vals:
                s = m["strata"][field].get(v, {"n": 0})
                cells.append(
                    f"{s.get('recall@10', '—')} / {s.get('mrr', '—')} / {s['n']}"
                    if s["n"] else "—"
                )
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "## Holdout (human-verified pairs, rank@10 or miss)", ""]
    lines.append("| arm | hits@10 | detail |")
    lines.append("|---|---|---|")
    for name, m in report["arms"].items():
        h = m["holdout"]
        detail = ", ".join(f"{sid}: {rk}" for sid, rk in h["detail"].items())
        lines.append(f"| {name} | {h['hits@10']}/{h['n']} | {detail} |")
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-date", default=time.strftime("%Y-%m-%d"))
    args = ap.parse_args()

    rows = [json.loads(ln) for ln in LABELED.read_text().splitlines() if ln.strip()]
    print(f"[bench] {len(rows)} labeled rows; active model = {get_active_slug()}")
    cache = load_cache()

    report = {
        "date": args.out_date,
        "k": K,
        "n_rows": len(rows),
        "n_generated": sum(1 for r in rows if r["source"] == "generated"),
        "n_holdout": sum(1 for r in rows if r["source"] == "holdout"),
        "index_size": len(load_snapshot_index()),
        "active_slug": get_active_slug(),
        "arms": {},
    }
    for arm in ARMS:
        ranked = run_arm(arm, rows, cache)
        covered = get_store(arm["slug"]).ids()
        report["arms"][arm["name"]] = metrics(rows, ranked, arm["name"], covered)
        o = report["arms"][arm["name"]]["overall"]
        print(f"[{arm['name']}] r@10={o['recall@10']} MRR={o['mrr']} "
              f"uncovered={report['arms'][arm['name']]['gold_uncovered']}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / f"{args.out_date}-baseline.json"
    md_path = RESULTS_DIR / f"{args.out_date}-baseline.md"
    json_path.write_text(json.dumps(report, indent=2))
    md_path.write_text(fmt_md(report))
    print(f"[done] wrote {md_path} and {json_path}")


if __name__ == "__main__":
    main()
