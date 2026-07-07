#!/usr/bin/env python3
"""Ranking sandbox — sweep retrieval levers against the ground-truth benchmark.

Loads the controlled corpus (eval/ground_truth/), embeds it into an ISOLATED
scratch VectorStore per embedding model (no live-ledger pollution, portable
across models), and runs a PARAMETERIZED re-implementation of the pipeline that
reuses production's own primitives (rrf_fuse / mmr_select from retrieval.py, the
real Vertex reranker via rerank.py) while exposing every fix lever as a knob:

  keyword_mode : fused (current) | gated | dedup | off
  semantic_floor : cosine admission floor
  rerank : off | rankspace (current: order only) | preserve (keep absolute score)
  rerank_floor : drop pool members below this absolute rerank score
  variable_k : return fewer than k when few survive (vs pad to k)

Because relevance is KNOWN (gold = a topic's cluster; hard-negatives = the
lexical decoys engineered to share its trap tokens), we get TRUE precision@k,
recall@k, nDCG@k, noise-count and decoy-injection-rate — the metrics the real
corpus can't give. READ-ONLY wrt the live BlackBox.

Run:  Orchestrator/venv/bin/python eval/rank_sandbox.py [--slug gemini-embedding-2]
"""
from __future__ import annotations
import argparse
import asyncio
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.chdir(REPO)
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402
from Orchestrator.embeddings.store import get_store  # noqa: E402
from Orchestrator.embeddings.chunker import chunks_for_snapshot  # noqa: E402
from Orchestrator.embeddings.providers import get_provider  # noqa: E402
from Orchestrator.embeddings.registry import EMBEDDING_MODELS  # noqa: E402
from Orchestrator.retrieval import rrf_fuse, mmr_select  # noqa: E402
from Orchestrator.fossils import extract_snapshot_content  # noqa: E402
from Orchestrator import rerank as RR  # noqa: E402

GT = REPO / "eval" / "ground_truth"
PASS_CHARS = 4096
K = 10
CAND_N = 40
RRF_C = 60


# ── load .env for Vertex/Gemini creds (rerank + embed) ────────────────────────
for ln in (REPO / ".env").read_text().splitlines() if (REPO / ".env").exists() else []:
    if "=" in ln and not ln.strip().startswith("#"):
        k, _, v = ln.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def embed(slug, texts, purpose):
    return asyncio.run(get_provider(slug).embed(texts, purpose))


def corpus_store(slug):
    """Embed the ground-truth corpus into a per-model scratch store (persisted
    under eval/ground_truth/_store_<slug>/ so re-runs are instant). Returns
    (store, {sid: envelope_text}, [all_sids])."""
    corpus = [json.loads(l) for l in (GT / "corpus.jsonl").read_text().splitlines() if l.strip()]
    bodies = {c["sid"]: c["envelope_text"] for c in corpus}
    dims = EMBEDDING_MODELS[slug]["dims"]
    store = get_store(slug, dims=dims, base_dir=GT / "_store", schema=2, content_mode="full")
    have = store.ids()
    todo = [c for c in corpus if c["sid"] not in have]
    if todo:
        print(f"[store:{slug}] embedding {len(todo)} snapshots into scratch store...")
        for c in todo:
            chunks = chunks_for_snapshot(c["envelope_text"], model_key=slug, content_mode="full")
            vecs = embed(slug, chunks, "document")
            store.append_group(c["sid"], vecs)
    print(f"[store:{slug}] rows={store.rows} snapshots={store.snapshots}")
    return store, bodies, [c["sid"] for c in corpus]


# ── representative keyword lane (models production's lexical injection) ────────
def keyword_rank(bodies, query, n):
    """TF-IDF over corpus bodies — a lexical scorer that reproduces the
    injection behavior of fossils.keyword_retrieve (a snapshot sharing the
    query's rare tokens scores high regardless of topic)."""
    terms = [t for t in ''.join(ch.lower() if ch.isalnum() else ' ' for ch in query).split() if len(t) > 2]
    docs = {sid: extract_snapshot_content(txt).lower() for sid, txt in bodies.items()}
    N = len(docs)
    df = {t: sum(1 for d in docs.values() if t in d) for t in set(terms)}
    scored = []
    for sid, d in docs.items():
        s = 0.0
        for t in terms:
            tf = d.count(t)
            if tf:
                s += tf * math.log(1 + N / (1 + df[t]))
        if s > 0:
            scored.append((sid, s))
    scored.sort(key=lambda x: -x[1])
    return [sid for sid, _ in scored[:n]]


# ── the parameterized pipeline ────────────────────────────────────────────────
DEFAULT = dict(keyword_mode="fused", semantic_floor=0.40, keyword_gate=0.55,
               rerank="rankspace", rerank_floor=None, output_cos_floor=None,
               variable_k=False, mmr_lambda=0.85, mmr_protect=3, k=K)


