"""Canonical snapshot retrieval — the single ranking core for every surface.

Pipeline: candidate generation (keyword ids + semantic ids/vectors from the
ACTIVE store) -> RRF fusion (scale-free) -> mild recency tie-break (relevance
dominates; recency flips only near-ties) -> MMR diversity (drops near-duplicate
session clusters; the fused top-P is protected from elimination — see
mmr_select) -> top-k. A low junk floor replaces the old hard 0.60
threshold (per-model registry floors are available behind
[retrieval] registry_floor_enabled, default false — see _resolve_junk_floor).
An OPTIONAL cross-encoder rerank stage ([retrieval] rerank_enabled, default
false — M11/WI-4, audit A9) can reorder the post-recency pool; off (the
default, and whenever Orchestrator/rerank.py has no live provider) the
pipeline is byte-identical to the historical no-rerank path and stays
offline-capable, incl. the on-device phone profile.
All semantic candidates come from get_active_store(), so the retriever is
automatically correct for whatever embedding model is active.

This module is the CORE only (Phase 3a). It is intentionally NOT wired into any
surface (hybrid_retrieve / /fossil/hybrid / context_builder) — that is Phase 3b.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

import numpy as np

from Orchestrator.config import CFG, VOL_PATH
from Orchestrator.embeddings import search as _emb
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.fossils import (
    extract_snapshot_content,
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

    The boost is bounded by `weight` (max at age 0). Post-RRF, relevance scores
    span only ~0.0066 (1/60 - 1/99 at candidate_n=40, rrf_c=60), so the weight
    must be SMALLER than that span for "relevance dominates" to actually hold:
    0.005 (measured, eval/results/2026-07-02-recency-sweep.md) flips genuine
    near-ties but cannot displace a clearly-better older match. The original
    0.05 was ~7.6x the span and demoted older golds by whole ranks (r@10
    0.268 -> 0.489 when corrected). An id absent from age_days is treated as
    ancient (3650d) -> negligible boost.
    """
    out: dict[str, float] = {}
    for sid, rel in relevance.items():
        boost = weight * math.pow(2.0, -age_days.get(sid, 3650.0) / half_life_days)
        out[sid] = rel + boost
    return sorted(out.items(), key=lambda kv: kv[1], reverse=True)


def mmr_select(cands, k: int, lam: float, protect: int = 0) -> list[str]:
    """MMR over (snap_id, relevance, unit_vector); greedily max lam*rel-(1-lam)*max_sim.

    Greedy Maximal Marginal Relevance: each pick maximizes
    `lam * relevance - (1 - lam) * max_cosine_to_already_picked`, so a candidate
    that is a near-duplicate of something already chosen is penalized — breaking
    near-identical session clusters while still preferring relevant items.
    `cands` is consumed in order; ties keep the earlier (more relevant) item.

    protect (M6f iteration 3 — top-rank protect): `cands` arrives in fused rank
    order, and the first `protect` of them are seeded into the picked set IN
    RANK ORDER (their vectors join the diversity comparison) before the greedy
    loop fills the remaining k-protect slots. WHY: items the fused ranking puts
    in the top-P are definitionally the strongest cross-channel evidence —
    MMR's job is breaking near-duplicate clusters in the TAIL of top-k, not
    vetoing the strongest results. Measured basis: two human-verified golds at
    post-RRF+recency rank 3, present in BOTH channels, were MMR-dropped as
    near-duplicates of a first-picked same-domain sibling at every lambda <
    1.0 (eval/results/2026-07-03-wholevec-gate.md). Protected items count
    toward k; protect=0 is exactly the historical behavior; protect >= k
    degrades to the pure fused ranking.
    """
    picked: list[str] = []
    picked_vecs: list = []
    pool = list(cands)
    n_protect = max(0, min(int(protect), k, len(pool)))
    for sid, _rel, vec in pool[:n_protect]:
        picked.append(sid)
        picked_vecs.append(vec)
    pool = pool[n_protect:]
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

