"""Canonical snapshot retrieval — the single ranking core for every surface.

Pipeline: candidate generation (keyword ids + semantic ids/vectors from the
ACTIVE store) -> RRF fusion (scale-free) -> mild recency tie-break (relevance
dominates; recency flips only near-ties) -> MMR diversity (drops near-duplicate
session clusters) -> top-k. A low junk floor replaces the old hard 0.60
threshold. No rerank (offline-capable, incl. the on-device phone profile).
All semantic candidates come from get_active_store(), so the retriever is
automatically correct for whatever embedding model is active.

This module is the CORE only (Phase 3a). It is intentionally NOT wired into any
surface (hybrid_retrieve / /fossil/hybrid / context_builder) — that is Phase 3b.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np

from Orchestrator.config import CFG, VOL_PATH
from Orchestrator.embeddings import search as _emb
from Orchestrator.fossils import (
    keyword_retrieve_ids_for_operator,
    load_snapshot_index,
)
from Orchestrator.volume import read_text_safe


# ── pure ranking primitives ───────────────────────────────────────────────────

def rrf_fuse(rankings: dict[str, list[str]], c: int = 60) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion. score(d) = sum over channels of 1/(c + rank).

    Scale-free: only each id's *rank position* in a channel matters, never the
    channel's raw score magnitude — which is exactly what fixes the TF-IDF (keyword)
    vs cosine (semantic) magnitude mismatch. An id near the top of multiple
    channels accumulates the most; agreement across channels is rewarded.
    """
    scores: dict[str, float] = {}
    for ids in rankings.values():
        for rank, sid in enumerate(ids):
            scores[sid] = scores.get(sid, 0.0) + 1.0 / (c + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def _age_days(ts_iso: str, now: datetime) -> float:
    """Age in days from an ISO-8601 timestamp; 3650 (≈10y) on any parse failure
    so an unparseable/missing timestamp gets ~zero recency boost (never #1)."""
    try:
        t = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, (now - t).total_seconds() / 86400.0)
    except Exception:
        return 3650.0


def apply_recency_tiebreak(
    relevance: dict[str, float],
    age_days: dict[str, float],
    weight: float,
    half_life_days: float,
) -> list[tuple[str, float]]:
    """final = relevance + weight * 2^(-age/half_life). Small weight => tie-break.

    The boost is bounded by `weight` (max at age 0), so with the locked
    weight=0.05 a fresh snapshot can out-rank an equally-relevant old one but
    can NEVER overturn a clearly-better older match (relevance dominates). An id
    absent from age_days is treated as ancient (3650d) -> negligible boost.
    """
    out: dict[str, float] = {}
    for sid, rel in relevance.items():
        boost = weight * math.pow(2.0, -age_days.get(sid, 3650.0) / half_life_days)
        out[sid] = rel + boost
    return sorted(out.items(), key=lambda kv: kv[1], reverse=True)


def mmr_select(cands, k: int, lam: float) -> list[str]:
    """MMR over (snap_id, relevance, unit_vector); greedily max lam*rel-(1-lam)*max_sim.

    Greedy Maximal Marginal Relevance: each pick maximizes
    `lam * relevance - (1 - lam) * max_cosine_to_already_picked`, so a candidate
    that is a near-duplicate of something already chosen is penalized — breaking
    near-identical session clusters while still preferring relevant items.
    `cands` is consumed in order; ties keep the earlier (more relevant) item.
    """
    picked: list[str] = []
    picked_vecs: list = []
    pool = list(cands)
    while pool and len(picked) < k:
        best_i, best_val = 0, -1e9
        for i, (_sid, rel, vec) in enumerate(pool):
            sim = max((float(vec @ pv) for pv in picked_vecs), default=0.0)
            val = lam * rel - (1.0 - lam) * sim
            if val > best_val:
                best_val, best_i = val, i
        sid, _rel, vec = pool.pop(best_i)
        picked.append(sid)
        picked_vecs.append(vec)
    return picked


# ── orchestrating retrieve() ───────────────────────────────────────────────────

