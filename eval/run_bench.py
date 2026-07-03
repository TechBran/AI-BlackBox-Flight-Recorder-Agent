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

--candidate-dir/--candidate-slug (M6f): bench the chunk-store BUILD CANDIDATE
(schema 2, e.g. Manifest/embeddings/_build/gemini-embedding-2) through the same
store-override seam. Candidate runs are cache-keyed by the store's meta
`generation`, so a refreshed (re-diffed) candidate re-benches instead of
serving stale ranks. The store must already exist (never created here).

--gate (M6f runbook step 3, the swap authorization): runs BOTH gemini2 arms
(hybrid + semantic) against the candidate AND fresh same-config runs against
the active store, then prints the six-gate table vs the corrected w=0.005
baselines read programmatically from eval/results/2026-07-02-recency-sweep.json
(gates 1–3; the sweep JSON has no position strata, so gate 4's tail-third
baseline comes from the fresh active-store runs). Gate 6 runs
test_retrieval_golden.py + test_local_lean_retrieval.py in-process POINTED AT
THE CANDIDATE (the search.get_active_store seam is patched for the run — the
test files are unmodified). Writes eval/results/{date}-chunk-gate.{md,json}
and exits non-zero if ANY gate fails (a failed gate = STOP, no cutover).

Run (from the repo root):
    Orchestrator/venv/bin/python eval/run_bench.py [--out-date 2026-07-02]
    Orchestrator/venv/bin/python eval/run_bench.py --recency-sweep
    Orchestrator/venv/bin/python eval/run_bench.py --gate \
        --candidate-dir Manifest/embeddings/_build \
        --candidate-slug gemini-embedding-2
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


def _qhash(row: dict) -> str:
    return hashlib.sha1(f"{row['query']}\x00{row['operator']}".encode()).hexdigest()


def row_key(arm_name: str, row: dict) -> str:
    return f"{arm_name}|{_qhash(row)}|k={K}"


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


def run_arm(arm: dict, rows: list, cache: dict, vec_map: dict = None) -> dict:
    """-> {row_key: [snap_id, ...]} for every labeled row, filling the cache.

    Default arms behave byte-identically to the original bench. An arm may
    additionally carry:
      * "store" — an already-open override store (M6f candidate); default
        arms keep resolving via get_store(slug).
      * "cache_name" — the cache-key arm label; candidate keys embed the
        store's meta generation (a refreshed candidate re-benches), gate
        baselines embed the weight. Defaults to arm["name"].
    vec_map: optional {_qhash(row): query_vector} of pre-embedded vectors
    shared across same-model arms (gate mode embeds the 503 queries ONCE).
    """
    store = arm["store"] if arm.get("store") is not None else get_store(arm["slug"])
    cname = arm.get("cache_name", arm["name"])
    pending = [r for r in rows if row_key(cname, r) not in cache]
    print(f"[{arm['name']}] store={arm['slug']} rows={len(rows)} "
          f"cached={len(rows) - len(pending)} to-run={len(pending)}")
    if pending:
        t0 = time.time()
        if vec_map is None:
            vecs = embed_queries(arm["slug"], [r["query"] for r in pending])
            print(f"[{arm['name']}] embedded {len(vecs)} queries in {time.time() - t0:.1f}s")
        else:
            vecs = [vec_map[_qhash(r)] for r in pending]
        for n, (r, qv) in enumerate(zip(pending, vecs), 1):
            results = retrieve(
                r["query"], operator=r["operator"], k=K,
                include_keyword=arm["include_keyword"],
                store=store, query_vector=qv,
            )
            cache[row_key(cname, r)] = [sid for sid, _score in results]
            if n % 25 == 0 or n == len(pending):
                save_cache(cache)
                print(f"[{arm['name']}] {n}/{len(pending)} "
                      f"({(time.time() - t0) / n:.2f}s/row)")
    return {row_key(cname, r): cache[row_key(cname, r)] for r in rows}


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


# ── M6f: candidate chunk-store arm + six-gate mode (runbook step 3) ──────────

GATE_SWEEP_JSON = RESULTS_DIR / "2026-07-02-recency-sweep.json"
GATE_WEIGHT = 0.005  # the corrected baseline row the gates compare against


