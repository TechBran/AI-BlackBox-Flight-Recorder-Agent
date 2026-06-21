"""Canonical retriever — pure-function unit tests + one live integration test.

Phase 3a of the memory-retrieval hardening. These tests pin the *algorithm*
(RRF fusion, mild recency tie-break, MMR diversity) deterministically with no
network. The single integration test exercises retrieve() against the LIVE
active store and is skipped when the store/provider is unavailable.

Design (operator-locked):
- recency is a MILD additive tie-break (default weight 0.05) — semantic
  relevance dominates; recency flips only near-ties.
- fusion is Reciprocal Rank Fusion (scale-free), no rerank stage.
- MMR breaks near-duplicate session clusters.
"""
import numpy as np
import pytest

from Orchestrator.retrieval import (
    apply_recency_tiebreak,
    mmr_select,
    retrieve,
    rrf_fuse,
)


# ── RRF fusion ────────────────────────────────────────────────────────────────

def test_rrf_rewards_agreement():
    fused = rrf_fuse({"kw": ["A", "B"], "sem": ["B", "C"]}, c=60)
    assert fused[0][0] == "B"                      # high in both channels -> top
    assert {sid for sid, _ in fused} == {"A", "B", "C"}


def test_rrf_single_channel_preserves_order():
    fused = rrf_fuse({"sem": ["X", "Y", "Z"]}, c=60)
    assert [sid for sid, _ in fused] == ["X", "Y", "Z"]


# ── recency = mild tie-break ──────────────────────────────────────────────────

def test_recency_is_only_a_tiebreak():
    # far-better old item keeps #1; among near-ties the newer wins
    rel = {"old_strong": 1.00, "old_near": 0.50, "new_near": 0.49}
    ages = {"old_strong": 400, "old_near": 400, "new_near": 5}
    ranked = [sid for sid, _ in apply_recency_tiebreak(rel, ages, weight=0.05, half_life_days=90)]
    assert ranked[0] == "old_strong"               # relevance dominates
    assert ranked.index("new_near") < ranked.index("old_near")  # near-tie flips to recent


def test_recency_weight_cannot_overturn_clear_winner():
    rel = {"best": 0.90, "newish": 0.70}
    ages = {"best": 1000, "newish": 1}
    ranked = [sid for sid, _ in apply_recency_tiebreak(rel, ages, weight=0.05, half_life_days=90)]
    assert ranked[0] == "best"                      # 0.05 boost can't beat a 0.20 gap


def test_recency_missing_age_treated_as_ancient():
    # An id with no age entry gets the ~3650d (ancient) default -> ~zero boost.
    rel = {"a": 0.50, "b": 0.50}
    ages = {"a": 0}  # b missing
    ranked = [sid for sid, _ in apply_recency_tiebreak(rel, ages, weight=0.05, half_life_days=90)]
    assert ranked[0] == "a"  # fresh a beats age-defaulted b


# ── MMR diversity ─────────────────────────────────────────────────────────────

def test_mmr_drops_near_duplicates():
    v = lambda x: np.array(x, dtype="float32")
    cands = [("A", 1.0, v([1, 0])), ("A2", 0.98, v([0.99, 0.01])), ("B", 0.9, v([0, 1]))]
    assert mmr_select(cands, k=2, lam=0.7) == ["A", "B"]


def test_mmr_respects_k():
    v = lambda x: np.array(x, dtype="float32")
    cands = [("A", 1.0, v([1, 0])), ("B", 0.9, v([0, 1])), ("C", 0.8, v([0.7, 0.7]))]
    assert mmr_select(cands, k=1, lam=0.7) == ["A"]
    assert len(mmr_select(cands, k=5, lam=0.7)) == 3  # k > pool -> whole pool


# ── live integration (skipped when store/provider unavailable) ────────────────

def test_retrieve_live_returns_recent_snapshots():
    try:
        from Orchestrator.embeddings.search import get_active_store
        store = get_active_store()
        if store.count == 0:
            pytest.skip("active store empty")
    except Exception as e:  # noqa: BLE001 - provider/store unavailable in test env
        pytest.skip(f"active store/provider unavailable: {e}")

    results = retrieve("embeddings model switch reembed", "system", k=10)
    if not results:
        pytest.skip("retrieve returned nothing (query embed unavailable)")

    assert len(results) <= 10
    assert all(isinstance(sid, str) and isinstance(score, float) for sid, score in results)
    # The recency tie-break should surface recent (June 2026) work near the top.
    top_ids = [sid for sid, _ in results]
    assert any(sid.startswith("SNAP-202606") for sid in top_ids), top_ids
