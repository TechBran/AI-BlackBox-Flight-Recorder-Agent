"""retrieve() cross-encoder rerank insertion (M11/WI-4 — audit A9 placement).

All fixtures/fakes (no GPU, no provider, no volume): these tests pin
1. flag-off byte-identical ranking (the audit's acceptance) — rerank code is
   never consulted and the store call keeps the frozen 3-tuple shape;
2. the corrected insertion math: post-recency pool -> reranked ORDER ->
   rank-space remap 1/(rrf_c + new_rank) -> recency RE-APPLIED -> score_by_id
   rebuilt -> MMR on the new ordering. Scales stay coherent (rank-space
   relevance span vs the <=0.005 recency boost — the audit's numbers);
3. keyword-only candidates are scored on the same scale as chunk-provenance
   candidates (the fake sees N passages == pool size);
4. reranker returning None (or a preflight failure) falls through to a result
   identical to flag-off, with a once-per-process log.

World: 3 semantic candidates (A > B > C by cosine, all above the junk floor)
+ 1 keyword-only candidate KW1; keyword channel ranks [KW1, A]. With rrf_c=60:
  relevance: A = 1/60 + 1/61, KW1 = 1/60, B = 1/61, C = 1/62
Ages are pinned via a patched _age_days (timestamps are sentinels), so every
score is exactly reproducible float arithmetic.
"""
import contextlib
import math

import pytest

import Orchestrator.rerank as rerank_mod
import Orchestrator.retrieval as retrieval
from Orchestrator.config import CFG
from Orchestrator.tests.test_retrieval_store_override import FakeStore

W = 0.005          # pinned recency weight
RRF_C = 60

IDX = {
    "A":   {"operator": "alice", "timestamp": "T0", "text": "text of snapshot A"},
    "B":   {"operator": "alice", "timestamp": "T0", "text": "text of snapshot B"},
    "C":   {"operator": "alice", "timestamp": "T0", "text": "text of snapshot C"},
    "KW1": {"operator": "alice", "timestamp": "T0", "text": "keyword-only text"},
}

# timestamp sentinel -> age in days ("T0" = brand new; "OLD" = ancient enough
# that 2^(-age/90) is ~0 at float precision granularity we assert on).
AGE_BY_TS = {"T0": 0.0, "OLD": 3650.0}

# Near-orthogonal rows (unit-ish; FakeStore re-normalizes): cosine order
# A > B > C against the [1,0,0,0] query, all above the 0.40 junk floor.
ROWS = [
    ("A", [0.70, 0.714, 0.0, 0.0]),
    ("B", [0.65, 0.0, 0.76, 0.0]),
    ("C", [0.60, 0.0, 0.0, 0.80]),
]


class FakeOrdinalStore(FakeStore):
    """FakeStore + the M8 with_ordinals contract + call-shape recording."""

    def __init__(self, rows, dims=4, slug=None, ordinals=None):
        super().__init__(rows, dims=dims, slug=slug)
        self._ordinals = ordinals or {}
        self.calls = []  # recorded with_ordinals kwarg per search call

    def search_with_vectors(self, query_vec, k, allowed_ids=None,
                            with_ordinals=False):
        self.calls.append(with_ordinals)
        base = super().search_with_vectors(query_vec, k, allowed_ids)
        if not with_ordinals:
            return base
        return [(sid, cos, vec, self._ordinals.get(sid))
                for sid, cos, vec in base]


class LegacyStore(FakeStore):
    """Pre-M8 store: no with_ordinals kwarg at all (eval-stub shape)."""


@contextlib.contextmanager
def pin_cfg(section, **keys):
    if not CFG.has_section(section):
        CFG.add_section(section)
    saved = {
        opt: (CFG.get(section, opt) if CFG.has_option(section, opt) else None)
        for opt in keys
    }
    try:
        for opt, val in keys.items():
            if val is None:
                CFG.remove_option(section, opt)
            else:
                CFG.set(section, opt, str(val))
        yield
    finally:
        for opt, prev in saved.items():
            if prev is None:
                CFG.remove_option(section, opt)
            else:
                CFG.set(section, opt, prev)


def pin_retrieval(**extra):
    """The full [retrieval] knob set these tests' arithmetic assumes."""
    base = dict(candidate_n="40", rrf_c=str(RRF_C), recency_weight=str(W),
                recency_half_life_days="90", mmr_lambda="0.85",
                mmr_protect_top="3", junk_floor="0.40",
                registry_floor_enabled="false", debug_log="false")
    base.update(extra)
    return pin_cfg("retrieval", **base)