def rank(query, cfg, store, bodies, all_sids, qv=None):
    slug = store.slug
    if qv is None:
        qv = np.asarray(embed(slug, [query], "query")[0], dtype=np.float32)
    # cosine for ALL corpus sids (small corpus) -> full visibility
    sem_all = store.search_with_vectors(qv, len(all_sids), None)
    cos_by_id = {sid: cos for sid, cos, _v in sem_all}
    vec_by_id = {sid: v for sid, _c, v in sem_all}
    sem_ids = [sid for sid, cos, _v in sem_all[:CAND_N] if cos >= cfg["semantic_floor"]]
    sem_set = set(sem_ids)

    kw_all = keyword_rank(bodies, query, CAND_N)
    mode = cfg["keyword_mode"]
    if mode == "off":
        kw_ids = []
    elif mode == "fused":
        kw_ids = kw_all
    elif mode == "dedup":                       # agreement only: never injects
        kw_ids = [s for s in kw_all if s in sem_set]
    elif mode == "gated":                       # keyword must clear a cosine gate
        kw_ids = [s for s in kw_all if cos_by_id.get(s, 0.0) >= cfg["keyword_gate"]]
    else:
        raise ValueError(mode)
    kw_set = set(kw_ids)

    rankings = {"semantic": sem_ids}
    if kw_ids:
        rankings["keyword"] = kw_ids
    fused = rrf_fuse(rankings, c=RRF_C)
    if not fused:
        return [], {}
    relevance = dict(fused)
    ranked = sorted(relevance.items(), key=lambda kv: kv[1], reverse=True)
    pre_floor_order = [s for s, _ in ranked]

    rr_by = {}
    if cfg["rerank"] != "off" and RR.available():
        pool = [s for s, _ in ranked[:CAND_N]]
        passages = [extract_snapshot_content(bodies[s])[:PASS_CHARS] for s in pool]
        scores = RR.score(query, passages)
        if scores and len(scores) == len(pool):
            rr_by = {pool[i]: scores[i] for i in range(len(pool))}
            if cfg["rerank"] == "rankspace":
                order = sorted(range(len(pool)), key=lambda i: scores[i], reverse=True)
                new_order = [pool[i] for i in order] + [s for s, _ in ranked[len(pool):]]
                relevance = {sid: 1.0 / (RRF_C + r) for r, sid in enumerate(new_order)}
            else:  # preserve absolute rerank score as relevance
                relevance = dict(rr_by)
                for s, _ in ranked[len(pool):]:
                    relevance[s] = 0.0
            # absolute rerank floor: drop sub-floor pool members
            if cfg["rerank_floor"] is not None:
                relevance = {sid: v for sid, v in relevance.items()
                             if rr_by.get(sid, 1.0) >= cfg["rerank_floor"]}
            ranked = sorted(relevance.items(), key=lambda kv: kv[1], reverse=True)

    # output cosine floor (the reranker-ABSENT relevance gate: drop final
    # candidates whose raw cosine is below the floor — enables variable-k on a
    # box with no cross-encoder).
    if cfg["output_cos_floor"] is not None:
        relevance = {sid: v for sid, v in relevance.items()
                     if cos_by_id.get(sid, 0.0) >= cfg["output_cos_floor"]}
        ranked = sorted(relevance.items(), key=lambda kv: kv[1], reverse=True)

    # MMR over the surviving window
    window = max(cfg["k"] * 2, 20)
    dims = qv.shape[0]
    zero = np.zeros(dims, dtype=np.float32)
    cands = [(sid, relevance[sid], vec_by_id.get(sid, zero)) for sid, _ in ranked[:window]]
    protect = cfg["mmr_protect"] if len(rankings) > 1 else 0
    picked = mmr_select(cands, cfg["k"], cfg["mmr_lambda"], protect=protect)

    # pad-to-k (current behavior) vs variable-k (return fewer)
    if not cfg["variable_k"] and len(picked) < cfg["k"]:
        for s in pre_floor_order:
            if s not in picked:
                picked.append(s)
                if len(picked) >= cfg["k"]:
                    break
    chan = {sid: ("both" if sid in sem_set and sid in kw_set else
                  "semantic" if sid in sem_set else
                  "keyword" if sid in kw_set else "other") for sid in picked}
    return picked, chan


# ── evaluation ────────────────────────────────────────────────────────────────
def ndcg(delivered, gold, k):
    dcg = sum(1.0 / math.log2(i + 2) for i, s in enumerate(delivered[:k]) if s in gold)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(min(len(gold), k)))
    return dcg / idcg if idcg else 0.0


