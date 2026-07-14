"""Layer-1 telemetry-sink tests for Orchestrator.retrieval.retrieve() ("The Signal").

Task 1.2: retrieve() accepts an optional ``telemetry`` dict and, when given,
fills it with the retrieval-stage metrics build_retrieval_activity consumes —
sourced from the variables retrieve() already computes (nothing is recomputed).
These tests drive the eval seam (store=/query_vector=) so they are hermetic: a
fake in-memory store, a supplied query vector, and the keyword channel disabled
(include_keyword=False) — no live embeddings provider, no volume read.

PORTABILITY: the empty-corpus case (store.count == 0) must STILL record
corpus_count (0) and embed_model BEFORE the early return, so a fresh box with an
empty ledger narrates "embedded, empty corpus" honestly rather than nothing.
"""
import numpy as np

from Orchestrator.retrieval import retrieve


class _FakeStore:
    """Minimal VectorStore stand-in for retrieve()'s eval seam (WI-6).

    Returns two candidates ABOVE the junk floor and one BELOW it, so
    ``candidates`` (survivors of the ``>= junk_floor`` filter) is
    deterministically 2 for any sane global floor (0.40 on this box). Supports
    ``with_ordinals`` because the live rerank sidecar makes retrieve() request
    4-tuples — handling both shapes keeps the test independent of whether the
    reranker happens to be enabled.
    """

    def __init__(self, slug="fake-embed-v1", dims=4, count=1234):
        self.slug = slug
        self.dims = dims
        self.count = count
        # (snap_id, cosine, unit vector, chunk ordinal)
        self._rows = [
            ("FAKE-1", 0.95, np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32), 0),
            ("FAKE-2", 0.90, np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32), 3),
            ("FAKE-3", 0.10, np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32), 0),  # < floor
        ]

    def search_with_vectors(self, qv, n, allowed, with_ordinals=False):
        if with_ordinals:
            return list(self._rows)
        return [(sid, cos, vec) for (sid, cos, vec, _o) in self._rows]


def test_retrieve_populates_telemetry_sink():
    """A populated telemetry dict carries the real embed identity, dims, corpus
    size, junk-floor survivor count, and MMR-selected count — all from the
    store/locals, none hardcoded."""
    tel: dict = {}
    fake = _FakeStore(slug="fake-embed-v1", dims=4, count=1234)
    results = retrieve(
        query="x", query_vector=[0.1, 0.2, 0.3, 0.4], store=fake,
        include_keyword=False, telemetry=tel,
    )
    assert tel["embed_model"] == "fake-embed-v1"   # store.slug
    assert tel["embed_dims"] == 4                   # len(query_vector)
    assert tel["corpus_count"] == 1234              # store.count
    assert tel["candidates"] == 2                   # survivors of >= junk_floor
    assert tel["mmr_topk"] == 2                     # final MMR selection (k=10 > 2)
    assert len(results) == 2


def test_retrieve_empty_corpus_still_records_corpus_count():
    """PORTABILITY: an empty corpus returns [] but STILL records corpus_count=0
    and the embed identity so a fresh box degrades honestly, not silently."""
    tel: dict = {}
    fake = _FakeStore(slug="fake-embed-v1", dims=4, count=0)
    results = retrieve(
        query="x", query_vector=[0.1, 0.2, 0.3, 0.4], store=fake,
        include_keyword=False, telemetry=tel,
    )
    assert results == []
    assert tel["corpus_count"] == 0
    assert tel["embed_model"] == "fake-embed-v1"
    assert tel["embed_dims"] == 4
