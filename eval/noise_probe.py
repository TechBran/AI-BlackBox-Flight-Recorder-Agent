#!/usr/bin/env python3
"""Noise-tail / precision probe for the live retrieval pipeline (READ-ONLY).

The existing eval/run_bench.py measures RECALL (does the ONE synthesized gold
appear in top-10). It is structurally blind to the symptom Brandon reports:
"2-3 good hits, then a tail of snapshots that have nothing to do with the
query." That is a PRECISION / noise problem, and its dominant mechanism —
proven by the 2026-07-07 audit — is the keyword lane being RRF-fused into the
semantic relevance ranking (retrieval.py:426-429), injecting lexical-only
matches that never cleared any semantic gate, plus fill-to-k with no output
relevance floor (retrieval.py:473-488).

This tool measures that DETERMINISTICALLY (no human labels needed) by
replicating retrieve()'s exact candidate generation and attributing every
DELIVERED result to its channel:

  * semantic  — sid was in the floor-passed semantic candidate set (has a cosine)
  * keyword   — sid was ONLY in the keyword channel (lexical-only injection = noise)
  * both      — sid appeared in both channels (strongest evidence)

Per query it reports:
  * the production top-k Brandon actually receives (R.retrieve, hybrid), each
    row tagged channel / raw cosine / raw Vertex rerank score / snippet;
  * KEYWORD-ONLY FRACTION of the delivered top-k (the headline noise number);
  * EVICTIONS — genuine semantic hits (in the semantic-only top-k) that keyword
    fusion pushed OUT of the hybrid top-k (the flip side: keyword injection
    doesn't just add noise, it displaces real hits);
  * the raw Vertex rerank score of each delivered item, so we can SEE whether a
    post-rerank absolute floor would separate the keyword-only noise (it is
    computed live but DISCARDED by retrieval.py:283's rank-space remap).

--calibrate: the "where does relevance actually live" measurement Brandon asked
for. Scores known-relevant (query,gold) POSITIVE pairs vs random unrelated
NEGATIVE pairs through the active provider/store and prints both cosine
distributions + their overlap — the empirical answer to "is there a clean
floor" (audit expectation: the distributions OVERLAP; there is no clean valley,
so the fix is architectural, not a threshold).

READ-ONLY: no mint, no store write, no config.ini edit. Uses the live active
model + Vertex reranker (the box is the dev/staging box — experimenting here is
sanctioned). Query embeds cost pennies; Vertex rerank is ~1 call/query.

Run (from repo root):
    Orchestrator/venv/bin/python eval/noise_probe.py
    Orchestrator/venv/bin/python eval/noise_probe.py --calibrate --neg 400
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.chdir(REPO)  # Orchestrator.config reads config.ini relative to CWD
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

from Orchestrator.config import CFG  # noqa: E402
from Orchestrator.embeddings.providers import get_provider  # noqa: E402
from Orchestrator.embeddings.store import get_store, get_active_slug  # noqa: E402
from Orchestrator.fossils import (  # noqa: E402
    load_snapshot_index,
    keyword_retrieve_ids_for_operator,
    extract_snapshot_content,
)
from Orchestrator import retrieval as R  # noqa: E402
from Orchestrator import rerank as RR  # noqa: E402

QUERIES_PATH = REPO / "eval" / "noise_queries.jsonl"
LABELED_PATH = REPO / "eval" / "labeled_set.jsonl"
RESULTS_DIR = REPO / "eval" / "results"
K = 10

ACTIVE = get_active_slug()
STORE = get_store(ACTIVE)
INDEX = load_snapshot_index()
CANDIDATE_N = CFG.getint("retrieval", "candidate_n", fallback=40)
JUNK = R._resolve_junk_floor(STORE)          # the SAME floor retrieve() applies
PASS_CHARS = CFG.getint("rerank", "passage_chars", fallback=4096)


def embed_query(q: str) -> np.ndarray:
    """Active-model query vector via the production provider (retrieval_query)."""
    provider = get_provider(ACTIVE)
    vec = asyncio.run(provider.embed([q], "query"))[0]
    return np.asarray(vec, dtype=np.float32)


def allowed_for(operator: str):
    """retrieve()'s exact operator scoping (retrieval.py:372-377)."""
    if not operator or operator == "system":
        return None
    return {sid for sid, m in INDEX.items() if m.get("operator") == operator}


def _decode(sid: str):
    meta = INDEX.get(sid)
    return R._decode_snapshot_text(meta) if meta else None


def passage_of(sid: str) -> str:
    """Body-only head-cut — identical to the reranker's passage (retrieval.py:264)."""
    text = _decode(sid)
    return extract_snapshot_content(text)[:PASS_CHARS] if text else ""


def snippet_of(sid: str, n: int = 140) -> str:
    text = _decode(sid)
    body = extract_snapshot_content(text) if text else ""
    return re.sub(r"\s+", " ", body).strip()[:n]


