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

The behavioral and peak tests read this box's ledger (marker live_store) and skip
on a box without one.
"""
import gc
import tracemalloc

import pytest

from Orchestrator.fossils import (
    hybrid_retrieve,
    extract_snap_ids,
    read_volume_bytes,
    keyword_retrieve_for_operator,
)
from Orchestrator.config import VOL_PATH
from Orchestrator.tests.live_store import assert_has_recent


def _vol():
    if not VOL_PATH.exists():
        pytest.skip("no ledger on this box (fresh box)")
    return read_volume_bytes(VOL_PATH).decode("utf-8", "replace")


# Behavioral queries (no pinned id baseline — ranking is recency-aware by design).
QUERIES = [
    "embeddings model switch reembed",
    "control phone on-device gemma",
    "ugv nav2 slam tuning costmap",
]


@pytest.mark.live_store
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


@pytest.mark.live_store
def test_hybrid_retrieve_surfaces_recent_work():
    """(c) Recency tie-break: a recent snapshot appears in the top-k for a topic
    with recent activity (the recency fix is the point of Phase 3b)."""
    vol = _vol()
    results = hybrid_retrieve(vol, "embeddings model switch reembed", k=5, operator="system")
    assert_has_recent(extract_snap_ids(results))


HYBRID_PEAK_FACTOR = 0.25
HYBRID_PEAK_SLACK_BYTES = 16 * 1024 * 1024
MIN_MEANINGFUL_VOLUME_BYTES = 1024 * 1024


def _hybrid_peak_ceiling_bytes(volume_bytes: int) -> int:
    return int(volume_bytes * HYBRID_PEAK_FACTOR) + HYBRID_PEAK_SLACK_BYTES


def test_hybrid_peak_ceiling_still_catches_a_full_volume_decode():
    """The relative ceiling must stay well below a reintroduced full-volume
    read_text_safe decode (~7x the volume) and a copy of the bytes the
    keyword scan scores (~1x), at any volume size."""
    for vol_mb in (35, 56, 120, 400):
        vol_bytes = vol_mb * 1024 * 1024
        assert _hybrid_peak_ceiling_bytes(vol_bytes) < 7 * vol_bytes
        assert _hybrid_peak_ceiling_bytes(vol_bytes) < vol_bytes


@pytest.mark.live_store
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
    whenever the index is non-empty, dropping the steady-state peak to ~39MB, and
    the keyword scan now slices the shared ledger map instead of copying the bytes
    it scores (audit rank 77), dropping it to ~0.1x the volume. This test pins an
    honest post-fix ceiling relative to the volume (see _hybrid_peak_ceiling_bytes)
    so that a reintroduced copy of the scored bytes (~1x), a full-volume decode
    (~250MB) or an unbounded/O(n^2) blowup is caught.
    """
    vol_bytes = len(read_volume_bytes(VOL_PATH)) if VOL_PATH.exists() else 0
    if vol_bytes < MIN_MEANINGFUL_VOLUME_BYTES:
        pytest.skip(f"live volume too small to measure a peak ({vol_bytes} bytes)")
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

    mb = 1024 * 1024
    hybrid_mb = peak_hybrid / mb
    ceiling = _hybrid_peak_ceiling_bytes(vol_bytes)
    # Honest post-fix ceiling: the steady-state peak was ~1.07x the volume (the
    # streaming keyword scan held a copy of the bytes it scored: ~39MB at ~35MB,
    # 59.3MB at 55.6MB on 2026-10-11) and is ~0.1x since the scan slices the
    # shared ledger map (6.5MB at 55.9MiB on 2026-10-29, audit rank 77). The
    # ceiling is RELATIVE to the volume because the ledger grows with every mint.
    # 0.25x + 16MB catches that copy coming back (~1x), a reintroduced
    # full-volume read_text_safe decode (~7x) or an unbounded / O(n^2) blowup.
    assert peak_hybrid <= ceiling, (
        f"hybrid_retrieve peak {hybrid_mb:.1f}MB exceeded the {ceiling / mb:.1f}MB ceiling "
        f"({HYBRID_PEAK_FACTOR}x the {vol_bytes / mb:.1f}MB volume + "
        f"{HYBRID_PEAK_SLACK_BYTES // mb}MB) — a new unbounded allocation (e.g. a "
        f"reintroduced full-volume decode) may have been introduced"
    )