def _resolve_junk_floor(store) -> float:
    """Per-retrieval junk floor (WI-3/M9, audit A8): the store's registry
    `junk_floor` when `[retrieval] registry_floor_enabled` is true AND the
    store's model declares one (non-null); otherwise the global
    `[retrieval] junk_floor` (default 0.40). Flag default false ⇒ the
    per-model floors are inert and behavior is byte-identical to the
    historical single-knob path.

    The floor is a NOISE floor (drop obvious junk), never relevance
    selection: on the live chunk-max store the measured gap between the noise
    ceiling (0.6125) and the relevance band (≥0.6256) is only +0.013
    (scripts/calibrate_threshold.py run 2026-07-02 — the script itself warned
    the bands are too close to select on), so ranking does relevance and the
    calibrated floors sit well below the band (gemini-2: 0.55).

    Resolution keys on the STORE's slug, not the active pointer, so the eval
    seam (`retrieve(store=...)`, M4) benches a candidate arm with the floor
    that arm would ship with. Per-model floors exist because one global value
    cannot fit every score distribution — audit A8's wipe scenario: qwen3-0.6b
    scores on-topic hits ~0.45, so a gemini-band 0.54 floor empties the
    phone-lean semantic-only profile entirely (invariant-2 violation); qwen's
    own measured noise floor is 0.35. An unknown or slug-less store falls back
    to the global floor.
    """
    fallback = CFG.getfloat("retrieval", "junk_floor", fallback=0.40)
    if not CFG.getboolean("retrieval", "registry_floor_enabled", fallback=False):
        return fallback
    entry = EMBEDDING_MODELS.get(getattr(store, "slug", None), {})
    value = entry.get("junk_floor")
    return float(value) if value is not None else fallback


# ── precision levers (WI-8 / precision fix, plan 2026-07-07) ──────────────────
# All default to CURRENT behavior so retrieve() is byte-identical until an
# operator opts in. Per-model floors (registry `cos_floor`) resolve only when
# [retrieval] registry_floor_enabled is true — the same flag that gates the
# per-model junk_floor — so a calibrated box lights up all its floors together.

def _resolve_cos_floor(store) -> float:
    """Output cosine floor (the reranker-ABSENT relevance gate AND the
    gated-keyword cosine gate). Global [retrieval] output_cos_floor (default
    0.0 = inert) unless registry_floor_enabled AND the active model declares a
    calibrated `cos_floor` (measured gold-cosine p10 — see M2 calibration)."""
    g = CFG.getfloat("retrieval", "output_cos_floor", fallback=0.0)
    if CFG.getboolean("retrieval", "registry_floor_enabled", fallback=False):
        v = EMBEDDING_MODELS.get(getattr(store, "slug", None), {}).get("cos_floor")
        if v is not None:
            return float(v)
    return g


def _resolve_rerank_floor() -> float:
    """Absolute cross-encoder-score floor (drop pool members below it). Global
    [retrieval] rerank_floor (default 0.0 = inert); a per-reranker calibrated
    value lands in the rerank sidecar (M2) and is read by _apply_rerank."""
    return CFG.getfloat("retrieval", "rerank_floor", fallback=0.0)


# ── optional cross-encoder rerank stage (M11/WI-4, audit A9) ──────────────────

# Fall-through is logged ONCE per process (a dead reranker on a hot path must
# not spam the journal on every retrieve).
_rerank_fallthrough_logged = False


def _log_rerank_fallthrough(reason: str) -> None:
    global _rerank_fallthrough_logged
    if not _rerank_fallthrough_logged:
        print(f"[RERANK] {reason}; falling through to the un-reranked "
              "ranking (logged once per process)")
        _rerank_fallthrough_logged = True


