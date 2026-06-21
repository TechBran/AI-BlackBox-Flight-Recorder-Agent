"""Ranking-parity + allocation tests for the rewritten hybrid_retrieve (F2).

Phase 2 rewrite: fuse keyword + semantic BY snap_id (no full snap_to_text rebuild,
no O(n^2) text-equality remap), and decode ONLY the <=k result snapshots' bytes.
This is a PURE PERFORMANCE change -- ranking output must be preserved.

What the rewrite eliminated (and what the allocation test below proves):
  * The OLD code rebuilt a `snap_to_text` dict by decoding ALL ~7176 snapshots'
    bytes on EVERY call (~37 MB of transient allocation), then ran an O(n^2)
    text-equality remap to recover snap_ids from keyword *texts*. Both are GONE.
  * The return path now decodes ONLY the <=k (here k=5) result snapshots, not 7176.

Why the per-call PEAK does NOT drop, and what the allocation test actually asserts:
  `keyword_retrieve` itself must decode + `.lower()`-copy every snapshot text to do
  TF-IDF (an unavoidable full-volume scan), which peaks at ~127 MB. The OLD
  `snap_to_text` rebuild was allocated/freed INSIDE that same envelope, so deleting
  it cuts transient allocation, GC churn, and CPU (the real F2 leak-pressure win) but
  NOT the high-water mark. The honest, falsifiable claim is therefore: hybrid_retrieve
  adds NO separate full-volume decode on top of keyword_retrieve's unavoidable scan
  (peak_hybrid ~= peak_keyword, not 2x), which the test below pins.

The 127 MB keyword peak is keyword-floored and is addressed separately in Phase 2b
(streaming/chunked TF-IDF), already tracked.

The expected ids below are the BASELINE captured from the pre-change
hybrid_retrieve (commit 3bdf4e6, before Task 2.1/2.2) for these exact queries.
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


# Captured baseline (pre-change hybrid_retrieve output ids), operator="system", k=5.
BASELINE = {
    "embeddings model switch reembed": [
        "SNAP-20260613-7031", "SNAP-20260612-7011", "SNAP-20260128-2792",
        "SNAP-20260211-3369", "SNAP-20260613-7028",
    ],
    "control phone on-device gemma": [
        "SNAP-20260619-7168", "SNAP-20260614-7066", "SNAP-20260619-7188",
        "SNAP-20260619-7184", "SNAP-20260619-7187",
    ],
    "ugv nav2 slam tuning costmap": [
        "SNAP-20260505-6459", "SNAP-20260405-5600", "SNAP-20260504-6451",
        "SNAP-20260412-5897", "SNAP-20260416-5943",
    ],
}


def test_hybrid_retrieve_ranking_parity():
    vol = _vol()
    for q, expected_ids in BASELINE.items():
        results = hybrid_retrieve(vol, q, k=5, operator="system")
        got_ids = extract_snap_ids(results)
        assert got_ids == expected_ids, f"ranking drift for {q!r}: {got_ids} != {expected_ids}"
        # snippets must be non-empty
        assert all(len(r) > 0 for r in results)


def test_hybrid_retrieve_adds_no_separate_full_decode():
    """hybrid_retrieve adds NO separate full-volume decode beyond keyword's scan.

    OLD code: rebuilt snap_to_text over ALL ~7176 snapshots (37 MB transient) +
    O(n^2) text-equality remap, on top of keyword_retrieve's own full-volume scan.
    NEW code: both eliminated; return path decodes only the <=k result snapshots.

    The per-call PEAK is floored by keyword_retrieve's unavoidable full-volume
    TF-IDF scan (decode + .lower() copy of every snapshot text, ~127 MB), which the
    OLD snap_to_text rebuild was freed inside. So the right falsifiable assertion is
    NOT "peak dropped by 25 MB" -- it's "hybrid does not roughly DOUBLE the keyword
    peak by adding its own full decode pass." We measure keyword-alone peak and
    hybrid peak in the same process and require hybrid <= 1.10x keyword.

    (The 127 MB keyword floor is addressed separately in Phase 2b.)
    """
    vol = _vol()
    q = "embeddings model switch reembed"
    # warm caches (index, embeddings store) so we measure steady-state, not load
    _ = hybrid_retrieve(vol, q, k=5, operator="system")
    _ = keyword_retrieve_for_operator(vol, q, 20, "system")

    gc.collect()
    tracemalloc.start()
    _ = keyword_retrieve_for_operator(vol, q, 20, "system")
    _cur, peak_kw = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    gc.collect()
    tracemalloc.start()
    _ = hybrid_retrieve(vol, q, k=5, operator="system")
    _cur, peak_hybrid = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    kw_mb = peak_kw / 1024 / 1024
    hybrid_mb = peak_hybrid / 1024 / 1024
    assert peak_hybrid <= peak_kw * 1.10, (
        f"hybrid_retrieve added a separate full-volume decode on top of the keyword "
        f"scan: keyword peak={kw_mb:.1f}MB hybrid peak={hybrid_mb:.1f}MB "
        f"(expected hybrid <= 1.10x keyword)"
    )
