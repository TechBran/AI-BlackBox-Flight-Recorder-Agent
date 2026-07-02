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

--recency-sweep (M4b, measurement only): re-runs BOTH gemini2 arms over all
labeled rows at recency_weight in {0.05, 0.03, 0.02, 0.01, 0.005, 0.0} by
setting the value on the in-process CFG object (retrieve() reads CFG at call
time) — config.ini ON DISK IS NEVER TOUCHED (byte-asserted before/after, and
the original in-process value restored). Because the labeled set alone is
biased toward w=0 (old golds, random spans), each weight ALSO runs the 4
human recurring-topic queries from test_retrieval_golden through the
production path and checks a current/previous-month snapshot stays in the
top-5 (the "latest state of X" freshness use case). NO production code or
config change is made here; the config decision is made elsewhere.

Run (from the repo root):
    Orchestrator/venv/bin/python eval/run_bench.py [--out-date 2026-07-02]
    Orchestrator/venv/bin/python eval/run_bench.py --recency-sweep
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


# ── M4b: recency-weight sweep (measurement only) ─────────────────────────────

SWEEP_WEIGHTS = [0.05, 0.03, 0.02, 0.01, 0.005, 0.0]
SWEEP_ARMS = [a for a in ARMS if a["slug"] == "gemini-embedding-2"]


def _sweep_agg(rows: list, ranks: list) -> dict:
    """recall@1/3/5/10 + MRR overall, and >10k-band recall@10."""
    n = len(rows)
    out = {"n": n}
    for k in (1, 3, 5, 10):
        out[f"recall@{k}"] = round(sum(1 for x in ranks if x and x <= k) / n, 4)
    out["mrr"] = round(sum(1.0 / x for x in ranks if x) / n, 4)
    band = [(r, rk) for r, rk in zip(rows, ranks) if r.get("length_band") == ">10k"]
    out["recall@10_gt10k"] = round(
        sum(1 for _r, rk in band if rk and rk <= 10) / len(band), 4
    ) if band else None
    out["n_gt10k"] = len(band)
    return out


def _freshness_prefixes() -> tuple:
    """SNAP id prefixes for the current and previous month (dynamic, not pinned)."""
    from datetime import date, timedelta
    today = date.today()
    prev = today.replace(day=1) - timedelta(days=1)
    return f"SNAP-{today:%Y%m}", f"SNAP-{prev:%Y%m}"


def run_sweep(rows: list, out_date: str) -> None:
    """Sweep [retrieval] recency_weight IN-PROCESS ONLY; config.ini untouched."""
    from Orchestrator.config import CFG
    # The 4 human recurring-topic queries — single source of truth in the golden test.
    from Orchestrator.tests.test_retrieval_golden import RECURRING_QUERIES

    ini_path = REPO / "config.ini"
    ini_before = ini_path.read_bytes()

    store = get_store("gemini-embedding-2")
    print(f"[sweep] embedding {len(rows)} queries once (reused across all weights/arms)")
    vecs = embed_queries("gemini-embedding-2", [r["query"] for r in rows])

    if not CFG.has_section("retrieval"):
        CFG.add_section("retrieval")
    had_opt = CFG.has_option("retrieval", "recency_weight")
    orig = CFG.get("retrieval", "recency_weight") if had_opt else None
    cur_pfx, prev_pfx = _freshness_prefixes()

    result = {"date": out_date, "k": K, "n_rows": len(rows),
              "weights": SWEEP_WEIGHTS, "freshness_prefixes": [cur_pfx, prev_pfx],
              "freshness_queries": list(RECURRING_QUERIES), "sweep": []}
    try:
        for w in SWEEP_WEIGHTS:
            CFG.set("retrieval", "recency_weight", str(w))
            entry = {"weight": w, "arms": {}, "freshness": {}}
            for arm in SWEEP_ARMS:
                t0 = time.time()
                ranks = []
                for r, qv in zip(rows, vecs):
                    res = retrieve(
                        r["query"], operator=r["operator"], k=K,
                        include_keyword=arm["include_keyword"],
                        store=store, query_vector=qv,
                    )
                    ids = [sid for sid, _s in res]
                    ranks.append(ids.index(r["gold_snap_id"]) + 1
                                 if r["gold_snap_id"] in ids else None)
                entry["arms"][arm["name"]] = _sweep_agg(rows, ranks)
                a = entry["arms"][arm["name"]]
                print(f"[sweep w={w}] {arm['name']}: r@10={a['recall@10']} "
                      f"MRR={a['mrr']} >10k r@10={a['recall@10_gt10k']} "
                      f"({time.time() - t0:.0f}s)")
            # Freshness guard: production path (active store + active-model embed),
            # hybrid, system operator, k=5 — mirrors the golden Half-A assertion.
            for q in RECURRING_QUERIES:
                res = retrieve(q, "system", k=5)
                top = [sid for sid, _s in res]
                entry["freshness"][q] = any(
                    sid.startswith(cur_pfx) or sid.startswith(prev_pfx) for sid in top
                )
            passed = sum(entry["freshness"].values())
            print(f"[sweep w={w}] freshness: {passed}/{len(RECURRING_QUERIES)} pass")
            result["sweep"].append(entry)
    finally:
        # Restore the in-process CFG exactly; disk was never written.
        if had_opt:
            CFG.set("retrieval", "recency_weight", orig)
        else:
            CFG.remove_option("retrieval", "recency_weight")
    assert ini_path.read_bytes() == ini_before, "config.ini changed on disk — BUG"
    print("[sweep] config.ini byte-identical before/after: OK")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / f"{out_date}-recency-sweep.json"
    md_path = RESULTS_DIR / f"{out_date}-recency-sweep.md"
    json_path.write_text(json.dumps(result, indent=2))
    md_path.write_text(fmt_sweep_md(result))
    print(f"[done] wrote {md_path} and {json_path}")