@pytest.fixture()
def world(monkeypatch, tmp_path):
    """Hermetic retrieve() world: fixed query vector, fixed index, keyword
    channel [KW1, A], passage decode from the index fixture, pinned ages,
    reset once-per-process rerank state.

    Isolates the rerank.json sidecar dir (M8): the retrieve() gate now reads
    rerank.is_enabled() (sidecar > config), so an EMPTY tmp stores dir makes
    _load_sidecar() return None and these tests' pinned [retrieval]
    rerank_enabled config gate the enable decision, as they assume."""
    from Orchestrator import config as _config
    monkeypatch.setattr(_config, "EMBEDDINGS_STORES_DIR", str(tmp_path / "stores"))
    monkeypatch.setattr(
        retrieval._emb, "generate_embedding_sync",
        lambda text, purpose="query": [1.0, 0.0, 0.0, 0.0])
    monkeypatch.setattr(retrieval, "load_snapshot_index", lambda: dict(IDX))
    monkeypatch.setattr(
        retrieval, "keyword_retrieve_ids_for_operator",
        lambda vol, q, n, op: ["KW1", "A"])
    monkeypatch.setattr(
        retrieval, "_decode_snapshot_text", lambda meta: meta.get("text"))
    monkeypatch.setattr(
        retrieval, "_age_days", lambda ts, now: AGE_BY_TS.get(ts, 3650.0))
    monkeypatch.setattr(retrieval, "_rerank_fallthrough_logged", False)
    rerank_mod.reset_preflight()
    yield
    rerank_mod.reset_preflight()


def make_store(ordinals=None):
    return FakeOrdinalStore(ROWS, slug="test-slug", ordinals=ordinals)


def _boom(*a, **k):
    raise AssertionError("rerank consulted on a path that must never touch it")


# The exact flag-off expectation, computed with the pipeline's own arithmetic
# (accumulation order matters for float-exactness: semantic channel first).
def expected_flag_off():
    rel = {"A": 1.0 / 60 + 1.0 / 61, "KW1": 1.0 / 60,
           "B": 1.0 / 61, "C": 1.0 / 62}
    boost = W * math.pow(2.0, -0.0 / 90.0)  # all ages pinned 0 -> +W each
    # ranked: A, KW1, B, C; protect=3 (two channels) seeds top-3, C fills.
    return [(sid, rel[sid] + boost) for sid in ("A", "KW1", "B", "C")]


# ── 1. flag off = byte-identical, rerank never consulted ─────────────────────

def test_flag_off_is_byte_identical_and_never_touches_rerank(world, monkeypatch):
    monkeypatch.setattr(rerank_mod, "available", _boom)
    monkeypatch.setattr(rerank_mod, "score", _boom)
    store = make_store()
    with pin_retrieval(rerank_enabled=None, rerank_candidate_n=None):
        results = retrieval.retrieve("q", "system", k=10, store=store)
    assert results == expected_flag_off()          # exact floats, exact order
    assert store.calls == [False]                  # frozen 3-tuple call shape


def test_flag_explicitly_false_identical_to_absent(world, monkeypatch):
    monkeypatch.setattr(rerank_mod, "available", _boom)
    with pin_retrieval(rerank_enabled="false"):
        results = retrieval.retrieve("q", "system", k=10, store=make_store())
    assert results == expected_flag_off()


def test_flag_on_but_provider_unavailable_is_identical_to_flag_off(
        world, monkeypatch):
    """rerank_enabled=true on this GPU-less box (null provider): available()
    is False and the ranking is exactly the flag-off ranking."""
    monkeypatch.setattr(rerank_mod, "available", lambda: False)
    monkeypatch.setattr(rerank_mod, "score", _boom)
    store = make_store()
    with pin_retrieval(rerank_enabled="true"):
        results = retrieval.retrieve("q", "system", k=10, store=store)
    assert results == expected_flag_off()
    assert store.calls == [True]  # ordinal fetch is the flag-on internal shape


# ── 2. insertion math: remap + recency re-application + MMR on new order ─────

