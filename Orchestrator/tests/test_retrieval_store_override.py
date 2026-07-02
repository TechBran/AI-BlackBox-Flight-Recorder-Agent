"""Store-override eval seam for the canonical retriever (WI-6 Phase A, M4).

`retrieve(query, store=<candidate>)` must score against the OVERRIDE store
through the FULL ranking pipeline (junk floor -> RRF -> recency tie-break ->
MMR) and must NEVER consult the active-store plumbing (`get_active_store`,
which reads Manifest/embeddings/active.json). This is the offline eval seam
that makes "swap only after the golden/bench gates pass" executable: candidate
chunk stores (M6) get benched pre-swap without touching the live pointer.

Production surfaces never pass `store=` — the default path must stay exactly
`get_active_store()`, which these tests also pin.
"""
import numpy as np
import pytest

import Orchestrator.retrieval as retrieval


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeStore:
    """Minimal VectorStore stand-in: pre-normalized rows + cosine top-k."""

    def __init__(self, rows, dims=4):
        # rows: [(snap_id, vector)] — normalized here so `vec @ q` is cosine.
        self.dims = dims
        self._rows = []
        for sid, v in rows:
            vec = np.asarray(v, dtype=np.float32)
            n = float(np.linalg.norm(vec))
            self._rows.append((sid, vec / n if n > 0 else vec))

    @property
    def count(self):
        return len(self._rows)

    def search_with_vectors(self, query_vec, k, allowed_ids=None):
        q = np.asarray(query_vec, dtype=np.float32)
        n = float(np.linalg.norm(q))
        if n > 0:
            q = q / n
        scored = [
            (sid, float(vec @ q), vec.copy())
            for sid, vec in self._rows
            if allowed_ids is None or sid in allowed_ids
        ]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:k]


# Equal timestamps: recency boosts cancel, so RRF rank order decides alone.
# (Adjacent RRF ranks are near-ties by construction — 1/60 vs 1/61 — which the
# mild recency tie-break is DESIGNED to flip; equal ages keep these tests about
# the store override, not the tie-break.)
FAKE_INDEX = {
    "SNAP-A": {"operator": "alice", "timestamp": "2026-06-01T00:00:00Z"},
    "SNAP-B": {"operator": "alice", "timestamp": "2026-06-01T00:00:00Z"},
    "SNAP-C": {"operator": "bob", "timestamp": "2026-06-01T00:00:00Z"},
}


@pytest.fixture()
def hermetic(monkeypatch):
    """No network, no live store, no volume: pin every external seam."""
    # Query embeds to a fixed unit vector via the (patched) active-model path.
    monkeypatch.setattr(
        retrieval._emb, "generate_embedding_sync", lambda text, purpose="query": [1.0, 0.0, 0.0, 0.0]
    )
    # The active store must NEVER be consulted when an override is passed.
    def _boom():
        raise AssertionError(
            "get_active_store() consulted despite store= override (eval seam broken)"
        )
    monkeypatch.setattr(retrieval._emb, "get_active_store", _boom)
    # Hermetic index + dead keyword channel (retrieval imported these by name).
    monkeypatch.setattr(retrieval, "load_snapshot_index", lambda: dict(FAKE_INDEX))
    monkeypatch.setattr(
        retrieval, "keyword_retrieve_ids_for_operator", lambda vol, q, n, op: []
    )


# ── store= override ───────────────────────────────────────────────────────────

def test_override_store_scores_and_active_store_never_touched(hermetic):
    store = FakeStore([
        ("SNAP-A", [1.0, 0.1, 0.0, 0.0]),   # near-aligned with the query
        ("SNAP-B", [0.7, 0.7, 0.0, 0.0]),   # cos ≈ 0.70 — above junk floor
        ("SNAP-C", [0.0, 0.0, 1.0, 0.0]),   # orthogonal — junk-floored out
    ])
    results = retrieval.retrieve("test query", "system", k=5, store=store)
    ids = [sid for sid, _ in results]
    assert ids[0] == "SNAP-A"
    assert "SNAP-B" in ids
    assert "SNAP-C" not in ids  # cos 0.0 < junk floor
    # get_active_store is patched to raise — reaching here proves it was never called.


def test_override_store_respects_operator_scoping(hermetic):
    store = FakeStore([
        ("SNAP-A", [1.0, 0.1, 0.0, 0.0]),
        ("SNAP-C", [1.0, 0.0, 0.1, 0.0]),  # bob's — must be scoped out for alice
    ])
    results = retrieval.retrieve("test query", "alice", k=5, store=store)
    ids = [sid for sid, _ in results]
    assert "SNAP-A" in ids
    assert "SNAP-C" not in ids


def test_override_store_empty_returns_empty(hermetic):
    assert retrieval.retrieve("test query", "system", k=5, store=FakeStore([])) == []


# ── default path unchanged ────────────────────────────────────────────────────

def test_default_path_still_uses_active_store(monkeypatch):
    store = FakeStore([("SNAP-A", [1.0, 0.1, 0.0, 0.0])])
    calls = {"n": 0}

    def _active():
        calls["n"] += 1
        return store

    monkeypatch.setattr(
        retrieval._emb, "generate_embedding_sync", lambda text, purpose="query": [1.0, 0.0, 0.0, 0.0]
    )
    monkeypatch.setattr(retrieval._emb, "get_active_store", _active)
    monkeypatch.setattr(retrieval, "load_snapshot_index", lambda: dict(FAKE_INDEX))
    monkeypatch.setattr(
        retrieval, "keyword_retrieve_ids_for_operator", lambda vol, q, n, op: []
    )

    results = retrieval.retrieve("test query", "system", k=5)
    assert calls["n"] == 1, "default path must resolve the store via get_active_store()"
    assert [sid for sid, _ in results] == ["SNAP-A"]