def fmt_sweep_md(result: dict) -> str:
    lines = [
        f"# Recency-weight sweep — {result['date']} (M4b, measurement only)",
        "",
        f"{result['n_rows']} labeled rows, both gemini-embedding-2 arms, full "
        f"retrieve() pipeline, k={K}. recency_weight set on the in-process CFG "
        "only — config.ini untouched (byte-asserted). Freshness guard: the 4 "
        "human recurring-topic queries (test_retrieval_golden Half-A) must keep "
        f"a {result['freshness_prefixes'][0]}/{result['freshness_prefixes'][1]} "
        "snapshot in the production top-5.",
        "",
    ]
    for arm in SWEEP_ARMS:
        lines += [f"## {arm['name']}", "",
                  "| weight | r@1 | r@3 | r@5 | r@10 | MRR | >10k r@10 |",
                  "|---|---|---|---|---|---|---|"]
        for e in result["sweep"]:
            a = e["arms"][arm["name"]]
            lines.append(
                f"| {e['weight']} | {a['recall@1']} | {a['recall@3']} | {a['recall@5']} "
                f"| {a['recall@10']} | {a['mrr']} | {a['recall@10_gt10k']} |"
            )
        lines.append("")
    lines += ["## Freshness guard (recent snapshot in top-5, production path)", "",
              "| weight | " + " | ".join(
                  f"Q{i + 1}" for i in range(len(result["freshness_queries"]))) + " | pass |",
              "|---|" + "---|" * (len(result["freshness_queries"]) + 1)]
    for e in result["sweep"]:
        marks = [("PASS" if e["freshness"][q] else "FAIL")
                 for q in result["freshness_queries"]]
        lines.append(f"| {e['weight']} | " + " | ".join(marks) +
                     f" | {sum(m == 'PASS' for m in marks)}/{len(marks)} |")
    lines += ["", "Queries: " + "; ".join(
        f"Q{i + 1}=\"{q}\"" for i, q in enumerate(result["freshness_queries"])), ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-date", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--recency-sweep", action="store_true",
                    help="M4b measurement sweep; no baseline re-run, no cache use")
    args = ap.parse_args()

    rows = [json.loads(ln) for ln in LABELED.read_text().splitlines() if ln.strip()]
    print(f"[bench] {len(rows)} labeled rows; active model = {get_active_slug()}")

    if args.recency_sweep:
        run_sweep(rows, args.out_date)
        return

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