def test_reranked_order_remaps_to_rank_space_and_reapplies_recency(
        world, monkeypatch):
    """Fake reranker REVERSES the pool: pool = post-recency top-3 [A, KW1, B]
    -> reranked [B, KW1, A]; tail [C] keeps its position after the pool.
    New rank-space relevance: B=1/60, KW1=1/61, A=1/62, C=1/63; recency
    re-applied (+W each, ages pinned 0); MMR runs on the NEW ordering."""
    monkeypatch.setattr(rerank_mod, "available", lambda: True)
    # ascending scores by passage position -> stable sort reverses the pool.
    monkeypatch.setattr(
        rerank_mod, "score", lambda q, ps: [float(i) for i in range(len(ps))])
    with pin_retrieval(rerank_enabled="true", rerank_candidate_n="3"):
        results = retrieval.retrieve("q", "system", k=10, store=make_store())

    boost = W * math.pow(2.0, -0.0 / 90.0)
    expected = [("B", 1.0 / 60 + boost), ("KW1", 1.0 / 61 + boost),
                ("A", 1.0 / 62 + boost), ("C", 1.0 / 63 + boost)]
    assert results == expected

    # Scale coherence (audit A9 numbers): remapped relevance lives in the
    # rank-space band (1/(rrf_c+n-1) .. 1/rrf_c] — span ~0.0066 at n=40,
    # here n=4 — and the recency term stays bounded by the 0.005 weight, so
    # relevance still dominates and recency stays a tie-break.
    for rank, (sid, score) in enumerate(results):
        rel = 1.0 / (RRF_C + rank)
        assert score == rel + boost
        assert 1.0 / (RRF_C + len(results) - 1) <= rel <= 1.0 / RRF_C
    assert 0.0 < boost <= W


def test_recency_reapplication_flips_near_ties_on_the_new_ordering(
        world, monkeypatch):
    """After the remap, adjacent rank-space gaps (~0.00027) sit BELOW the
    recency weight — a fresh candidate the reranker placed lower must win its
    near-tie against an ancient one, proving apply_recency_tiebreak really
    re-ran on the reranked ordering (not the stale pre-rerank scores)."""
    idx = {sid: dict(meta) for sid, meta in IDX.items()}
    for sid in ("B", "C", "KW1"):
        idx[sid]["timestamp"] = "OLD"          # A stays "T0" (fresh)
    monkeypatch.setattr(retrieval, "load_snapshot_index", lambda: idx)
    monkeypatch.setattr(rerank_mod, "available", lambda: True)
    monkeypatch.setattr(
        rerank_mod, "score", lambda q, ps: [float(i) for i in range((len(ps)))])
    with pin_retrieval(rerank_enabled="true", rerank_candidate_n="3"):
        results = retrieval.retrieve("q", "system", k=10, store=make_store())
    # Reranked order was [B, KW1, A, C]; fresh A (+0.005) overtakes the
    # ancient B (1/60) and KW1 (1/61): 1/62 + 0.005 > 1/60.
    assert [sid for sid, _ in results] == ["A", "B", "KW1", "C"]


# ── 3. every pool member scored on ONE scale (keyword-only included) ─────────

def test_keyword_only_candidates_get_passages_and_scores(world, monkeypatch):
    """Pool = all 4 fused candidates (rerank_candidate_n=40 > pool). The fake
    scorer must see EXACTLY 4 passages — the keyword-only KW1 (never in the
    semantic channel) is decoded and scored on the same scale (audit A9:
    otherwise the keyword channel's exact-string wins are annihilated)."""
    seen = {}
    monkeypatch.setattr(rerank_mod, "available", lambda: True)

    def fake_score(q, ps):
        seen["query"], seen["passages"] = q, list(ps)
        return [0.0] * len(ps)     # all ties -> stable sort keeps the order

    monkeypatch.setattr(rerank_mod, "score", fake_score)
    windowed = []
    monkeypatch.setattr(
        retrieval, "window_snapshot_text",
        lambda text, ordinal, budget, model_key: (
            windowed.append((text, ordinal, budget, model_key)) or text))

    store = make_store(ordinals={"A": 2, "B": 0})
    with pin_retrieval(rerank_enabled="true", rerank_candidate_n="40"):
        with pin_cfg("rerank", passage_chars="1234"):
            results = retrieval.retrieve("the query", "system", k=10, store=store)

    assert seen["query"] == "the query"
    assert len(seen["passages"]) == 4              # every pool member scored
    assert seen["passages"] == [IDX[sid]["text"]
                                for sid in ("A", "KW1", "B", "C")]
    # Passage wiring: chunk provenance flows into the M8 window helper —
    # A's winning chunk ordinal 2; B's whole-doc win 0; KW1 (keyword-only,
    # no semantic identity) None -> head window. Budget + the STORE's slug
    # (the ordinals came from that store's chunker config) pass through.
    assert [(o, b, mk) for _t, o, b, mk in windowed] == [
        (2, 1234, "test-slug"), (None, 1234, "test-slug"),
        (0, 1234, "test-slug"), (None, 1234, "test-slug")]
    # all-tie scores: the stable sort preserves the incoming post-recency
    # ORDER (scores become rank-space values, so compare ids, not floats).
    assert [sid for sid, _ in results] == [
        sid for sid, _ in expected_flag_off()]