def resolve_candidate_store(candidate_dir: str, slug: str):
    """Open the build candidate: an EXISTING schema-2 store only (schema=2 is
    ALWAYS explicit — never autodetected — and get_store would otherwise
    CREATE a store; a missing dir is refused instead). Accepts either the
    base dir containing <slug>/ (e.g. Manifest/embeddings/_build) or the
    store dir itself."""
    p = Path(candidate_dir).resolve()
    if (p / slug / "meta.json").is_file():
        base = p
    elif p.name == slug and (p / "meta.json").is_file():
        base = p.parent
    else:
        raise SystemExit(
            f"--candidate-dir {p} has no existing {slug!r} store "
            f"(need <dir>/{slug}/meta.json — refusing to create one)"
        )
    return get_store(slug, base_dir=base, schema=2)


def store_generation(store) -> int:
    """The store's meta `generation` — cache-key component for candidate arms
    so a refreshed (re-diffed) candidate re-benches instead of reusing ranks."""
    try:
        return int(json.loads(store.meta_path.read_text()).get("generation", 0))
    except Exception:
        return 0


def candidate_arms(slug: str, store, generation: int) -> list:
    """Hybrid + semantic candidate arms, generation-keyed in the cache."""
    arms = []
    for include_keyword, label in ((True, "hybrid"), (False, "semantic")):
        name = f"cand-{slug}-{label}"
        arms.append({
            "name": name, "slug": slug, "include_keyword": include_keyword,
            "store": store, "cache_name": f"{name}|gen={generation}",
        })
    return arms


def load_gate_baselines(sweep_json_path, weight: float) -> dict:
    """The w=<weight> sweep entry's arm numbers — read programmatically from
    the committed sweep results, never hardcoded."""
    sweep = json.loads(Path(sweep_json_path).read_text())
    entry = next(
        (e for e in sweep["sweep"] if abs(e["weight"] - weight) < 1e-12), None)
    if entry is None:
        raise SystemExit(
            f"no w={weight} entry in {sweep_json_path} "
            f"(have: {[e['weight'] for e in sweep['sweep']]})")
    return entry["arms"]


def _stratum(m: dict, field: str, value: str, metric: str = "recall@10"):
    """A stratified metric from a metrics() dict; None when the bucket is
    absent/empty (evaluate_gates treats that as FAIL, never a silent pass)."""
    s = m.get("strata", {}).get(field, {}).get(value, {})
    return s.get(metric) if s.get("n") else None


def evaluate_gates(sweep_arms: dict, cand: dict, fresh_base: dict,
                   tests_gate) -> tuple:
    """The six-gate table (M6f runbook step 3) as pure number comparisons.

    sweep_arms: load_gate_baselines() output (gates 1–3 thresholds).
    cand / fresh_base: {"hybrid": metrics(), "semantic": metrics()} for the
      candidate and the SAME-CONFIG fresh active-store runs (the sweep JSON
      has no position strata, so gate 4's tail-third baseline is measured,
      not copied).
    tests_gate: gate-6 outcome (True/False), or None when explicitly skipped
      — a skipped gate 6 is recorded but does not fail the run (the runbook
      still requires it before cutover).
    -> ([row dicts], all_pass). Gates 1–2 are >= (no regression); gates 3–4
    are strict > (MUST IMPROVE); gate 5 is hits@10 == n on the human holdout.
    """
    rows = []

    def add(num, label, baseline, candidate, passed, note=""):
        rows.append({
            "gate": str(num), "label": label,
            "baseline": baseline, "candidate": candidate,
            "verdict": ("SKIPPED" if passed is None
                        else "PASS" if passed else "FAIL"),
            "note": note,
        })

    def rnd(x):
        return None if x is None else round(float(x), 4)

    for n_, arm_key, side in ((1, "gemini2-hybrid", "hybrid"),
                              (2, "gemini2-semantic", "semantic")):
        base = sweep_arms[arm_key]["recall@10"]
        got = cand[side]["overall"].get("recall@10")
        add(n_, f"{side} overall r@10 >= baseline (no regression)",
            rnd(base), rnd(got), got is not None and got >= base)

    for sub, arm_key, side in (("3a", "gemini2-hybrid", "hybrid"),
                               ("3b", "gemini2-semantic", "semantic")):
        base = sweep_arms[arm_key]["recall@10_gt10k"]
        got = _stratum(cand[side], "length_band", ">10k")
        add(sub, f"{side} >10k-band r@10 MUST IMPROVE (>)",
            rnd(base), rnd(got),
            got is not None and base is not None and got > base)

    for sub, side in (("4a", "hybrid"), ("4b", "semantic")):
        base = _stratum(fresh_base[side], "position_third", "tail")
        got = _stratum(cand[side], "position_third", "tail")
        add(sub, f"{side} tail-third r@10 MUST IMPROVE (>)",
            rnd(base), rnd(got),
            got is not None and base is not None and got > base,
            note="baseline measured fresh at the gate weight (sweep JSON has no position strata)")

    for sub, side in (("5a", "hybrid"), ("5b", "semantic")):
        h = cand[side]["holdout"]
        add(sub, f"{side} human holdout pairs still hit (hits@10)",
            f"{h['n']}/{h['n']}", f"{h['hits@10']}/{h['n']}",
            h["n"] > 0 and h["hits@10"] == h["n"])

    add(6, "golden + lean-profile suites vs candidate "
           "(test_retrieval_golden.py + test_local_lean_retrieval.py)",
        "all pass",
        {True: "all pass", False: "FAILURES", None: "skipped"}[tests_gate],
        tests_gate,
        note="" if tests_gate is not None else
             "--skip-tests-gate: recorded only; still REQUIRED before cutover")

    all_pass = all(r["verdict"] != "FAIL" for r in rows)
    return rows, all_pass


