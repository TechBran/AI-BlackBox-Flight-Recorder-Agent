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
import re

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


# ── MMR top-rank protect (M6f iteration 3) ────────────────────────────────────
#
# The fused top-P must be IMMUNE to MMR elimination: two human-verified golds
# at post-RRF rank 3 were MMR-dropped as near-duplicates of a first-picked
# same-domain sibling at every lambda < 1.0 (eval/results/2026-07-03-wholevec-gate.md).

def _v(*xs):
    return np.array(xs, dtype="float32")


# Rank order (rel descending): S1, S2, GOLD (near-identical to S1), S4 (diverse).
PROTECT_CANDS = [
    ("S1", 1.00, _v(1.0, 0.0, 0.0)),
    ("S2", 0.98, _v(0.0, 1.0, 0.0)),
    ("GOLD", 0.96, _v(0.9995, 0.0316, 0.0)),   # cos to S1 ≈ 0.9995
    ("S4", 0.70, _v(0.0, 0.0, 1.0)),
]


def test_mmr_protect_keeps_rank3_near_duplicate_that_unprotected_mmr_drops():
    # Unprotected (protect=0 == historical behavior): the rank-3 near-dup of the
    # first pick loses to the diverse S4 at lambda=0.7 — exactly the gate failure.
    assert mmr_select(PROTECT_CANDS, k=3, lam=0.7) == ["S1", "S2", "S4"]
    assert mmr_select(PROTECT_CANDS, k=3, lam=0.7, protect=0) == ["S1", "S2", "S4"]
    # Protected: the fused top-3 survive verbatim; MMR has no remaining slots.
    assert mmr_select(PROTECT_CANDS, k=3, lam=0.7, protect=3) == ["S1", "S2", "GOLD"]


def test_mmr_protected_items_lead_in_rank_order_then_mmr_picks_follow():
    cands = [
        ("S1", 1.00, _v(1.0, 0.0, 0.0)),
        ("S2", 0.98, _v(0.9995, 0.0316, 0.0)),  # near-dup of S1
        ("S3", 0.50, _v(0.0, 0.0, 1.0)),        # diverse
    ]
    # Unprotected at lambda=0.6 MMR reorders: S3 beats the near-dup S2.
    assert mmr_select(cands, k=3, lam=0.6) == ["S1", "S3", "S2"]
    # protect=2 seeds the top-2 IN RANK ORDER ahead of the greedy picks.
    assert mmr_select(cands, k=3, lam=0.6, protect=2) == ["S1", "S2", "S3"]


def test_mmr_protect_at_or_above_k_degrades_to_pure_fused_ranking():
    # protect >= k: the top-k of the fused ranking verbatim — MMR is moot.
    assert mmr_select(PROTECT_CANDS, k=3, lam=0.7, protect=3) == ["S1", "S2", "GOLD"]
    assert mmr_select(PROTECT_CANDS, k=3, lam=0.7, protect=99) == ["S1", "S2", "GOLD"]
    # protect > pool size with room in k: whole pool in rank order, no crash.
    assert mmr_select(PROTECT_CANDS, k=10, lam=0.7, protect=99) == [
        "S1", "S2", "GOLD", "S4"]


def test_mmr_protect_negative_clamps_to_zero():
    assert (mmr_select(PROTECT_CANDS, k=3, lam=0.7, protect=-1)
            == mmr_select(PROTECT_CANDS, k=3, lam=0.7, protect=0))


# ── retrieve() wiring of the protect knob (hermetic) ──────────────────────────

from Orchestrator.tests.test_retrieval_store_override import FakeStore  # noqa: E402

# Equal timestamps: recency boosts are identical, so fused rank order is the
# semantic cosine order — the protect scenario is controlled by vectors alone.
_PROTECT_INDEX = {
    sid: {"operator": "alice", "timestamp": "2026-06-01T00:00:00Z"}
    for sid in ("S1", "S2", "GOLD", "S4")
}

# Cosine-to-query ranks: S1 0.70, S2 0.65, GOLD 0.64 (near-dup of S1, cos≈0.996),
# S4 0.50 (diverse). All above the 0.40 junk floor. At lambda=0.7/k=3 unprotected
# MMR picks the diverse S4 over the rank-3 GOLD; the protect must keep GOLD.
_PROTECT_STORE_ROWS = [
    ("S1", [0.70, 0.714, 0.0, 0.0]),
    ("S2", [0.65, 0.0, 0.76, 0.0]),
    ("GOLD", [0.64, 0.768, 0.0, 0.0]),
    ("S4", [0.50, 0.0, 0.0, 0.866]),
]


@pytest.fixture()
def hermetic_protect(monkeypatch):
    import Orchestrator.retrieval as retrieval_mod
    monkeypatch.setattr(
        retrieval_mod._emb, "generate_embedding_sync",
        lambda text, purpose="query": [1.0, 0.0, 0.0, 0.0],
    )
    monkeypatch.setattr(
        retrieval_mod, "load_snapshot_index", lambda: dict(_PROTECT_INDEX))
    monkeypatch.setattr(
        retrieval_mod, "keyword_retrieve_ids_for_operator",
        lambda vol, q, n, op: [])
    return FakeStore(_PROTECT_STORE_ROWS)


