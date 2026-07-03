#!/usr/bin/env python3
"""M6f iteration 1 — gate-failure diagnosis + MMR sweep on the chunk candidate.

The 2026-07-02 six-gate run (eval/results/2026-07-02-chunk-gate.md) FAILED on
gates 1/5a/5b/6 while the chunking gates (3/4, tail + >10k recovery) passed.
Audit hypothesis: chunk-max scoring compresses top cosines into a tight band,
so MMR (mmr_lambda=0.7 over best-chunk representative vectors) now drops true
golds as near-duplicates of first-picked same-session siblings, and the
rank-space shuffle inside compressed clusters hurts hybrid RRF fusion.

This script does two things, ALL read-only (candidate opened with
get_store(slug, base_dir=..., schema=2); live store only searched; config.ini
byte-asserted untouched):

1. DIAGNOSIS of the failing holdout/golden items (the 2 failing pairs x both
   arms): raw collapsed store rank, keyword rank, post-RRF+recency (pre-MMR)
   rank, survival with MMR disabled (lambda=1.0), and the picked-before-gold
   sibling with the highest cosine to the gold's best chunk (the MMR-kill
   evidence).

2. SWEEP mmr_lambda in {0.7, 0.8, 0.85, 0.9, 0.95, 1.0} x candidate_n in
   {40, 60} x both arms (hybrid/semantic) x BOTH stores (candidate AND the
   active baseline — gates 1-4 compare candidate-vs-baseline at IDENTICAL
   pipeline config, so the baseline must move with the knob) over all 503
   labeled rows. Per config it also runs the gate-6 proxies through the REAL
   retrieve() (CFG.set in-process, restored, ini byte-asserted): the 3
   SINGLE_EVENT_GOLD pairs, the 4 RECURRING freshness queries, and the 3
   lean-profile queries, all against the candidate.

Method note (honesty): the sweep decomposes retrieve() into its published
pure steps (store.search_with_vectors -> junk floor -> rrf_fuse ->
apply_recency_tiebreak -> mmr_select) so the expensive per-row work (query
embed, keyword channel, store search) is computed once per (store, arm, n)
and only the final mmr_select re-runs per lambda. The decomposition is
verified against the production retrieve() on a random row sample at two
lambdas via the store/query_vector eval seams — any mismatch aborts the run.
Keyword ids are fetched separately per candidate_n (the keyword re-scorer has
NO prefix property); semantic top-60 is sliced for n=40 (argsort descent has
the prefix property, floor applied after slicing exactly like retrieve()).

Run (from the repo root):
    Orchestrator/venv/bin/python eval/mmr_sweep.py [--out-date 2026-07-02]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.chdir(REPO)  # Orchestrator.config reads config.ini relative to CWD
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

from Orchestrator.config import CFG  # noqa: E402
from Orchestrator.retrieval import (  # noqa: E402
    _age_days,
    apply_recency_tiebreak,
    mmr_select,
    retrieve,
    rrf_fuse,
)
from Orchestrator.embeddings.providers import get_provider  # noqa: E402
from Orchestrator.embeddings.store import get_store, get_active_slug  # noqa: E402
from Orchestrator.fossils import (  # noqa: E402
    keyword_retrieve_ids_for_operator,
    load_snapshot_index,
)
from Orchestrator.tests.test_retrieval_golden import (  # noqa: E402
    RECURRING_QUERIES,
    SINGLE_EVENT_GOLD,
)
from Orchestrator.tests.test_local_lean_retrieval import LEAN_QUERIES  # noqa: E402

LABELED = REPO / "eval" / "labeled_set.jsonl"
RESULTS_DIR = REPO / "eval" / "results"
K = 10
EMBED_BATCH = 16
SLUG = "gemini-embedding-2"
CANDIDATE_DIR = "Manifest/embeddings/_build"
LAMBDAS = [0.7, 0.8, 0.85, 0.9, 0.95, 1.0]
CANDIDATE_NS = [40, 60]
# The two gate-failing pairs (holdout rows 501-502 == golden queries 1-2).
FAILING = [
    ("android AudioRecord release race SIGABRT fix", "SNAP-20260606-6930"),
    ("UGV ZUPT zero-velocity IMU drift preprocessor", "SNAP-20260427-6316"),
]
EQUIV_SAMPLE = 12          # rows sampled for the retrieve() equivalence check
EQUIV_LAMBDAS = [0.7, 0.9]


def cfg_knobs() -> dict:
    return {
        "rrf_c": CFG.getint("retrieval", "rrf_c", fallback=60),
        "recency_weight": CFG.getfloat("retrieval", "recency_weight", fallback=0.005),
        "half_life": CFG.getfloat("retrieval", "recency_half_life_days", fallback=90.0),
        "junk_floor": CFG.getfloat("retrieval", "junk_floor", fallback=0.40),
        "base_mmr_lambda": CFG.getfloat("retrieval", "mmr_lambda", fallback=0.7),
        "base_candidate_n": CFG.getint("retrieval", "candidate_n", fallback=40),
    }


def embed_queries(texts: list) -> list:
    provider = get_provider(SLUG)
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        out.extend(asyncio.run(provider.embed(texts[i:i + EMBED_BATCH], "query")))
    return out


def allowed_for(operator: str, index: dict):
    if operator and operator != "system":
        return {sid for sid, m in index.items() if m.get("operator") == operator}
    return None


def pipeline_ranked(sem_pool, kw_ids, include_keyword, knobs, index, now):
    """Steps 4-7 of retrieve(): junk floor -> RRF -> recency. Returns
    (ranked [(sid, score)...], vec_by_id) — everything mmr_select needs."""
    sem = [(s, c, v) for s, c, v in sem_pool if c >= knobs["junk_floor"]]
    sem_ids = [s for s, _c, _v in sem]
    vec_by_id = {s: v for s, _c, v in sem}
    rankings = {"semantic": sem_ids}
    if include_keyword and kw_ids:
        rankings["keyword"] = kw_ids
    fused = rrf_fuse(rankings, c=knobs["rrf_c"])
    if not fused:
        return [], vec_by_id
    relevance = dict(fused)
    ages = {s: _age_days(index.get(s, {}).get("timestamp", ""), now)
            for s in relevance}
    ranked = apply_recency_tiebreak(
        relevance, ages, knobs["recency_weight"], knobs["half_life"])
    return ranked, vec_by_id


def mmr_top10(ranked, vec_by_id, lam, dims):
    """Step 8 of retrieve(): MMR over the top window -> top-k ids."""
    window = max(K * 2, 20)
    zero = np.zeros(dims, dtype=np.float32)
    cands = [(s, sc, vec_by_id.get(s, zero)) for s, sc in ranked[:window]]
    return mmr_select(cands, K, lam)


# ── 1. diagnosis of the failing items ────────────────────────────────────────

def diagnose(cand, index, knobs, now) -> list:
    """Rank diagnosis for the 2 failing pairs x both arms at the BASE config."""
    vecs = embed_queries([q for q, _ in FAILING])
    out = []
    for (query, gold), qv in zip(FAILING, vecs):
        qv = np.asarray(qv, dtype=np.float32)
        full = cand.search(qv, cand.count)  # raw collapsed full-depth ranking
        raw_rank = next((i + 1 for i, (s, _) in enumerate(full) if s == gold), None)
        raw_cos = next((c for s, c in full if s == gold), None)
        n = knobs["base_candidate_n"]
        sem_pool = cand.search_with_vectors(qv, n, None)
        kw_ids = keyword_retrieve_ids_for_operator("", query, n, "system")
        kw_rank = kw_ids.index(gold) + 1 if gold in kw_ids else None
        for arm, inc_kw in (("hybrid", True), ("semantic", False)):
            ranked, vec_by = pipeline_ranked(
                sem_pool, kw_ids, inc_kw, knobs, index, now)
            pre_mmr = next((i + 1 for i, (s, _) in enumerate(ranked) if s == gold), None)
            picked_base = mmr_top10(ranked, vec_by, knobs["base_mmr_lambda"], cand.dims)
            picked_off = mmr_top10(ranked, vec_by, 1.0, cand.dims)
            killer = None
            gv = vec_by.get(gold)
            if gold not in picked_base and gv is not None:
                zero = np.zeros(cand.dims, dtype=np.float32)
                sims = sorted(
                    ((float(gv @ vec_by.get(s, zero)), s) for s in picked_base),
                    reverse=True)
                killer = {"sid": sims[0][1], "cos_to_gold": round(sims[0][0], 4)}
            out.append({
                "query": query, "gold": gold, "arm": arm,
                "raw_collapsed_rank": raw_rank,
                "raw_cos": round(raw_cos, 4) if raw_cos is not None else None,
                "in_top40_pool": bool(raw_rank and raw_rank <= n),
                "keyword_rank": kw_rank if inc_kw else None,
                "post_rrf_recency_rank": pre_mmr,
                "mmr_base_lambda_top10": gold in picked_base,
                "mmr_off_top10_rank": (picked_off.index(gold) + 1
                                       if gold in picked_off else None),
                "killer_sibling": killer,
            })
    return out


# ── 2. the sweep ─────────────────────────────────────────────────────────────

def agg(rows, ranks) -> dict:
    """r@10 + MRR overall, >10k band, tail third, holdout hits/detail."""
    def block(subset_idx):
        n = len(subset_idx)
        if n == 0:
            return {"n": 0}
        rs = [ranks[i] for i in subset_idx]
        return {"n": n,
                "recall@10": round(sum(1 for r in rs if r) / n, 4),
                "mrr": round(sum(1.0 / r for r in rs if r) / n, 4)}
    all_idx = list(range(len(rows)))
    gt10k = [i for i in all_idx if rows[i].get("length_band") == ">10k"]
    tail = [i for i in all_idx if rows[i].get("position_third") == "tail"]
    hold = [i for i in all_idx if rows[i]["source"] == "holdout"]
    return {
        "overall": block(all_idx),
        "gt10k": block(gt10k),
        "tail": block(tail),
        "holdout": {
            "n": len(hold),
            "hits@10": sum(1 for i in hold if ranks[i]),
            "detail": {rows[i]["gold_snap_id"]: (ranks[i] or "miss") for i in hold},
        },
    }


def run_sweep(rows, vecs, stores, index, knobs, now):
    """ranks[(side, arm, n, lam)] -> aligned rank list (None = miss)."""
    ranks = {(side, arm, n, lam): []
             for side in stores for arm in ("hybrid", "semantic")
             for n in CANDIDATE_NS for lam in LAMBDAS}
    t0 = time.time()
    for i, (row, qv) in enumerate(zip(rows, vecs)):
        q, gold, op = row["query"], row["gold_snap_id"], row["operator"]
        qv = np.asarray(qv, dtype=np.float32)
        allowed = allowed_for(op, index)
        kw = {n: keyword_retrieve_ids_for_operator("", q, n, op or "")
              for n in CANDIDATE_NS}
        for side, store in stores.items():
            sem_max = store.search_with_vectors(qv, max(CANDIDATE_NS), allowed)
            for n in CANDIDATE_NS:
                sem_pool = sem_max[:n]  # argsort-descent prefix == top-n
                for arm, inc_kw in (("hybrid", True), ("semantic", False)):
                    ranked, vec_by = pipeline_ranked(
                        sem_pool, kw[n], inc_kw, knobs, index, now)
                    for lam in LAMBDAS:
                        picked = mmr_top10(ranked, vec_by, lam, store.dims)
                        ranks[(side, arm, n, lam)].append(
                            picked.index(gold) + 1 if gold in picked else None)
        if (i + 1) % 50 == 0 or i + 1 == len(rows):
            print(f"[sweep] {i + 1}/{len(rows)} rows "
                  f"({(time.time() - t0) / (i + 1):.2f}s/row)")
    return ranks


def equivalence_check(rows, vecs, stores, index, knobs) -> dict:
    """Decomposed pipeline == production retrieve() on sampled rows.

    retrieve() is called with the SAME store + query_vector via the eval seams
    while mmr_lambda is set on the in-process CFG (restored afterwards); the
    decomposition recomputes with a fresh `now` right before each call so the
    recency term matches. Any top-10 mismatch is fatal."""
    rng = random.Random(20260702)
    sample = rng.sample(range(len(rows)), min(EQUIV_SAMPLE, len(rows)))
    n = knobs["base_candidate_n"]
    checked, mismatches = 0, []
    had = CFG.has_option("retrieval", "mmr_lambda")
    orig = CFG.get("retrieval", "mmr_lambda") if had else None
    try:
        for lam in EQUIV_LAMBDAS:
            CFG.set("retrieval", "mmr_lambda", str(lam))
            for i in sample:
                row, qv = rows[i], vecs[i]
                allowed = allowed_for(row["operator"], index)
                kw_ids = keyword_retrieve_ids_for_operator(
                    "", row["query"], n, row["operator"] or "")
                for side, store in stores.items():
                    sem_pool = store.search_with_vectors(
                        np.asarray(qv, dtype=np.float32), n, allowed)
                    for arm, inc_kw in (("hybrid", True), ("semantic", False)):
                        now = datetime.now(timezone.utc)
                        ranked, vec_by = pipeline_ranked(
                            sem_pool, kw_ids, inc_kw, knobs, index, now)
                        mine = mmr_top10(ranked, vec_by, lam, store.dims)
                        prod = [sid for sid, _s in retrieve(
                            row["query"], operator=row["operator"], k=K,
                            include_keyword=inc_kw, store=store, query_vector=qv)]
                        checked += 1
                        if mine != prod:
                            mismatches.append({
                                "row": i, "side": side, "arm": arm, "lam": lam,
                                "mine": mine, "prod": prod})
    finally:
        if had:
            CFG.set("retrieval", "mmr_lambda", orig)
        else:
            CFG.remove_option("retrieval", "mmr_lambda")
    return {"checked": checked, "mismatches": mismatches}


def gate6_proxies(cand, knobs) -> dict:
    """Golden single-event + recurring-freshness + lean queries through the
    REAL retrieve() against the candidate, per (lam, n) — CFG.set in-process.

    Mirrors gate 6's assertions (top-10 gold / 2026-06 in top-5 / non-empty
    k=3) without the pytest harness; the full gate re-run remains the
    authority."""
    golden_qs = [q for q, _g in SINGLE_EVENT_GOLD]
    all_qs = golden_qs + list(RECURRING_QUERIES) + list(LEAN_QUERIES)
    vec_by_q = dict(zip(all_qs, embed_queries(all_qs)))
    out = {}
    opts = {}
    for opt in ("mmr_lambda", "candidate_n"):
        opts[opt] = (CFG.has_option("retrieval", opt),
                     CFG.get("retrieval", opt) if CFG.has_option("retrieval", opt) else None)
    try:
        for n in CANDIDATE_NS:
            CFG.set("retrieval", "candidate_n", str(n))
            for lam in LAMBDAS:
                CFG.set("retrieval", "mmr_lambda", str(lam))
                golden = {}
                for q, gold in SINGLE_EVENT_GOLD:
                    ids = [sid for sid, _s in retrieve(
                        q, "system", k=10, store=cand, query_vector=vec_by_q[q])]
                    golden[gold] = ids.index(gold) + 1 if gold in ids else "miss"
                recurring = {}
                for q in RECURRING_QUERIES:
                    ids = [sid for sid, _s in retrieve(
                        q, "system", k=5, store=cand, query_vector=vec_by_q[q])]
                    recurring[q] = any(sid.startswith("SNAP-202606") for sid in ids)
                lean_ok = all(
                    len(retrieve(q, "system", k=3, include_keyword=False,
                                 store=cand, query_vector=vec_by_q[q])) >= 1
                    for q in LEAN_QUERIES)
                out[(n, lam)] = {
                    "golden": golden,
                    "golden_hits": sum(1 for v in golden.values() if v != "miss"),
                    "recurring_pass": sum(recurring.values()),
                    "recurring": recurring,
                    "lean_ok": lean_ok,
                }
                print(f"[gate6-proxy] n={n} lam={lam}: "
                      f"golden {out[(n, lam)]['golden_hits']}/3, "
                      f"recurring {out[(n, lam)]['recurring_pass']}/4, "
                      f"lean {'ok' if lean_ok else 'FAIL'}")
    finally:
        for opt, (had, orig) in opts.items():
            if had:
                CFG.set("retrieval", opt, orig)
            else:
                CFG.remove_option("retrieval", opt)
    return out


def provisional_gates(m_cand_h, m_base_h, m_cand_s, m_base_s, proxy) -> dict:
    """The six gates as candidate-vs-baseline at IDENTICAL pipeline config
    (gate 6 via the retrieve()-level proxies; the pytest gate re-run is the
    final authority)."""
    g = {
        "1": m_cand_h["overall"]["recall@10"] >= m_base_h["overall"]["recall@10"],
        "2": m_cand_s["overall"]["recall@10"] >= m_base_s["overall"]["recall@10"],
        "3a": m_cand_h["gt10k"]["recall@10"] > m_base_h["gt10k"]["recall@10"],
        "3b": m_cand_s["gt10k"]["recall@10"] > m_base_s["gt10k"]["recall@10"],
        "4a": m_cand_h["tail"]["recall@10"] > m_base_h["tail"]["recall@10"],
        "4b": m_cand_s["tail"]["recall@10"] > m_base_s["tail"]["recall@10"],
        "5a": m_cand_h["holdout"]["hits@10"] == m_cand_h["holdout"]["n"],
        "5b": m_cand_s["holdout"]["hits@10"] == m_cand_s["holdout"]["n"],
        "6proxy": (proxy["golden_hits"] == 3 and proxy["recurring_pass"] == 4
                   and proxy["lean_ok"]),
    }
    g["all_pass"] = all(g.values())
    return g


# ── report ───────────────────────────────────────────────────────────────────

def fmt_md(report: dict) -> str:
    c = report["candidate"]
    lines = [
        f"# MMR sweep + gate-failure diagnosis — {report['date']} (M6f iteration 1)",
        "",
        f"Candidate: `{c['dir']}` (schema {c['schema']}, rows {c['rows']}, "
        f"snapshots {c['snapshots']}, generation {c['generation']}); active "
        f"baseline store at {report['active_store_snapshots']} snapshots. "
        f"{report['n_rows']} labeled rows, k={K}. Pipeline decomposed into "
        "retrieve()'s pure steps (search -> junk floor -> RRF -> recency -> "
        "MMR) so only mmr_select re-runs per lambda; decomposition verified "
        f"against production retrieve() on {report['equivalence']['checked']} "
        f"sampled calls ({len(report['equivalence']['mismatches'])} mismatches). "
        "All knob changes in-process only — config.ini byte-asserted untouched. "
        "Baseline arms are re-run at every (lambda, n): gates 1-4 compare "
        "candidate-vs-baseline at IDENTICAL pipeline config.",
        "",
        "## Diagnosis — the 4 failing items (base config: lambda="
        f"{report['knobs']['base_mmr_lambda']}, n={report['knobs']['base_candidate_n']})",
        "",
        "| query -> gold | arm | raw collapsed rank (cos) | kw rank | "
        "post-RRF+recency rank | in top-10 @ lambda=0.7 | MMR off (lambda=1.0) "
        "rank | killer sibling (cos to gold best chunk) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for d in report["diagnosis"]:
        killer = (f"{d['killer_sibling']['sid']} ({d['killer_sibling']['cos_to_gold']})"
                  if d["killer_sibling"] else "—")
        lines.append(
            f"| {d['query']!r} -> {d['gold']} | {d['arm']} "
            f"| {d['raw_collapsed_rank']} ({d['raw_cos']}) "
            f"| {d['keyword_rank'] or '—'} | {d['post_rrf_recency_rank']} "
            f"| {'YES' if d['mmr_base_lambda_top10'] else 'NO — MMR-dropped'} "
            f"| {d['mmr_off_top10_rank'] or 'miss'} | {killer} |")
    for n in CANDIDATE_NS:
        lines += [
            "",
            f"## Sweep matrix — candidate_n={n} (candidate/baseline at identical config)",
            "",
            "| lambda | G1 hyb r@10 c/b | G2 sem r@10 c/b | G3a >10k hyb c/b "
            "| G3b >10k sem c/b | G4a tail hyb c/b | G4b tail sem c/b "
            "| 5a hold | 5b hold | golden | recur | lean | gates |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for lam in LAMBDAS:
            e = report["sweep"][f"n={n}|lam={lam}"]
            m = e["metrics"]
            g = e["gates"]
            p = e["proxy"]

            # metric dicts are keyed [side][arm][block]
            def cb(arm, block):
                cv = m["candidate"][arm][block]["recall@10"]
                bv = m["baseline"][arm][block]["recall@10"]
                return f"{cv}/{bv}"
            fails = [k for k, v in g.items() if k != "all_pass" and not v]
            verdict = "ALL PASS" if g["all_pass"] else "FAIL: " + ",".join(fails)
            lines.append(
                f"| {lam} | {cb('hybrid', 'overall')} | {cb('semantic', 'overall')} "
                f"| {cb('hybrid', 'gt10k')} | {cb('semantic', 'gt10k')} "
                f"| {cb('hybrid', 'tail')} | {cb('semantic', 'tail')} "
                f"| {m['candidate']['hybrid']['holdout']['hits@10']}/3 "
                f"| {m['candidate']['semantic']['holdout']['hits@10']}/3 "
                f"| {p['golden_hits']}/3 | {p['recurring_pass']}/4 "
                f"| {'ok' if p['lean_ok'] else 'FAIL'} | {verdict} |")
    lines += ["", "## Recommendation", "", report["recommendation"], ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-date", default="2026-07-02")
    args = ap.parse_args()

    ini_path = REPO / "config.ini"
    ini_before = ini_path.read_bytes()

    knobs = cfg_knobs()
    active_slug = get_active_slug()
    if active_slug != SLUG:
        raise SystemExit(f"active model {active_slug!r} != {SLUG!r}")
    cand = get_store(SLUG, base_dir=CANDIDATE_DIR, schema=2)
    active = get_store(SLUG)
    stores = {"candidate": cand, "baseline": active}
    index = load_snapshot_index()
    now = datetime.now(timezone.utc)
    print(f"[setup] candidate schema={cand.schema} rows={cand.rows} "
          f"snapshots={cand.snapshots}; active snapshots={active.count}; "
          f"knobs={knobs}")

    rows = [json.loads(ln) for ln in LABELED.read_text().splitlines() if ln.strip()]
    print(f"[setup] {len(rows)} labeled rows; embedding queries once")
    vecs = embed_queries([r["query"] for r in rows])

    diagnosis = diagnose(cand, index, knobs, now)
    for d in diagnosis:
        print(f"[diag] {d['gold']} {d['arm']}: raw={d['raw_collapsed_rank']} "
              f"kw={d['keyword_rank']} preMMR={d['post_rrf_recency_rank']} "
              f"mmr0.7={'in' if d['mmr_base_lambda_top10'] else 'DROPPED'} "
              f"mmrOff={d['mmr_off_top10_rank']} killer={d['killer_sibling']}")

    equiv = equivalence_check(rows, vecs, stores, index, knobs)
    print(f"[equiv] {equiv['checked']} retrieve() calls compared, "
          f"{len(equiv['mismatches'])} mismatches")
    if equiv["mismatches"]:
        print(json.dumps(equiv["mismatches"][:5], indent=1))
        raise SystemExit("[equiv] decomposed pipeline != retrieve() — ABORT")

    ranks = run_sweep(rows, vecs, stores, index, knobs, now)
    proxies = gate6_proxies(cand, knobs)

    sweep = {}
    for n in CANDIDATE_NS:
        for lam in LAMBDAS:
            metrics = {side: {arm: agg(rows, ranks[(side, arm, n, lam)])
                              for arm in ("hybrid", "semantic")}
                       for side in stores}
            proxy = proxies[(n, lam)]
            gates = provisional_gates(
                metrics["candidate"]["hybrid"], metrics["baseline"]["hybrid"],
                metrics["candidate"]["semantic"], metrics["baseline"]["semantic"],
                proxy)
            proxy_out = dict(proxy)
            proxy_out["golden"] = {g: r for g, r in proxy["golden"].items()}
            sweep[f"n={n}|lam={lam}"] = {
                "candidate_n": n, "mmr_lambda": lam,
                "metrics": metrics, "proxy": proxy_out, "gates": gates,
            }

    # Recommendation: the SMALLEST change that passes all six provisional
    # gates — prefer keeping candidate_n=40, then the lambda closest to the
    # current 0.7.
    passing = [(n, lam) for n in CANDIDATE_NS for lam in LAMBDAS
               if sweep[f"n={n}|lam={lam}"]["gates"]["all_pass"]]
    passing.sort(key=lambda t: (t[0] != knobs["base_candidate_n"],
                                abs(t[1] - knobs["base_mmr_lambda"])))
    if passing:
        n, lam = passing[0]
        others = ", ".join(f"(n={a}, lambda={b})" for a, b in passing[1:]) or "none"
        rec = (f"**mmr_lambda={lam}, candidate_n={n}** is the smallest change "
               f"that passes ALL SIX provisional gates (candidate-vs-baseline "
               f"at identical config + gate-6 proxies). Other passing configs: "
               f"{others}. Authority: re-run the full gate via "
               f"`eval/run_bench.py --gate --mmr-lambda {lam}"
               + (f" --candidate-n {n}" if n != knobs["base_candidate_n"] else "")
               + "` before any cutover decision.")
    else:
        best = max(
            (sweep[f"n={n}|lam={lam}"] for n in CANDIDATE_NS for lam in LAMBDAS),
            key=lambda e: sum(1 for k, v in e["gates"].items()
                              if k != "all_pass" and v))
        fails = [k for k, v in best["gates"].items()
                 if k != "all_pass" and not v]
        rec = (f"NO swept config passes all six gates. Best: "
               f"mmr_lambda={best['mmr_lambda']}, candidate_n={best['candidate_n']} "
               f"— still failing: {', '.join(fails)}. The chunk candidate does "
               f"not clear the gate with MMR/pool tuning alone.")
    print(f"[recommendation] {rec}")

    report = {
        "date": args.out_date, "k": K, "n_rows": len(rows),
        "candidate": {"slug": SLUG, "dir": str(cand.meta_path.parent),
                      "schema": cand.schema, "rows": cand.rows,
                      "snapshots": cand.snapshots,
                      "generation": json.loads(cand.meta_path.read_text()).get("generation")},
        "active_store_snapshots": active.count,
        "knobs": knobs, "lambdas": LAMBDAS, "candidate_ns": CANDIDATE_NS,
        "diagnosis": diagnosis, "equivalence": equiv,
        "sweep": sweep, "recommendation": rec,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / f"{args.out_date}-mmr-sweep.json"
    md_path = RESULTS_DIR / f"{args.out_date}-mmr-sweep.md"
    json_path.write_text(json.dumps(report, indent=2, default=str))
    md_path.write_text(fmt_md(report))
    print(f"[done] wrote {md_path} and {json_path}")

    assert ini_path.read_bytes() == ini_before, "config.ini changed on disk — BUG"
    print("[done] config.ini byte-identical before/after: OK")


if __name__ == "__main__":
    main()
