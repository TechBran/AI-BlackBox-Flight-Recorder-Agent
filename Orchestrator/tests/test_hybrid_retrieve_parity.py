"""Behavioral + allocation tests for hybrid_retrieve (Phase 3b).

Phase 3b rewrite: hybrid_retrieve is now a THIN SHIM over the canonical
Orchestrator.retrieval.retrieve() — RRF fusion of keyword + semantic candidates,
a mild recency tie-break, MMR diversity, and a junk-floor instead of a hard
threshold. This INTENTIONALLY changes the ranking that the old 40/60 weighted-sum
fuser produced — that is the whole point of routing this explicit-search surface
through the single canonical retriever.

Because the ranking changed BY DESIGN, the old byte-identical *parity* assertion
(pinned baseline ids from commit 3bdf4e6) no longer applies and was REMOVED. Re-
pinning a fresh arbitrary baseline would be a meaningless tautology, so instead we
assert the *behaviors* the new pipeline must exhibit: bounded top-k, real decoded
snapshot texts with START markers, recency surfacing recent work, and no duplicate
ids. The allocation test still proves hybrid adds no separate full-volume decode
on top of the keyword scan inside retrieve().

The earlier Phase-2 PARITY history (pre-change baseline ids) is intentionally not
preserved here — see git history of this file for the old pinned baseline.
"""
import gc
import tracemalloc

from Orchestrator.fossils import (
    hybrid_retrieve,
    extract_snap_ids,
    read_volume_bytes,
    keyword_retrieve_for_operator,
)
from Orchestrator.config import VOL_PATH


def _vol():
    return read_volume_bytes(VOL_PATH).decode("utf-8", "replace")


# Behavioral queries (no pinned id baseline — ranking is recency-aware by design).
QUERIES = [
    "embeddings model switch reembed",
    "control phone on-device gemma",
    "ugv nav2 slam tuning costmap",
]


def test_hybrid_retrieve_returns_bounded_real_snapshots():
    """Every query returns <=k real, non-empty, START-marked, de-duplicated snaps."""
    vol = _vol()
    k = 5
    for q in QUERIES:
        results = hybrid_retrieve(vol, q, k=k, operator="system")
        # (a) bounded by k
        assert len(results) <= k, f"{q!r}: got {len(results)} results, expected <= {k}"
        # results should be non-empty for these well-represented topics
        assert results, f"{q!r}: expected at least one result"
        # (b) each result is a non-empty text carrying a SNAP- START marker
        for r in results:
            assert isinstance(r, str) and len(r) > 0
            assert "=== START SNAPSHOT" in r and "SNAP-" in r, (
                f"{q!r}: a result is missing its START marker / SNAP- id"
            )
        # (d) no duplicate snap_ids in the output
        ids = extract_snap_ids(results)
        assert len(ids) == len(set(ids)), f"{q!r}: duplicate snap_ids in output: {ids}"


def test_hybrid_retrieve_surfaces_recent_work():
    """(c) Recency tie-break: a recent 2026-06 snapshot appears in the top-k for a
    topic with recent activity (the recency fix is the point of Phase 3b)."""
    vol = _vol()
    results = hybrid_retrieve(vol, "embeddings model switch reembed", k=5, operator="system")
    ids = extract_snap_ids(results)
    assert any(sid.startswith("SNAP-202606") for sid in ids), (
        f"expected a recent 2026-06 snapshot in the top-k, got: {ids}"
    )


def test_hybrid_retrieve_peak_is_bounded():
    """hybrid_retrieve's per-call peak stays BOUNDED (no unbounded / O(n^2) blowup).

    hybrid_retrieve delegates ranking to retrieve(). With a non-empty snapshot index
    (the production case), the dominant transient is the keyword channel's streaming
    TF-IDF scan over `read_volume_bytes` (per-snapshot decode, one snapshot held at a
    time — see _keyword_retrieve_for_operator_scored's Phase-2b peak fix) PLUS the
    semantic candidate-vector lookup. There is NO O(n^2) text-equality remap and NO
    per-snapshot full-corpus materialization — the peak is a small constant multiple
    of the volume size, never a function of k nor a quadratic of the snapshot count.

    FIXED (F2): retrieve() previously called `read_text_safe(VOL_PATH)` on EVERY call
    — a fresh full-volume bytes-read + utf-8 decode that spiked the transient to
    ~250MB — even though the index-backed keyword path IGNORES that decoded str (it
    decodes per-snapshot from read_volume_bytes on demand; verified: passing "" yields
    the SAME keyword ids when an index exists). retrieve() now skips read_text_safe
    whenever the index is non-empty, dropping the steady-state peak to ~39MB. This
    test pins an honest post-fix ceiling (<80MB, ~2x headroom over the ~39MB floor) so
    that a reintroduced full-volume decode (~250MB) or an unbounded/O(n^2) blowup is
    caught.
    """
    vol = _vol()
    q = "embeddings model switch reembed"
    # warm caches (index, embeddings store) so we measure steady-state, not load
    _ = hybrid_retrieve(vol, q, k=5, operator="system")
    _ = keyword_retrieve_for_operator(vol, q, 20, "system")

    gc.collect()
    tracemalloc.start()
    _ = hybrid_retrieve(vol, q, k=5, operator="system")
    _cur, peak_hybrid = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    hybrid_mb = peak_hybrid / 1024 / 1024
    # Honest post-fix ceiling: the steady-state peak is ~39MB (the streaming keyword
    # scan over read_volume_bytes, floored by the ~35MB volume). Pin at 80MB (~2x
    # headroom) so a reintroduced full-volume read_text_safe decode (~250MB) or an
    # unbounded / O(n^2) blowup is caught while real runs pass comfortably.
    assert hybrid_mb < 80.0, (
        f"hybrid_retrieve peak {hybrid_mb:.1f}MB exceeded the 80MB ceiling — a new "
        f"unbounded allocation (e.g. a reintroduced full-volume decode) may have "
        f"been introduced"
    )