def _decode_snapshot_text(meta) -> "str | None":
    """Byte-offset decode of ONE snapshot from the volume (rerank passage
    source). Targeted seek-read — a few KB per candidate, NOT the ~35MB
    whole-volume read the no-index keyword fallback pays. None on any
    failure (missing offsets, short file, IO error) — never raises."""
    try:
        start, end = int(meta["byte_start"]), int(meta["byte_end"])
        if end <= start:
            return None
        with open(VOL_PATH, "rb") as f:
            f.seek(start)
            raw = f.read(end - start)
        if len(raw) < end - start:
            return None  # offsets beyond EOF (index/volume drift)
        return raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - rerank is best-effort, never raises
        return None


def _best_passage_window(body: str, query: str, width: int) -> str:
    """Pick the `width`-char window of `body` richest in the query's terms (M4).

    The default rerank passage is a blind head-cut (body[:width]); on a long
    snapshot that clips the relevant turn before the reranker sees it. This
    slides a window (half-width stride, cheap: O(len(body)/stride * n_terms))
    and returns the span with the most query-term hits, so the cross-encoder
    judges each snapshot's MOST-relevant body window instead of its head.
    Falls back to the head-cut when the body already fits or the query has no
    usable terms (byte-identical to head mode in those cases)."""
    if len(body) <= width:
        return body
    terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2]
    if not terms:
        return body[:width]
    low = body.lower()
    stride = max(1, width // 2)
    best_start, best_score = 0, -1
    for start in range(0, len(body), stride):
        win = low[start:start + width]
        score = sum(win.count(t) for t in terms)
        if score > best_score:
            best_score, best_start = score, start
    # A zero-hit body (only envelope/generic text matched) keeps the head — no
    # window is more representative than another, so don't drift off the head.
    return body[best_start:best_start + width] if best_score > 0 else body[:width]


def _apply_rerank(query, ranked, index, rrf_c,
                  rerank_relevance: str = "rankspace", rerank_floor: float = 0.0,
                  passage_mode: str = "head"):
    """Cross-encoder rerank of the post-recency pool -> new relevance dict, or
    None to fall through un-reranked (never raises).

    rerank_relevance (precision fix): "rankspace" (default, byte-identical)
    maps the reranked ORDER to 1/(rrf_c + new_rank) — the cross-encoder's
    absolute magnitude is discarded. "preserve" keeps the raw 0–1 relevance
    score AS the relevance, so a genuinely low-relevance candidate keeps a low
    final score (and the recency tie-break becomes a true tie-break on the 0–1
    scale). rerank_floor > 0 DROPS pool members whose absolute score is below
    the floor (the reranker becomes a bouncer, not just a re-orderer) — the
    dropped ids leave the relevance dict entirely, so downstream MMR/delivery
    return fewer than k (no padding). Both default to today's behavior.

    A9-corrected placement: the pool is the top
    min([retrieval] rerank_candidate_n (fallback 40), len(ranked)) of the
    POST-recency ordering. One passage per pool member, all on ONE scale
    ([rerank] passage_chars, fallback 4096 ≈ the 1024-token chunk budget):
    the BODY-ONLY content of each snapshot — extract_snapshot_content(text)
    head-truncated to passage_chars.

    M14.2 (measured, 2026-07-04): passages are body-only, NOT the M8
    chunk-ordinal window. The envelope (=== START SNAPSHOT === / CROSS-FILE
    BEACON / VOLUME TRACKER / GAUGES) is near-identical across the corpus and
    measurably sabotages cross-encoders — reranking on body-only text lifted
    Vertex recall@10 0.654 -> 0.846 (+29%). The chunk-ordinal window was
    designed for envelope-inclusive chunk space and is inconsistent with body
    extraction; the body head (user message + AI response start) is the most
    representative content for reranking, so keyword-only and chunk-provenance
    candidates alike are scored on the clean body head (audit A9's
    same-scale-for-all invariant holds — the ordinal is simply no longer
    consulted). Limitation: a plain head-cut can clip a very long body before
    its most relevant turn; a smarter body-window is a possible refinement, not
    this task.

    Any pool member that cannot be decoded aborts the stage (partial pools
    cannot be ranked on one scale) -> None.

    The reranked ORDER (stable: score ties keep the incoming post-recency
    order) maps back into rank-space relevance 1/(rrf_c + new_rank) over the
    FULL list — reranked pool first, un-reranked tail after in its original
    order, so the tail can never leapfrog the pool. The caller RE-APPLIES
    apply_recency_tiebreak on this dict: rank-space keeps the post-RRF span
    the recency weight was calibrated against (~0.0066 across 40 candidates
    vs boost <= 0.005), so freshness keeps exactly its tie-break influence
    on the new ordering.
    """
    try:
        from Orchestrator import rerank as _rerank  # lazy: flag-on path only
        if not _rerank.available():
            return None
        pool_n = min(
            CFG.getint("retrieval", "rerank_candidate_n", fallback=40),
            len(ranked),
        )
        if pool_n <= 0:
            return None
        passage_chars = CFG.getint("rerank", "passage_chars", fallback=4096)
        pool = [sid for sid, _ in ranked[:pool_n]]
        passages = []
        for sid in pool:
            meta = index.get(sid)
            text = _decode_snapshot_text(meta) if meta else None
            if not text:
                _log_rerank_fallthrough(f"passage decode failed for {sid}")
                return None
            # M14.2: body-only passage — strip the bookkeeping envelope, then
            # head-cut to passage_chars. The chunk ordinal (ord_by_id) and the
            # store are no longer consulted for passage building (M13: dropped
            # from this signature); the ordinal fetch is kept upstream in
            # retrieve() for provenance mode.
            body = extract_snapshot_content(text)
            passages.append(_best_passage_window(body, query, passage_chars)
                            if passage_mode == "window" else body[:passage_chars])
        scores = _rerank.score(query, passages)
        if scores is None or len(scores) != len(passages):
            _log_rerank_fallthrough("scorer returned no usable scores")
            return None
        order = sorted(range(pool_n), key=lambda i: scores[i], reverse=True)
        # rerank_floor: the reranker becomes a BOUNCER — sub-floor pool members
        # are dropped from the ranking entirely (they never reach delivery), so
        # a query with few genuinely-relevant snapshots returns fewer than k
        # instead of padding with the least-bad junk.
        kept = [i for i in order if scores[i] >= rerank_floor] if rerank_floor > 0 else order
        tail = [sid for sid, _ in ranked[pool_n:]]  # un-reranked remainder
        # Observability (2026-07-05): log a concise SUCCESS line so a live rerank
        # is visible in the journal — previously only preflight + failures logged,
        # so a healthy rerank ran completely silently. Never breaks retrieval.
        try:
            _s = _rerank.get_settings()
            _top = pool[order[0]]
            _moved = "top→#1 CHANGED" if order[0] != 0 else "top unchanged"
            _drop = f", dropped {pool_n - len(kept)} sub-floor" if rerank_floor > 0 else ""
            _q = (query[:80] + "…") if len(query) > 80 else query
            print(f"[RERANK] provider={_s.get('provider')} model={_s.get('model')}: "
                  f"query={_q!r} → scored {pool_n} passages, {_moved} ({_top} now #1){_drop}")
        except Exception:  # noqa: BLE001 - logging must never break retrieval
            pass
        # A rerank_floor also DROPS the un-reranked tail: those ids were never
        # scored by the cross-encoder, so they cannot "clear" a relevance floor
        # — keeping them would let MMR refill top-k with ungated candidates and
        # silently defeat the floor (measured: delivered stuck at k). With no
        # floor the tail is retained (below the pool) for byte-identical output.
        keep_tail = [] if rerank_floor > 0 else tail
        if rerank_relevance == "preserve":
            # The absolute 0–1 score IS the relevance; the retained tail (only
            # when no floor) sorts strictly below every kept pool member.
            rel = {pool[i]: float(scores[i]) for i in kept}
            for sid in keep_tail:
                rel[sid] = 0.0
            return rel
        # rankspace (default; byte-identical to the historical path when
        # rerank_floor == 0, since kept == order and keep_tail == tail).
        new_order = [pool[i] for i in kept] + keep_tail
        return {sid: 1.0 / (rrf_c + r) for r, sid in enumerate(new_order)}
    except Exception as e:  # noqa: BLE001 - rerank must never break retrieval
        _log_rerank_fallthrough(f"rerank stage error ({e})")
        return None


def retrieve(query: str, operator: str = "", k: int = 10, *, include_keyword: bool = True,
             store=None, query_vector=None, return_provenance: bool = False):
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

    return_provenance (keyword-only, M8/WI-7a): when True, results become
    [(snap_id, score, best_ordinal)] — best_ordinal is the winning chunk's
    ordinal within its snapshot group from the store's collapse (v2 store;
    0 = whole-doc vector won = "no specific window", >= 1 = a specific chunk
    won), or None for candidates with no chunk identity (v1-store rows, and
    keyword-only candidates that never entered the semantic channel). Ranking
    is IDENTICAL — the flag only annotates the same top-k. Default False is
    byte-identical to the historical 2-tuple contract (pinned by tests).
    Requires the store to support `search_with_vectors(..., with_ordinals=
    True)` (the live VectorStore does; bare eval fakes may not).
    """
    if not query or not query.strip():
        return []

    # 1. config knobs from [retrieval] (operator-locked defaults).
    candidate_n = CFG.getint("retrieval", "candidate_n", fallback=40)
    rrf_c = CFG.getint("retrieval", "rrf_c", fallback=60)
    recency_weight = CFG.getfloat("retrieval", "recency_weight", fallback=0.005)
    half_life = CFG.getfloat("retrieval", "recency_half_life_days", fallback=90.0)
    mmr_lambda = CFG.getfloat("retrieval", "mmr_lambda", fallback=0.85)
    mmr_protect = CFG.getint("retrieval", "mmr_protect_top", fallback=3)
    debug_log = CFG.getboolean("retrieval", "debug_log", fallback=False)
    # Precision levers (plan 2026-07-07). Defaults reproduce today's behavior
    # exactly; see _resolve_cos_floor/_resolve_rerank_floor above.
    keyword_mode = CFG.get("retrieval", "keyword_mode", fallback="fused").strip().lower()
    rerank_relevance = CFG.get("retrieval", "rerank_relevance", fallback="rankspace").strip().lower()
    rerank_floor = _resolve_rerank_floor()
    rerank_passage_mode = CFG.get("retrieval", "rerank_passage_mode", fallback="head").strip().lower()
    min_results = CFG.getint("retrieval", "min_results", fallback=0)
    # Rerank gate resolves sidecar > config (M8): a wizard/Portal selection
    # (POST /rerank/select) that flips `enabled` in rerank.json turns the rerank
    # stage on here with no config.ini edit or restart. is_enabled() never
    # raises (fail-open to config, default False). Lazy import mirrors the
    # flag-on-path import in _apply_rerank below (cheap sys.modules lookup).
    from Orchestrator import rerank as _rerank
    rerank_enabled = _rerank.is_enabled()

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

    # Junk floor resolves per-STORE (WI-3/M9): the store's registry noise
    # floor behind [retrieval] registry_floor_enabled (default false → the
    # global [retrieval] junk_floor). See _resolve_junk_floor.
    junk_floor = _resolve_junk_floor(store)
    cos_floor = _resolve_cos_floor(store)  # output floor + gated-keyword gate

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
    #    Provenance mode additionally asks the store for each winner's chunk
    #    ordinal (M8/WI-7a best-chunk identity); the default call shape stays
    #    the frozen 3-tuple contract so eval fakes/older stores are untouched.
    ord_by_id: dict[str, "int | None"] = {}
    if return_provenance or rerank_enabled:
        # The rerank stage also wants best-chunk identity (its passages window
        # on the matched chunk — audit A9), so ordinals are fetched whenever
        # either consumer needs them. Flag-off + no provenance keeps the frozen
        # 3-tuple call shape below, byte-identical to the historical path.
        try:
            sem4 = store.search_with_vectors(
                qv_np, candidate_n, allowed_ids, with_ordinals=True
            )
        except TypeError:
            if return_provenance:
                raise  # provenance mode's documented hard requirement
            # rerank-only on a store without ordinal support (v1-era fake /
            # eval stub): no chunk identity — every passage degrades to the
            # head window; ranking still proceeds.
            sem3 = store.search_with_vectors(qv_np, candidate_n, allowed_ids)
            sem4 = [(sid, cos, vec, None) for (sid, cos, vec) in sem3]
        sem = [(sid, cos, vec) for (sid, cos, vec, _o) in sem4 if cos >= junk_floor]
        ord_by_id = {sid: o for (sid, cos, _vec, o) in sem4 if cos >= junk_floor}
    else:
        sem = store.search_with_vectors(qv_np, candidate_n, allowed_ids)
        sem = [(sid, cos, vec) for (sid, cos, vec) in sem if cos >= junk_floor]
    sem_ids = [sid for sid, _cos, _vec in sem]
    vec_by_id = {sid: vec for sid, _cos, vec in sem}
    cos_by_id = {sid: cos for sid, cos, _vec in sem}  # for the output cosine floor

    # 5. keyword candidates (skipped for lean/semantic-only profiles).
    #    keyword_mode (precision fix) decides the lane's ROLE:
    #      * fused  (default) — legacy: raw lexical ids RRF-fused as a co-equal
    #        relevance channel (a lexical-only match can inject/promote).
    #      * dedup  — keyword only REINFORCES ids already in the semantic set
    #        (agreement boost); it can never inject a new candidate.
    #      * gated  — a keyword-only id may enter only if its TRUE semantic
    #        cosine clears cos_floor (keeps genuine recoveries — checkpoints /
    #        exact-id hits that also embed near the query — while dropping pure
    #        lexical noise).
    #      * off    — no keyword channel.
    kw_ids: list[str] = []
    if include_keyword and keyword_mode != "off":
        try:
            # The index-backed keyword path decodes snapshots from byte offsets on
            # demand and IGNORES vol_txt entirely; vol_txt is only consumed in the
            # no-index fallback. So pay the ~35MB full-volume read+decode (a ~250MB
            # transient spike) ONLY when there is no index — otherwise pass "".
            vol_txt = read_text_safe(VOL_PATH) if not index else ""
            raw_kw = keyword_retrieve_ids_for_operator(
                vol_txt, query, candidate_n, operator or ""
            )
        except Exception as e:  # noqa: BLE001 - keyword channel is best-effort
            print(f"[RETRIEVAL] keyword channel unavailable ({e}); semantic-only")
            raw_kw = []
        if keyword_mode == "fused":
            kw_ids = raw_kw
        elif keyword_mode == "dedup":
            sem_set = set(sem_ids)
            kw_ids = [s for s in raw_kw if s in sem_set]
        elif keyword_mode == "gated":
            sem_set = set(sem_ids)
            kw_only = [s for s in raw_kw if s not in sem_set]
            gate_cos = (store.max_cosine_for(qv_np, kw_only)
                        if (kw_only and cos_floor > 0) else {})
            cos_by_id.update(gate_cos)  # so the output floor sees them too
            kw_ids = [s for s in raw_kw
                      if s in sem_set or gate_cos.get(s, 0.0) >= cos_floor]
        else:
            kw_ids = raw_kw  # unknown mode -> safe legacy behavior

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

    # 7.5 OPTIONAL cross-encoder rerank (M11/WI-4, audit A9 placement): rerank
    #     the post-recency top-N pool, map the reranked ORDER back into
    #     rank-space relevance 1/(rrf_c + new_rank), RE-APPLY the recency
    #     tie-break on that scale, rebuild score_by_id — then MMR (with the
    #     channel-conditional protect) runs unchanged on the new ordering.
    #     Gated on the flag AND provider availability AND the one-time latency
    #     preflight (all inside _apply_rerank); any failure falls through
    #     silently (logged once) to the un-reranked ranking above.
    if rerank_enabled:
        reranked_rel = _apply_rerank(query, ranked, index, rrf_c,
                                     rerank_relevance, rerank_floor,
                                     rerank_passage_mode)
        if reranked_rel is not None:
            relevance = reranked_rel
            ranked = apply_recency_tiebreak(
                relevance, ages, recency_weight, half_life
            )
            score_by_id = dict(ranked)

    # 8. MMR diversity over the top window, then take top-k. The fused top-P
    #    ([retrieval] mmr_protect_top, default 3) is protected from MMR
    #    elimination — seeded into the picked set in rank order, counting
    #    toward k (see mmr_select). P=0 disables (pure historical MMR).
    #    CHANNEL-CONDITIONAL (M6f iteration 4): the protect is justified by
    #    cross-channel agreement; a single-channel ranking has no agreement
    #    signal, and measured data shows single-channel MMR must keep full
    #    elimination freedom (eval/results/2026-07-03-protect-gate.md —
    #    semantic-only holdout 3/3 at P=0, 2/3 at every P>0). Semantic-only
    #    calls (lean profile include_keyword=False, or an empty keyword
    #    channel) therefore run unprotected.
    #    Keyword-only ids (absent from the semantic channel) get a zero vector of
    #    the query's dim so they never register as near-duplicates of anything.
    # Output relevance floor (precision fix): drop delivered candidates whose
    # semantic cosine is below cos_floor — the RERANKER-ABSENT relevance gate
    # (and a backstop with rerank on). Keyword-only ids that never earned a
    # cosine (fused/dedup modes) default to 0.0 and are dropped by a positive
    # floor. This is what lets a thin query return FEWER than k instead of
    # padding. min_results guards against over-pruning a valid query to empty:
    # if the floor leaves fewer than min_results, the top pre-floor candidates
    # are restored so a real query is never starved. cos_floor == 0 (default)
    # is byte-identical to the historical path.
    if cos_floor > 0:
        pre_floor = ranked
        kept = [(sid, sc) for sid, sc in ranked if cos_by_id.get(sid, 0.0) >= cos_floor]
        if len(kept) < min_results:
            have = {s for s, _ in kept}
            for sid, sc in pre_floor:
                if sid not in have:
                    kept.append((sid, sc))
                    have.add(sid)
                    if len(kept) >= min_results:
                        break
            kept.sort(key=lambda kv: kv[1], reverse=True)
        ranked = kept

    window = max(k * 2, 20)
    zero = np.zeros(qdim, dtype=np.float32)
    mmr_cands = [
        (sid, score_by_id[sid], vec_by_id.get(sid, zero))
        for sid, _score in ranked[:window]
    ]
    effective_protect = mmr_protect if len(rankings) > 1 else 0
    picked = mmr_select(mmr_cands, k, mmr_lambda, protect=effective_protect)
    if return_provenance:
        # Same top-k, annotated with best-chunk identity: ordinal from the
        # semantic channel's collapse; None for keyword-only candidates
        # (never scored against chunks) — delivery treats None like 0
        # ("no specific window" -> head truncation).
        results = [(sid, score_by_id[sid], ord_by_id.get(sid)) for sid in picked]
    else:
        results = [(sid, score_by_id[sid]) for sid in picked]

    # 9. OPT-IN provenance logging — answers "why did I get these results" from
    #    logs alone. Cheap: only computed when [retrieval] debug_log = true, and
    #    only over the final top-k. Does NOT touch ranking. The `rrf` field is the
    #    pre-recency RRF relevance; `recency_boost` is the additive tie-break term;
    #    `channels` shows which of semantic/keyword surfaced the id.
    if debug_log:
        sem_set = set(sem_ids)
        kw_set = set(kw_ids)
        for sid in picked:  # shape-agnostic (results may carry provenance)
            final = score_by_id[sid]
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