def op_of(sid: str) -> str:
    return INDEX.get(sid, {}).get("operator", "?")


# ── noise-tail attribution ─────────────────────────────────────────────────────

def analyze(item: dict) -> dict:
    q = item["query"]
    operator = item.get("operator", "Brandon")
    relevant = set(item.get("relevant", []))

    qv = embed_query(q)
    allow = allowed_for(operator)

    # Replicate retrieve()'s candidate generation EXACTLY (retrieval.py:379-420).
    sem_raw = STORE.search_with_vectors(qv, CANDIDATE_N, allow)
    sem = [(sid, cos) for (sid, cos, _v) in sem_raw if cos >= JUNK]
    cos_by_id = {sid: cos for sid, cos in sem}
    sem_ids = [sid for sid, _ in sem]
    sem_set = set(sem_ids)
    kw_ids = keyword_retrieve_ids_for_operator("", q, CANDIDATE_N, operator or "")
    kw_set = set(kw_ids)

    # Production truth: what Brandon receives (hybrid) + the pure-semantic lane.
    hybrid = R.retrieve(q, operator=operator, k=K)
    semantic_only = R.retrieve(q, operator=operator, k=K, include_keyword=False)
    hybrid_ids = [sid for sid, _ in hybrid]
    semonly_ids = [sid for sid, _ in semantic_only]

    # Raw Vertex rerank score of each delivered item (retrieve() computes these
    # then DISCARDS them at :283 — here we surface them to see if a floor helps).
    passages = [passage_of(sid) for sid in hybrid_ids]
    rr = RR.score(q, passages) if (RR.available() and all(passages)) else None

    rows = []
    for i, (sid, score) in enumerate(hybrid, 1):
        in_sem, in_kw = sid in sem_set, sid in kw_set
        channel = ("both" if in_sem and in_kw else
                   "semantic" if in_sem else
                   "keyword" if in_kw else "scoped/other")
        rows.append({
            "rank": i, "snap_id": sid, "channel": channel,
            "cosine": round(cos_by_id[sid], 4) if sid in cos_by_id else None,
            "rerank": round(rr[i - 1], 4) if rr else None,
            "final_score": round(score, 5),
            "relevant": sid in relevant,
            "operator": op_of(sid),
            "snippet": snippet_of(sid),
        })

    kw_only = [r for r in rows if r["channel"] == "keyword"]
    sem_deliv = [r for r in rows if r["channel"] in ("semantic", "both")]
    evicted = [sid for sid in semonly_ids if sid not in set(hybrid_ids)]

    return {
        "query": q, "operator": operator, "relevant": sorted(relevant),
        "delivered": rows,
        "keyword_only_count": len(kw_only),
        "keyword_only_frac": round(len(kw_only) / len(rows), 3) if rows else 0,
        "semantic_delivered_count": len(sem_deliv),
        "evicted_semantic_hits": evicted,
        "evicted_count": len(evicted),
        "semantic_only_topk": semonly_ids,
        "candidate_counts": {"semantic_after_floor": len(sem_ids),
                             "keyword": len(kw_ids),
                             "keyword_only": len(kw_set - sem_set)},
        "rerank_available": rr is not None,
        "rerank_kwonly_scores": [r["rerank"] for r in kw_only if r["rerank"] is not None],
        "rerank_sem_scores": [r["rerank"] for r in sem_deliv if r["rerank"] is not None],
    }


def print_query(a: dict) -> None:
    print("=" * 100)
    print(f"QUERY: {a['query']!r}   (operator={a['operator']})")
    cc = a["candidate_counts"]
    print(f"  candidates: semantic(after floor {JUNK})={cc['semantic_after_floor']}  "
          f"keyword={cc['keyword']}  keyword-ONLY={cc['keyword_only']}")
    print(f"  {'#':>2} {'chan':<9} {'cosine':>7} {'rerank':>7}  {'snap_id':<22} {'rel':<4} snippet")
    for r in a["delivered"]:
        cos = f"{r['cosine']:.4f}" if r["cosine"] is not None else "   —"
        rr = f"{r['rerank']:.4f}" if r["rerank"] is not None else "   —"
        rel = "REL" if r["relevant"] else ""
        flag = "  <<< KEYWORD-ONLY NOISE" if r["channel"] == "keyword" else ""
        print(f"  {r['rank']:>2} {r['channel']:<9} {cos:>7} {rr:>7}  "
              f"{r['snap_id']:<22} {rel:<4} {r['snippet'][:46]}{flag}")
    print(f"  >> keyword-only delivered: {a['keyword_only_count']}/{len(a['delivered'])} "
          f"({a['keyword_only_frac']:.0%})   "
          f"evicted genuine semantic hits: {a['evicted_count']} {a['evicted_semantic_hits'] or ''}")
    if a["rerank_kwonly_scores"] or a["rerank_sem_scores"]:
        import statistics as st
        def band(xs):
            return f"[{min(xs):.3f}..{max(xs):.3f}] med={st.median(xs):.3f}" if xs else "—"
        print(f"  >> Vertex rerank band  keyword-only: {band(a['rerank_kwonly_scores'])}   "
              f"semantic: {band(a['rerank_sem_scores'])}")