def evaluate(cfg, store, bodies, all_sids, queries, qvecs):
    agg = defaultdict(float)
    chan_noise = defaultdict(int)
    n = len(queries)
    for q in queries:
        gold = set(q["gold_sids"])
        hardneg = set(q["hard_negative_sids"])
        picked, chan = rank(q["query"], cfg, store, bodies, all_sids, qv=qvecs[q["query"]])
        dk = picked[:cfg["k"]]
        rel = [s for s in dk if s in gold]
        agg["precision"] += len(rel) / len(dk) if dk else 0
        agg["recall"] += len(rel) / len(gold) if gold else 0
        agg["ndcg"] += ndcg(dk, gold, cfg["k"])
        agg["noise"] += len([s for s in dk if s not in gold])
        agg["decoy_inj"] += len([s for s in dk if s in hardneg])
        agg["delivered"] += len(dk)
        for s in dk:
            if s not in gold:
                chan_noise[chan.get(s, "other")] += 1
    return {"precision": agg["precision"] / n, "recall": agg["recall"] / n,
            "ndcg": agg["ndcg"] / n, "noise_per_q": agg["noise"] / n,
            "decoy_per_q": agg["decoy_inj"] / n, "avg_delivered": agg["delivered"] / n,
            "noise_by_channel": dict(chan_noise)}


CONFIGS = [
    ("baseline (production)", {}),
    ("A: keyword=dedup", dict(keyword_mode="dedup")),
    ("A: keyword=gated@.55", dict(keyword_mode="gated", keyword_gate=0.55)),
    ("D: semantic_floor=.60", dict(semantic_floor=0.60)),
    ("C: rerank preserve+floor.02 +varK", dict(rerank="preserve", rerank_floor=0.02, variable_k=True)),
    ("C: rerank preserve+floor.03 +varK", dict(rerank="preserve", rerank_floor=0.03, variable_k=True)),
    ("A+B+C gated+floor.02+varK", dict(keyword_mode="gated", keyword_gate=0.55,
                                       rerank="preserve", rerank_floor=0.02, variable_k=True)),
    ("A+B+C dedup+floor.02+varK", dict(keyword_mode="dedup",
                                       rerank="preserve", rerank_floor=0.02, variable_k=True)),
    ("FULL gated+floor.03+semfloor.55+varK", dict(keyword_mode="gated", keyword_gate=0.55,
                                                  semantic_floor=0.55, rerank="preserve",
                                                  rerank_floor=0.03, variable_k=True)),
    # ── reranker-ABSENT path (on-device: no cross-encoder) ──────────────────
    ("noRerank baseline (fused)", dict(rerank="off")),
    ("noRerank A+D gated+semfloor.62", dict(rerank="off", keyword_mode="gated",
                                            keyword_gate=0.62, semantic_floor=0.62)),
    ("noRerank A+B+D gated+cosfloor.65+varK", dict(rerank="off", keyword_mode="gated",
                                                   keyword_gate=0.62, semantic_floor=0.62,
                                                   output_cos_floor=0.65, variable_k=True)),
    ("noRerank dedup+cosfloor.65+varK", dict(rerank="off", keyword_mode="dedup",
                                             semantic_floor=0.55, output_cos_floor=0.65,
                                             variable_k=True)),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default="gemini-embedding-2")
    args = ap.parse_args()

    queries = [json.loads(l) for l in (GT / "queries.jsonl").read_text().splitlines() if l.strip()]
    store, bodies, all_sids = corpus_store(args.slug)
    print(f"[sandbox] slug={args.slug} corpus={len(all_sids)} queries={len(queries)} "
          f"rerank_available={RR.available()}\n")

    # pre-embed queries once (shared across all configs)
    qvecs = {q["query"]: np.asarray(embed(args.slug, [q["query"]], "query")[0], dtype=np.float32)
             for q in queries}

    print(f"{'config':<40} {'prec':>5} {'recall':>6} {'nDCG':>5} {'noise/q':>7} "
          f"{'decoy/q':>7} {'deliv':>5}  noise-by-channel")
    print("-" * 110)
    results = []
    for name, over in CONFIGS:
        cfg = dict(DEFAULT); cfg.update(over)
        m = evaluate(cfg, store, bodies, all_sids, queries, qvecs)
        results.append({"config": name, "cfg": over, **m})
        print(f"{name:<40} {m['precision']:>5.2f} {m['recall']:>6.2f} {m['ndcg']:>5.2f} "
              f"{m['noise_per_q']:>7.2f} {m['decoy_per_q']:>7.2f} {m['avg_delivered']:>5.1f}  "
              f"{m['noise_by_channel']}")

    out = REPO / "eval" / "results" / "2026-07-07-sandbox-sweep.json"
    out.write_text(json.dumps({"slug": args.slug, "k": K, "results": results}, indent=2))
    print(f"\n[done] wrote {out}")


if __name__ == "__main__":
    main()