def run_tests_gate(candidate_store) -> bool:
    """Gate 6: run the golden + lean-profile suites POINTED AT THE CANDIDATE.

    The one production seam — Orchestrator.embeddings.search.get_active_store
    — is patched in-process for the pytest run (both test files resolve it at
    call time; the files themselves are unmodified). Requires the live
    embedding provider (the tests embed queries through the production path).
    Skipped tests FAIL the gate: a skip means the candidate was not actually
    exercised."""
    import pytest as _pytest
    from Orchestrator.embeddings import search as _search

    class _Counter:
        passed = failed = skipped = 0

        def pytest_runtest_logreport(self, report):
            if report.failed:
                self.failed += 1
            elif report.skipped:
                self.skipped += 1
            elif report.when == "call" and report.passed:
                self.passed += 1

    counter = _Counter()
    orig = _search.get_active_store
    _search.get_active_store = lambda: candidate_store
    try:
        rc = _pytest.main(
            ["-q", "-p", "no:cacheprovider",
             str(REPO / "Orchestrator" / "tests" / "test_retrieval_golden.py"),
             str(REPO / "Orchestrator" / "tests" / "test_local_lean_retrieval.py")],
            plugins=[counter],
        )
    finally:
        _search.get_active_store = orig
    ok = (rc == 0 and counter.failed == 0 and counter.skipped == 0
          and counter.passed > 0)
    print(f"[gate-6] golden+lean vs candidate: rc={rc} passed={counter.passed} "
          f"failed={counter.failed} skipped={counter.skipped} -> "
          f"{'PASS' if ok else 'FAIL'}")
    return ok


def fmt_gate_table(gate_rows: list) -> str:
    lines = ["| # | gate | baseline | candidate | verdict |",
             "|---|---|---|---|---|"]
    for r in gate_rows:
        lines.append(f"| {r['gate']} | {r['label']} | {r['baseline']} "
                     f"| {r['candidate']} | {r['verdict']} |")
    return "\n".join(lines)