def _with_retrieval_keys(**keys):
    """Context manager: pin [retrieval] keys on the in-process CFG (value None
    = ensure the key is ABSENT); restore exactly afterwards. Disk untouched."""
    import contextlib
    from Orchestrator.config import CFG

    @contextlib.contextmanager
    def _cm():
        if not CFG.has_section("retrieval"):
            CFG.add_section("retrieval")
        saved = {
            opt: (CFG.get("retrieval", opt)
                  if CFG.has_option("retrieval", opt) else None)
            for opt in keys
        }
        try:
            for opt, val in keys.items():
                if val is None:
                    CFG.remove_option("retrieval", opt)
                else:
                    CFG.set("retrieval", opt, str(val))
            yield
        finally:
            for opt, prev in saved.items():
                if prev is None:
                    CFG.remove_option("retrieval", opt)
                else:
                    CFG.set("retrieval", opt, prev)
    return _cm()


def test_retrieve_default_protect_is_3_without_config_key(hermetic_protect):
    """No [retrieval] mmr_protect_top key -> code fallback 3: the rank-3
    near-duplicate gold SURVIVES on the default path."""
    with _with_retrieval_keys(mmr_protect_top=None, mmr_lambda="0.7"):
        results = retrieve("q", "system", k=3, store=hermetic_protect)
    assert [sid for sid, _ in results] == ["S1", "S2", "GOLD"]


def test_retrieve_protect_zero_restores_pure_mmr_behavior(hermetic_protect):
    """mmr_protect_top=0 disables the protect: exactly the historical pipeline
    (the near-dup GOLD is MMR-dropped for the diverse S4)."""
    with _with_retrieval_keys(mmr_protect_top="0", mmr_lambda="0.7"):
        results = retrieve("q", "system", k=3, store=hermetic_protect)
    assert [sid for sid, _ in results] == ["S1", "S4", "S2"]


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


# ── opt-in provenance logging (Phase 5.1) ─────────────────────────────────────

def test_provenance_log_emitted_only_when_flag_enabled(capsys):
    """[retrieval] debug_log gates a structured [RETRIEVAL] line per result.

    Off by default (no log spam on the hot path); on => one line per final
    result with the documented field shape. Observability only — ranking is
    unchanged regardless of the flag. Skips when the live store/provider is
    unavailable in the test env.
    """
    from Orchestrator.config import CFG
    try:
        from Orchestrator.embeddings.search import get_active_store
        if get_active_store().count == 0:
            pytest.skip("active store empty")
    except Exception as e:  # noqa: BLE001 - provider/store unavailable
        pytest.skip(f"active store/provider unavailable: {e}")

    if not CFG.has_section("retrieval"):
        CFG.add_section("retrieval")
    had_key = CFG.has_option("retrieval", "debug_log")
    prev = CFG.get("retrieval", "debug_log") if had_key else None

    # 1. flag OFF -> no [RETRIEVAL] provenance lines.
    try:
        CFG.set("retrieval", "debug_log", "false")
        results_off = retrieve("embeddings model switch reembed", "system", k=5)
        if not results_off:
            pytest.skip("retrieve returned nothing (query embed unavailable)")
        off_out = capsys.readouterr().out
        assert "-> sid=" not in off_out, off_out

        # 2. flag ON -> one provenance line per result, matching the shape.
        CFG.set("retrieval", "debug_log", "true")
        results_on = retrieve("embeddings model switch reembed", "system", k=5)
        on_out = capsys.readouterr().out
    finally:
        if had_key:
            CFG.set("retrieval", "debug_log", prev)
        else:
            CFG.remove_option("retrieval", "debug_log")

    # ranking unchanged by the flag (observability must not perturb results).
    assert [s for s, _ in results_on] == [s for s, _ in results_off]

    prov_lines = [ln for ln in on_out.splitlines() if ln.startswith("[RETRIEVAL] q=")]
    assert len(prov_lines) == len(results_on), on_out

    # exact field shape of one line: q=... -> sid=... rrf=.. age_days=.. recency_boost=.. final=.. channels=..
    shape = re.compile(
        r"^\[RETRIEVAL\] q=.{1,42} -> sid=(SNAP-\S+) "
        r"rrf=-?\d+\.\d+ age_days=\d+\.\d+ recency_boost=-?\d+\.\d+ "
        r"final=-?\d+\.\d+ channels=(semantic|keyword|semantic\+keyword|none)$"
    )
    for ln in prov_lines:
        assert shape.match(ln), f"bad provenance line shape: {ln!r}"

    # the logged sids are exactly the returned ones, in order.
    logged_sids = [shape.match(ln).group(1) for ln in prov_lines]
    assert logged_sids == [s for s, _ in results_on]