def run_noise(out_date: str) -> None:
    items = [json.loads(ln) for ln in QUERIES_PATH.read_text().splitlines() if ln.strip()]
    print(f"[noise-probe] active={ACTIVE} store.count={STORE.count} schema={STORE.schema} "
          f"junk_floor={JUNK} candidate_n={CANDIDATE_N} rerank_available={RR.available()} "
          f"queries={len(items)}\n")
    t0 = time.time()
    analyses = []
    for item in items:
        a = analyze(item)
        analyses.append(a)
        print_query(a)

    # Aggregate
    n = len(analyses)
    tot_deliv = sum(len(a["delivered"]) for a in analyses)
    tot_kwonly = sum(a["keyword_only_count"] for a in analyses)
    tot_evict = sum(a["evicted_count"] for a in analyses)
    mean_frac = round(sum(a["keyword_only_frac"] for a in analyses) / n, 3) if n else 0
    all_kw = [s for a in analyses for s in a["rerank_kwonly_scores"]]
    all_sem = [s for a in analyses for s in a["rerank_sem_scores"]]
    print("\n" + "=" * 100)
    print("AGGREGATE")
    print(f"  queries={n}  delivered slots={tot_deliv}")
    print(f"  KEYWORD-ONLY (lexical noise) delivered: {tot_kwonly}/{tot_deliv} "
          f"({tot_kwonly / tot_deliv:.0%})   mean per-query fraction: {mean_frac:.0%}")
    print(f"  genuine semantic hits EVICTED by keyword fusion: {tot_evict}")
    if all_kw and all_sem:
        import statistics as st
        print(f"  Vertex rerank score — keyword-only med={st.median(all_kw):.3f} "
              f"vs semantic med={st.median(all_sem):.3f} "
              f"(overlap: kw max={max(all_kw):.3f} vs sem min={min(all_sem):.3f})")
    print(f"  ({time.time() - t0:.0f}s)")

    report = {
        "date": out_date, "active_model": ACTIVE, "k": K, "junk_floor": JUNK,
        "candidate_n": CANDIDATE_N, "rerank_available": RR.available(),
        "aggregate": {
            "queries": n, "delivered_slots": tot_deliv,
            "keyword_only_delivered": tot_kwonly,
            "keyword_only_frac_overall": round(tot_kwonly / tot_deliv, 3) if tot_deliv else 0,
            "keyword_only_frac_mean": mean_frac,
            "semantic_hits_evicted": tot_evict,
        },
        "queries_detail": analyses,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / f"{out_date}-noise-tail.json"
    md_path = RESULTS_DIR / f"{out_date}-noise-tail.md"
    json_path.write_text(json.dumps(report, indent=2))
    md_path.write_text(fmt_noise_md(report))
    print(f"[done] wrote {md_path} and {json_path}")


def fmt_noise_md(report: dict) -> str:
    ag = report["aggregate"]
    lines = [
        f"# Noise-tail / precision probe — {report['date']}",
        "",
        f"Active model `{report['active_model']}`, k={report['k']}, junk_floor="
        f"{report['junk_floor']}, candidate_n={report['candidate_n']}, "
        f"Vertex rerank available={report['rerank_available']}. Channel attribution "
        "replicates retrieve()'s candidate generation; delivered set is the live "
        "production `retrieve()` (hybrid). READ-ONLY.",
        "",
        "## Headline",
        "",
        f"- **Keyword-only (lexical) delivered: {ag['keyword_only_delivered']}/"
        f"{ag['delivered_slots']} ({ag['keyword_only_frac_overall']:.0%})** "
        f"— mean per-query {ag['keyword_only_frac_mean']:.0%}. These snapshots "
        "never cleared any semantic gate; they enter ranking purely on lexical "
        "RRF fusion (retrieval.py:426-429).",
        f"- **Genuine semantic hits EVICTED by keyword fusion: {ag['semantic_hits_evicted']}** "
        "(present in the semantic-only top-k, pushed out of the hybrid top-k).",
        "",
        "## Per-query worksheet (fill `judge:` relevant/borderline/irrelevant for precision@k)",
        "",
    ]
    for a in report["queries_detail"]:
        lines += [f"### {a['query']!r}  (operator={a['operator']}, "
                  f"keyword-only {a['keyword_only_count']}/{len(a['delivered'])}, "
                  f"evicted {a['evicted_count']})", "",
                  "| # | channel | cosine | rerank | snap_id | rel | judge | snippet |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in a["delivered"]:
            cos = f"{r['cosine']:.4f}" if r["cosine"] is not None else "—"
            rr = f"{r['rerank']:.4f}" if r["rerank"] is not None else "—"
            lines.append(
                f"| {r['rank']} | {r['channel']} | {cos} | {rr} | {r['snap_id']} "
                f"| {'REL' if r['relevant'] else ''} |  | {r['snippet'][:70]} |")
        if a["evicted_semantic_hits"]:
            lines.append("")
            lines.append(f"_Evicted semantic hits: {', '.join(a['evicted_semantic_hits'])}_")
        lines.append("")
    return "\n".join(lines)


# ── signal/noise calibration (--calibrate) ─────────────────────────────────────

def run_calibrate(out_date: str, n_neg: int, seed: int) -> None:
    """Positive (query,gold) vs negative (query,random-unrelated) cosine
    distributions — the empirical 'where does relevance live' answer."""
    rng = random.Random(seed)
    rows = [json.loads(ln) for ln in LABELED_PATH.read_text().splitlines() if ln.strip()]
    all_ids = list(INDEX.keys())
    provider = get_provider(ACTIVE)

    pos, neg = [], []
    sample = rows if len(rows) <= n_neg else rng.sample(rows, n_neg)
    print(f"[calibrate] {len(sample)} labeled rows -> positive + 1 negative each "
          f"(active={ACTIVE}, junk_floor={JUNK})")
    qs = [r["query"] for r in sample]
    vecs = []
    B = 16
    for i in range(0, len(qs), B):
        vecs.extend(asyncio.run(provider.embed(qs[i:i + B], "query")))
    for r, qv in zip(sample, vecs):
        q = np.asarray(qv, dtype=np.float32)
        gold = r["gold_snap_id"]
        # positive: the gold's own cosine (max-pool best chunk), full-store rank
        res = STORE.search(q, STORE.count, None)
        cmap = {s: c for s, c in res}
        if gold in cmap:
            pos.append(cmap[gold])
        # negative: a random snapshot that is NOT near this query (rank > 200)
        near = {s for s, _ in res[:200]}
        cand = rng.choice(all_ids)
        tries = 0
        while (cand in near or cand == gold) and tries < 20:
            cand = rng.choice(all_ids)
            tries += 1
        if cand in cmap:
            neg.append(cmap[cand])

    def pct(xs, p):
        if not xs:
            return float("nan")
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(p / 100 * len(xs)))]

    print(f"\n[calibrate] POSITIVE (query->gold) n={len(pos)}  "
          f"NEGATIVE (query->random-unrelated) n={len(neg)}")
    print(f"  {'pctile':>7} {'positive':>9} {'negative':>9}")
    for p in (1, 5, 10, 25, 50, 75, 90, 95, 99):
        print(f"  {p:>6}% {pct(pos, p):>9.4f} {pct(neg, p):>9.4f}")
    overlap_lo, overlap_hi = pct(neg, 90), pct(pos, 10)
    verdict = ("CLEAN GAP" if overlap_hi > overlap_lo and pct(pos, 5) > pct(neg, 95)
               else "OVERLAP — no clean floor")
    print(f"\n  negative p90={pct(neg,90):.4f}  positive p10={pct(pos,10):.4f}  "
          f"-> {verdict}")
    print(f"  current junk_floor={JUNK} sits at negative pctile "
          f"~{sum(1 for x in neg if x < JUNK) / len(neg) * 100:.0f}% / positive pctile "
          f"~{sum(1 for x in pos if x < JUNK) / len(pos) * 100:.0f}%")

    report = {"date": out_date, "active_model": ACTIVE, "junk_floor": JUNK,
              "n_pos": len(pos), "n_neg": len(neg),
              "positive_pctiles": {p: round(pct(pos, p), 4) for p in (1,5,10,25,50,75,90,95,99)},
              "negative_pctiles": {p: round(pct(neg, p), 4) for p in (1,5,10,25,50,75,90,95,99)},
              "verdict": verdict}
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"{out_date}-noise-calibration.json").write_text(json.dumps(report, indent=2))
    print(f"[done] wrote {RESULTS_DIR / (out_date + '-noise-calibration.json')}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-date", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--calibrate", action="store_true",
                    help="signal/noise distribution instead of the noise-tail probe")
    ap.add_argument("--neg", type=int, default=300, help="calibrate: rows to sample")
    ap.add_argument("--seed", type=int, default=13, help="calibrate: RNG seed")
    args = ap.parse_args(argv)
    if args.calibrate:
        run_calibrate(args.out_date, args.neg, args.seed)
    else:
        run_noise(args.out_date)


if __name__ == "__main__":
    main()