def fmt_gate_md(report: dict) -> str:
    c = report["candidate"]
    lines = [
        f"# M6f chunk-store gate — {report['date']} (runbook step 3)",
        "",
        f"Candidate: `{c['dir']}` (slug {c['slug']}, schema {c['schema']}, "
        f"rows {c['rows']}, snapshots {c['snapshots']}, generation {c['generation']}).",
        f"Gate baselines: `{report['baseline_source']}` w={report['baseline_weight']} "
        f"entry (gates 1–3) + fresh same-config active-store runs (gate 4 "
        f"tail-third — the sweep JSON carries no position strata). "
        f"{report['n_rows']} labeled rows, k={report['k']}, full retrieve() "
        f"pipeline via the M4 store-override seam; active store at "
        f"{report['active_store_snapshots']} snapshots during the run.",
    ]
    if report.get("pipeline_overrides"):
        lines.append(
            f"IN-PROCESS pipeline overrides: `{report['pipeline_overrides']}` "
            "(config.ini untouched, byte-asserted; BOTH the candidate and the "
            "fresh baseline arms — and the gate-6 pytest run — executed at "
            "these values, so every gate compares identical pipeline config).")
    lines += [
        "",
        "## Gates",
        "",
        fmt_gate_table(report["gates"]),
        "",
        f"**VERDICT: {'ALL GATES PASS — cutover authorized' if report['all_pass'] else 'GATE FAILED — STOP, no cutover'}**",
    ]
    notes = [r for r in report["gates"] if r.get("note")]
    if notes:
        lines += ["", "Notes:"] + [f"- gate {r['gate']}: {r['note']}" for r in notes]
    for field, label in (("position_third", "span position"),
                         ("length_band", "length band")):
        lines += ["", f"## By {label} (r@10 / MRR / n)", ""]
        vals = sorted({v for d in report["runs"].values()
                       for m in d.values() for v in m["strata"][field]})
        lines.append("| run | " + " | ".join(vals) + " |")
        lines.append("|---|" + "---|" * len(vals))
        for side in ("baseline", "candidate"):
            for short, m in report["runs"][side].items():
                cells = []
                for v in vals:
                    s = m["strata"][field].get(v, {"n": 0})
                    cells.append(
                        f"{s.get('recall@10', '—')} / {s.get('mrr', '—')} / {s['n']}"
                        if s["n"] else "—")
                lines.append(f"| {side}-{short} | " + " | ".join(cells) + " |")
    lines += ["", "## Holdout (human-verified pairs, rank@10 or miss)", "",
              "| run | hits@10 | detail |", "|---|---|---|"]
    for side in ("baseline", "candidate"):
        for short, m in report["runs"][side].items():
            h = m["holdout"]
            detail = ", ".join(f"{sid}: {rk}" for sid, rk in h["detail"].items())
            lines.append(f"| {side}-{short} | {h['hits@10']}/{h['n']} | {detail} |")
    lines.append("")
    return "\n".join(lines)