def retrieve(query: str, operator: str = "", k: int = 10, *, include_keyword: bool = True,
             store=None, query_vector=None):
    """Canonical ranked retrieval -> [(snap_id, score), ...] top-k.

    operator ""/"system" = all operators; any other operator restricts to its own
    snapshots. include_keyword=False yields a semantic-only path for lean profiles
    (e.g. the on-device phone) that lack the volume text. Returns [] when the query
    can't be embedded or the active store is empty/unavailable — never raises
    (on the production path; see query_vector below).

    store / query_vector (keyword-only, eval seam — WI-6): when `store` is given,
    semantic candidates come from that VectorStore instead of get_active_store(),
    so candidate chunk stores get benched pre-swap through the FULL ranking
    pipeline. When `query_vector` is given, it is used verbatim instead of
    embedding the query with the ACTIVE model — required for benching a
    non-active arm (e.g. a qwen store needs a qwen query vector); it is
    dims-checked against the store and RAISES ValueError on mismatch (an eval
    harness bug must fail loud, not bench garbage). Production callers never
    pass either; default behavior is byte-identical.
    """
    if not query or not query.strip():
        return []

    # 1. config knobs from [retrieval] (operator-locked defaults).
    candidate_n = CFG.getint("retrieval", "candidate_n", fallback=40)
    rrf_c = CFG.getint("retrieval", "rrf_c", fallback=60)
    recency_weight = CFG.getfloat("retrieval", "recency_weight", fallback=0.05)
    half_life = CFG.getfloat("retrieval", "recency_half_life_days", fallback=90.0)
    mmr_lambda = CFG.getfloat("retrieval", "mmr_lambda", fallback=0.7)
    junk_floor = CFG.getfloat("retrieval", "junk_floor", fallback=0.40)
    debug_log = CFG.getboolean("retrieval", "debug_log", fallback=False)

    # 2. embed the query (purpose="query" — the retrieval_query fix), unless the
    #    eval seam supplied a pre-embedded vector for a non-active arm's model.
    if query_vector is not None:
        qv = list(query_vector)
    else:
        qv = _emb.generate_embedding_sync(query, purpose="query")
    if not qv:
        return []
    if store is None:
        try:
            store = _emb.get_active_store()
        except Exception as e:  # noqa: BLE001 - corrupt dir / dims mismatch
            print(f"[RETRIEVAL] active store unavailable ({e}); returning no results")
            return []
    if query_vector is not None:
        store_dims = getattr(store, "dims", None)
        if store_dims is not None and len(qv) != store_dims:
            raise ValueError(
                f"query_vector has {len(qv)} dims but store "
                f"{getattr(store, 'slug', '?')} expects {store_dims} (eval seam misuse)"
            )
    if store.count == 0:
        return []

    qv_np = np.asarray(qv, dtype=np.float32)
    qdim = qv_np.shape[0]

    # 3. operator scoping (None == see everything).
    allowed_ids = None
    index = load_snapshot_index()
    if operator and operator != "system":
        allowed_ids = {
            sid for sid, meta in index.items() if meta.get("operator") == operator
        }

    # 4. semantic candidates WITH vectors; drop junk-floor misses, keep vectors.
    sem = store.search_with_vectors(qv_np, candidate_n, allowed_ids)
    sem = [(sid, cos, vec) for (sid, cos, vec) in sem if cos >= junk_floor]
    sem_ids = [sid for sid, _cos, _vec in sem]
    vec_by_id = {sid: vec for sid, _cos, vec in sem}

    # 5. keyword candidates (skipped for lean/semantic-only profiles).
    kw_ids: list[str] = []
    if include_keyword:
        try:
            # The index-backed keyword path decodes snapshots from byte offsets on
            # demand and IGNORES vol_txt entirely; vol_txt is only consumed in the
            # no-index fallback. So pay the ~35MB full-volume read+decode (a ~250MB
            # transient spike) ONLY when there is no index — otherwise pass "".
            vol_txt = read_text_safe(VOL_PATH) if not index else ""
            kw_ids = keyword_retrieve_ids_for_operator(
                vol_txt, query, candidate_n, operator or ""
            )
        except Exception as e:  # noqa: BLE001 - keyword channel is best-effort
            print(f"[RETRIEVAL] keyword channel unavailable ({e}); semantic-only")
            kw_ids = []

    # 6. RRF fusion of the two rank lists (scale-free).
    rankings = {"semantic": sem_ids}
    if kw_ids:
        rankings["keyword"] = kw_ids
    fused = rrf_fuse(rankings, c=rrf_c)
    if not fused:
        return []
    relevance = dict(fused)

    # 7. mild recency tie-break from index timestamps.
    now = datetime.now(timezone.utc)
    ages = {
        sid: _age_days(index.get(sid, {}).get("timestamp", ""), now)
        for sid in relevance
    }
    ranked = apply_recency_tiebreak(relevance, ages, recency_weight, half_life)
    score_by_id = dict(ranked)

    # 8. MMR diversity over the top window, then take top-k.
    #    Keyword-only ids (absent from the semantic channel) get a zero vector of
    #    the query's dim so they never register as near-duplicates of anything.
    window = max(k * 2, 20)
    zero = np.zeros(qdim, dtype=np.float32)
    mmr_cands = [
        (sid, score_by_id[sid], vec_by_id.get(sid, zero))
        for sid, _score in ranked[:window]
    ]
    picked = mmr_select(mmr_cands, k, mmr_lambda)
    results = [(sid, score_by_id[sid]) for sid in picked]

    # 9. OPT-IN provenance logging — answers "why did I get these results" from
    #    logs alone. Cheap: only computed when [retrieval] debug_log = true, and
    #    only over the final top-k. Does NOT touch ranking. The `rrf` field is the
    #    pre-recency RRF relevance; `recency_boost` is the additive tie-break term;
    #    `channels` shows which of semantic/keyword surfaced the id.
    if debug_log:
        sem_set = set(sem_ids)
        kw_set = set(kw_ids)
        for sid, final in results:
            rel = relevance.get(sid, 0.0)
            age = ages.get(sid, 3650.0)
            boost = final - rel
            chans = "+".join(
                c for c, present in
                (("semantic", sid in sem_set), ("keyword", sid in kw_set))
                if present
            ) or "none"
            print(
                f"[RETRIEVAL] q={query[:40]!r} -> sid={sid} "
                f"rrf={rel:.6f} age_days={age:.1f} recency_boost={boost:.6f} "
                f"final={final:.6f} channels={chans}"
            )

    return results