def test_legacy_store_without_ordinal_support_still_reranks(world, monkeypatch):
    """A pre-M8 store (no with_ordinals kwarg) on the flag-on path: the
    TypeError fallback drops chunk identity (head-window passages) but the
    rerank still runs and remaps."""
    monkeypatch.setattr(rerank_mod, "available", lambda: True)
    monkeypatch.setattr(
        rerank_mod, "score", lambda q, ps: [float(i) for i in range(len(ps))])
    with pin_retrieval(rerank_enabled="true", rerank_candidate_n="3"):
        results = retrieval.retrieve(
            "q", "system", k=10, store=LegacyStore(ROWS, slug="test-slug"))
    assert [sid for sid, _ in results] == ["B", "KW1", "A", "C"]


# ── 4. failure fall-throughs: identical to flag-off, logged once ─────────────

def test_scorer_none_falls_through_identical_to_flag_off(world, monkeypatch,
                                                         capsys):
    monkeypatch.setattr(rerank_mod, "available", lambda: True)
    monkeypatch.setattr(rerank_mod, "score", lambda q, ps: None)
    with pin_retrieval(rerank_enabled="true"):
        first = retrieval.retrieve("q", "system", k=10, store=make_store())
        second = retrieval.retrieve("q", "system", k=10, store=make_store())
    assert first == expected_flag_off()
    assert second == expected_flag_off()
    out = capsys.readouterr().out
    assert out.count("[RERANK]") == 1              # logged ONCE per process
    assert "falling through" in out


def test_passage_decode_failure_falls_through(world, monkeypatch, capsys):
    monkeypatch.setattr(rerank_mod, "available", lambda: True)
    monkeypatch.setattr(rerank_mod, "score", _boom)  # must never be reached
    monkeypatch.setattr(retrieval, "_decode_snapshot_text", lambda meta: None)
    with pin_retrieval(rerank_enabled="true"):
        results = retrieval.retrieve("q", "system", k=10, store=make_store())
    assert results == expected_flag_off()
    assert "passage decode failed" in capsys.readouterr().out


def test_preflight_failure_disables_rerank_and_probes_exactly_once(
        world, monkeypatch):
    """End-to-end gating through the REAL available()/preflight(): a
    configured provider whose probe misses the ceiling disables rerank for
    the process — ranking is flag-off-identical and the probe runs exactly
    once across retrieves."""
    probes = {"n": 0}

    def counting_score(q, ps):
        probes["n"] += 1
        return [1.0] * len(ps)

    monkeypatch.setattr(rerank_mod, "score", counting_score)
    with pin_cfg("rerank", provider="vllm", base_url="http://h:1",
                 preflight_ceiling_ms="-1"):   # any latency is over-ceiling
        with pin_retrieval(rerank_enabled="true"):
            first = retrieval.retrieve("q", "system", k=10, store=make_store())
            second = retrieval.retrieve("q", "system", k=10, store=make_store())
    assert first == expected_flag_off()
    assert second == expected_flag_off()
    assert probes["n"] == 1                        # one probe, then cached


# ── provenance mode coexists with rerank ──────────────────────────────────────

def test_provenance_mode_annotates_reranked_topk(world, monkeypatch):
    """return_provenance=True + rerank on: same reranked top-k, annotated
    with the SEMANTIC channel's chunk ordinals (KW1 stays None)."""
    monkeypatch.setattr(rerank_mod, "available", lambda: True)
    monkeypatch.setattr(
        rerank_mod, "score", lambda q, ps: [float(i) for i in range(len(ps))])
    store = make_store(ordinals={"A": 2, "B": 1, "C": 0})
    with pin_retrieval(rerank_enabled="true", rerank_candidate_n="3"):
        results = retrieval.retrieve("q", "system", k=10, store=store,
                                     return_provenance=True)
    assert [(sid, o) for sid, _s, o in results] == [
        ("B", 1), ("KW1", None), ("A", 2), ("C", 0)]