def run_gate(rows: list, args) -> None:
    """Six-gate swap authorization: candidate + fresh baselines, table,
    artifacts, exit code (any FAIL -> exit 1: STOP, no cutover).

    --mmr-lambda / --candidate-n (M6f iteration 1) / --mmr-protect (M6f
    iteration 3, [retrieval] mmr_protect_top): optional [retrieval]
    pipeline overrides applied to the IN-PROCESS CFG for the whole gate run
    (config.ini on disk byte-asserted untouched, original values restored).
    With overrides active, gates 1-3 CANNOT use the committed sweep-JSON
    baselines (those were measured at the default pipeline config) — the
    baselines for ALL gates switch to the fresh same-config active-store
    runs, so every gate compares candidate-vs-baseline at IDENTICAL pipeline
    config. Gate 6's pytest run also executes under the overrides (retrieve()
    reads CFG at call time). Overrides are recorded in the report, embedded
    in the cache keys, and suffixed onto the output filenames (the default
    gate artifacts are never clobbered)."""
    from Orchestrator.config import CFG

    overrides = {}
    if args.mmr_lambda is not None:
        overrides["mmr_lambda"] = str(args.mmr_lambda)
    if args.candidate_n is not None:
        overrides["candidate_n"] = str(args.candidate_n)
    if args.mmr_protect is not None:
        overrides["mmr_protect_top"] = str(args.mmr_protect)

    sweep_arms = (None if overrides
                  else load_gate_baselines(args.gate_sweep_json, args.gate_weight))

    live_w = CFG.getfloat("retrieval", "recency_weight", fallback=0.05)
    if abs(live_w - args.gate_weight) > 1e-9:
        raise SystemExit(
            f"[gate] live [retrieval] recency_weight={live_w} != gate baseline "
            f"w={args.gate_weight}: the comparison would be invalid (the sweep "
            f"baselines were measured at w={args.gate_weight}); fix config first")

    slug = args.candidate_slug
    active = get_active_slug()
    if slug != active:
        raise SystemExit(
            f"[gate] candidate slug {slug!r} != active model {active!r}: the "
            f"gate's query embeds and the gate-6 test run assume the active model")

    cand_store = resolve_candidate_store(args.candidate_dir, slug)
    gen = store_generation(cand_store)
    active_store = get_store(slug)  # the live store = fresh-baseline arms
    print(f"[gate] candidate {cand_store.meta_path.parent} schema={cand_store.schema} "
          f"rows={cand_store.rows} snapshots={cand_store.snapshots} generation={gen}")
    if overrides:
        print(f"[gate] IN-PROCESS pipeline overrides: {overrides} "
              f"(config.ini untouched; gates 1-3 baselines switch to the "
              f"fresh same-config active-store runs)")

    cache = load_cache()
    print(f"[gate] embedding {len(rows)} labeled queries once "
          f"(shared across all 4 runs — same model)")
    vec_list = embed_queries(slug, [r["query"] for r in rows])
    vec_map = {_qhash(r): v for r, v in zip(rows, vec_list)}

    ini_path = REPO / "config.ini"
    ini_before = ini_path.read_bytes()
    saved = {}
    for opt, val in overrides.items():
        saved[opt] = (CFG.has_option("retrieval", opt),
                      CFG.get("retrieval", opt)
                      if CFG.has_option("retrieval", opt) else None)
        CFG.set("retrieval", opt, val)
    # Cache-key + filename suffix so override runs never collide with (or
    # clobber) the default-config gate.
    osuf = "".join(f"|{k}={v}" for k, v in sorted(overrides.items()))
    fsuf = "".join(
        f"-{k.replace('_', '')}{v}" for k, v in sorted(overrides.items()))

    try:
        active_count = active_store.count
        runs = {"baseline": {}, "candidate": {}}
        for arm in SWEEP_ARMS:
            short = "hybrid" if arm["include_keyword"] else "semantic"
            for side, st, cname in (
                ("baseline", active_store,
                 f"{arm['name']}|gate-base|w={args.gate_weight}|n={active_count}{osuf}"),
                ("candidate", cand_store, f"{arm['name']}|gate-cand|gen={gen}{osuf}"),
            ):
                a = {"name": f"gate-{side}-{short}", "slug": slug,
                     "include_keyword": arm["include_keyword"],
                     "store": st, "cache_name": cname}
                ranked = run_arm(a, rows, cache, vec_map=vec_map)
                runs[side][short] = metrics(rows, ranked, cname, st.ids())
                o = runs[side][short]["overall"]
                print(f"[gate] {side}-{short}: r@10={o['recall@10']} MRR={o['mrr']} "
                      f">10k={_stratum(runs[side][short], 'length_band', '>10k')} "
                      f"tail={_stratum(runs[side][short], 'position_third', 'tail')}")

        if sweep_arms is None:
            # Overrides active: every gate compares candidate-vs-baseline at
            # the SAME pipeline config — gates 1-3 thresholds come from the
            # fresh active-store runs just measured (the sweep JSON's numbers
            # were measured at the default config and are not comparable).
            sweep_arms = {
                f"gemini2-{side}": {
                    "recall@10": runs["baseline"][side]["overall"]["recall@10"],
                    "recall@10_gt10k": _stratum(
                        runs["baseline"][side], "length_band", ">10k"),
                } for side in ("hybrid", "semantic")
            }

        tests_gate = None if args.skip_tests_gate else run_tests_gate(cand_store)
        gate_rows, ok = evaluate_gates(
            sweep_arms, runs["candidate"], runs["baseline"], tests_gate)
    finally:
        for opt, (had, orig) in saved.items():
            if had:
                CFG.set("retrieval", opt, orig)
            else:
                CFG.remove_option("retrieval", opt)
    assert ini_path.read_bytes() == ini_before, "config.ini changed on disk — BUG"
    if overrides:
        print("[gate] config.ini byte-identical before/after: OK")

    report = {
        "date": args.out_date, "k": K, "n_rows": len(rows),
        "candidate": {"slug": slug, "dir": str(cand_store.meta_path.parent),
                      "schema": cand_store.schema, "rows": cand_store.rows,
                      "snapshots": cand_store.snapshots, "generation": gen},
        "baseline_source": (
            "fresh same-config active-store runs (pipeline overrides active; "
            "the sweep-JSON baselines were measured at the default config)"
            if overrides else str(args.gate_sweep_json)),
        "baseline_weight": args.gate_weight,
        "pipeline_overrides": {
            k: (int(v) if k in ("candidate_n", "mmr_protect_top") else float(v))
            for k, v in overrides.items()} or None,
        "active_store_snapshots": active_count,
        "gates": gate_rows, "all_pass": ok, "runs": runs,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / f"{args.out_date}-chunk-gate{fsuf}.json"
    md_path = RESULTS_DIR / f"{args.out_date}-chunk-gate{fsuf}.md"
    json_path.write_text(json.dumps(report, indent=2))
    md_path.write_text(fmt_gate_md(report))
    print("\n" + fmt_gate_table(gate_rows) + "\n")
    print(f"[done] wrote {md_path} and {json_path}")
    print(f"[gate] VERDICT: "
          f"{'ALL GATES PASS — cutover authorized' if ok else 'GATE FAILED — STOP, no cutover'}")
    if not ok:
        sys.exit(1)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-date", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--recency-sweep", action="store_true",
                    help="M4b measurement sweep; no baseline re-run, no cache use")
    ap.add_argument("--candidate-dir", default=None,
                    help="M6f chunk candidate: base dir containing <slug>/ "
                         "(e.g. Manifest/embeddings/_build) or the store dir itself; "
                         "adds candidate hybrid+semantic arms (or feeds --gate)")
    ap.add_argument("--candidate-slug", default=None,
                    help="candidate model slug (the arm's model == this slug)")
    ap.add_argument("--gate", action="store_true",
                    help="M6f six-gate swap authorization (runbook step 3); "
                         "exits 1 on any gate failure")
    ap.add_argument("--gate-sweep-json", default=str(GATE_SWEEP_JSON),
                    help="corrected-baseline sweep results (gates 1-3 thresholds)")
    ap.add_argument("--gate-weight", type=float, default=GATE_WEIGHT,
                    help="sweep entry the gates compare against (must match the "
                         "live [retrieval] recency_weight)")
    ap.add_argument("--skip-tests-gate", action="store_true",
                    help="skip gate 6 (golden+lean pytest vs candidate); recorded "
                         "as SKIPPED — still required before cutover")
    ap.add_argument("--mmr-lambda", type=float, default=None,
                    help="gate-mode [retrieval] mmr_lambda override, applied "
                         "IN-PROCESS to candidate AND baseline arms (and gate 6); "
                         "config.ini untouched; recorded in the report")
    ap.add_argument("--candidate-n", type=int, default=None,
                    help="gate-mode [retrieval] candidate_n override, applied "
                         "IN-PROCESS to candidate AND baseline arms (and gate 6); "
                         "config.ini untouched; recorded in the report")
    ap.add_argument("--mmr-protect", type=int, default=None,
                    help="gate-mode [retrieval] mmr_protect_top override (M6f "
                         "iteration 3 top-rank protect), applied IN-PROCESS to "
                         "candidate AND baseline arms (and gate 6); config.ini "
                         "untouched; recorded in the report")
    args = ap.parse_args(argv)

    if args.candidate_dir and not args.candidate_slug:
        ap.error("--candidate-dir requires --candidate-slug")
    if args.gate and not (args.candidate_dir and args.candidate_slug):
        ap.error("--gate requires --candidate-dir and --candidate-slug")
    if (args.mmr_lambda is not None or args.candidate_n is not None
            or args.mmr_protect is not None) and not args.gate:
        ap.error("--mmr-lambda/--candidate-n/--mmr-protect are gate-mode "
                 "overrides (require --gate)")

    rows = [json.loads(ln) for ln in LABELED.read_text().splitlines() if ln.strip()]
    print(f"[bench] {len(rows)} labeled rows; active model = {get_active_slug()}")

    if args.gate:
        run_gate(rows, args)
        return

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
    arms = list(ARMS)
    if args.candidate_dir:
        cand_store = resolve_candidate_store(args.candidate_dir, args.candidate_slug)
        gen = store_generation(cand_store)
        print(f"[bench] candidate {cand_store.meta_path.parent} "
              f"schema={cand_store.schema} generation={gen}")
        arms += candidate_arms(args.candidate_slug, cand_store, gen)

    for arm in arms:
        ranked = run_arm(arm, rows, cache)
        store = arm["store"] if arm.get("store") is not None else get_store(arm["slug"])
        covered = store.ids()
        cname = arm.get("cache_name", arm["name"])
        report["arms"][arm["name"]] = metrics(rows, ranked, cname, covered)
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
