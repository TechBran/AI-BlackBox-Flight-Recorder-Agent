"""Ranking-parity + per-call PEAK tests for the keyword scorers (Phase 2b).

Phase 2b goal: cut the ~127 MB per-call PEAK of the keyword scorer (which fires on
every chat/voice search and is a prime OOM-recycle contributor) WITHOUT changing the
ranking output at all.

The win implemented in 2b: BOTH scorers no longer MATERIALIZE the whole corpus of
decoded snapshot strings. The OLD operator scorer held `vol_bytes` (~37 MB raw) AND a
`texts` list of ~7176 decoded strings (~89 MB of str objects) ALIVE SIMULTANEOUSLY for
the entire TF-IDF + scoring pass -- that coexistence was the ~127 MB high-water mark.
The OLD non-operator scorer similarly built a ~89 MB list of decoded substrings via
split_snapshots().

The fix streams: decode each snapshot on demand for the corpus document-frequency pass
and again for the scoring pass (folding TF-IDF inline), holding only ONE lowered text
at a time plus the small (score, i) tuples, then decode the top-k for the return value.
The TF-IDF math is INLINED but byte-for-byte identical to _compute_tfidf_scores (same
df pass, same `lc.count(term)` TF, same `log(num_docs/df[term])` IDF), and the
technical/recency/bigram/trigram boosts, the `score > 0` filter, the iteration/sort
order, and the re-score refinement stage are unchanged. The non-operator scorer uses a
new split_snapshot_spans() (offsets only, no substring copies) and slices vol_txt on
demand. Verified bit-equal to the old _compute_tfidf_scores across all test queries.

The BASELINE ids below were captured from the PRE-CHANGE scorers (commit f9dd2c8) by
running the test on the unchanged code first. The parity tests assert post-change
output equals these baselines EXACTLY, order included.

PEAK measurement (test_keyword_scorer_peak_ceiling):
  Measured with tracemalloc in this same process, steady-state (caches warm).
  OLD peak (commit f9dd2c8, before 2b): 127.3 MB for
    keyword_retrieve_ids_for_operator(vol, q, 20, "system").
  NEW peak (this change): 38.0 MB (now floored by vol_bytes alone, the ~89 MB
    materialized `texts` copy is gone). The non-operator scorer dropped 91.6 -> 2.4 MB.
  Since both versions can't run in one process, we pin an ABSOLUTE post-change
  ceiling of 55 MB (comfortably above the measured 38 MB, far below the OLD 127 MB).
"""
import gc
import math
import tracemalloc

from Orchestrator.fossils import (
    _keyword_retrieve_scored,
    _keyword_retrieve_for_operator_scored,
    _streaming_tfidf_scores,
    keyword_retrieve_ids_for_operator,
    read_volume_bytes,
)
from Orchestrator.config import VOL_PATH


def _vol():
    return read_volume_bytes(VOL_PATH).decode("utf-8", "replace")


QUERIES = [
    "embeddings model switch reembed",
    "control phone on-device gemma",
    "ugv nav2 slam tuning costmap",
]


# Baseline captured from the PRE-CHANGE scorers (commit f9dd2c8). Each entry maps a
# query to the ranked snap_id list. _keyword_retrieve_scored is the non-operator
# scorer; _keyword_retrieve_for_operator_scored is run for "system" and "Brandon".
# k=20 to exercise a meaningful slice of the ranking, not just the top few.
BASELINE_NONOP = {
    "embeddings model switch reembed": ['SNAP-20260613-7031', 'SNAP-20260612-7011', 'SNAP-20260423-6214', 'SNAP-20260128-2792', 'SNAP-20260614-7060', 'SNAP-20260613-7028', 'SNAP-20260309-4235', 'SNAP-20260328-5021', 'SNAP-20260211-3369', 'SNAP-20260329-5078', 'SNAP-20260227-3907', 'SNAP-20260211-3371', 'SNAP-20260425-6256', 'SNAP-20251208-1678', 'SNAP-20260212-3379', 'SNAP-20260612-7013', 'SNAP-20260211-3372', 'SNAP-20251122-1261', 'SNAP-20260228-3936', 'SNAP-20251026-814'],
    "control phone on-device gemma": ['SNAP-20260619-7168', 'SNAP-20260614-7066', 'SNAP-20260619-7181', 'SNAP-20260615-7087', 'SNAP-20260617-7116', 'SNAP-20260619-7184', 'SNAP-20260619-7188', 'SNAP-20260410-5879', 'SNAP-20260619-7183', 'SNAP-20260619-7187', 'SNAP-20260619-7182', 'SNAP-20260619-7169', 'SNAP-20260619-7191', 'SNAP-20260619-7185', 'SNAP-20260530-6868', 'SNAP-20260619-7189', 'SNAP-20260619-7190', 'SNAP-20260617-7118', 'SNAP-20260617-7096', 'SNAP-20260410-5887'],
    "ugv nav2 slam tuning costmap": ['SNAP-20260418-6105', 'SNAP-20260429-6346', 'SNAP-20260419-6108', 'SNAP-20260416-5980', 'SNAP-20260417-6009', 'SNAP-20260426-6293', 'SNAP-20260418-6053', 'SNAP-20260418-6104', 'SNAP-20260508-6520', 'SNAP-20260418-6065', 'SNAP-20260408-5801', 'SNAP-20260427-6314', 'SNAP-20260505-6459', 'SNAP-20260410-5865', 'SNAP-20260407-5691', 'SNAP-20260416-5943', 'SNAP-20260405-5600', 'SNAP-20260504-6451', 'SNAP-20260402-5418', 'SNAP-20260412-5897'],
}
BASELINE_OP_SYSTEM = {
    "embeddings model switch reembed": ['SNAP-20260226-3828', 'SNAP-20260613-7031', 'SNAP-20260128-2792', 'SNAP-20260612-7011', 'SNAP-20260613-7028', 'SNAP-20260211-3369', 'SNAP-20260423-6214', 'SNAP-20260309-4235', 'SNAP-20260614-7060', 'SNAP-20260328-5021', 'SNAP-20260612-7013', 'SNAP-20260227-3907', 'SNAP-20260212-3379', 'SNAP-20260211-3371', 'SNAP-20260329-5078', 'SNAP-20260228-3936', 'SNAP-20260425-6256', 'SNAP-20260211-3372', 'SNAP-20251208-1678', 'SNAP-20251122-1261'],
    "control phone on-device gemma": ['SNAP-20260619-7168', 'SNAP-20260530-6868', 'SNAP-20260614-7066', 'SNAP-20260619-7181', 'SNAP-20260617-7116', 'SNAP-20260619-7188', 'SNAP-20260619-7191', 'SNAP-20260619-7184', 'SNAP-20260619-7182', 'SNAP-20260619-7183', 'SNAP-20260619-7187', 'SNAP-20260615-7087', 'SNAP-20260619-7169', 'SNAP-20260619-7185', 'SNAP-20260410-5879', 'SNAP-20260619-7189', 'SNAP-20260619-7190', 'SNAP-20260617-7118', 'SNAP-20260617-7096', 'SNAP-20260410-5887'],
    "ugv nav2 slam tuning costmap": ['SNAP-20260418-6105', 'SNAP-20260416-5980', 'SNAP-20260419-6108', 'SNAP-20260429-6346', 'SNAP-20260417-6009', 'SNAP-20260426-6293', 'SNAP-20260418-6104', 'SNAP-20260427-6314', 'SNAP-20260418-6053', 'SNAP-20260408-5801', 'SNAP-20260508-6520', 'SNAP-20260418-6065', 'SNAP-20260505-6459', 'SNAP-20260410-5865', 'SNAP-20260405-5600', 'SNAP-20260504-6451', 'SNAP-20260402-5418', 'SNAP-20260412-5897', 'SNAP-20260416-5943', 'SNAP-20260407-5691'],
}
BASELINE_OP_BRANDON = {
    "embeddings model switch reembed": ['SNAP-20260226-3828', 'SNAP-20260613-7031', 'SNAP-20260128-2792', 'SNAP-20260612-7011', 'SNAP-20260613-7028', 'SNAP-20260211-3369', 'SNAP-20260423-6214', 'SNAP-20260309-4235', 'SNAP-20260614-7060', 'SNAP-20260328-5021', 'SNAP-20260612-7013', 'SNAP-20260227-3907', 'SNAP-20260212-3379', 'SNAP-20260211-3371', 'SNAP-20260329-5078', 'SNAP-20260228-3936', 'SNAP-20260425-6256', 'SNAP-20260211-3372', 'SNAP-20251208-1678', 'SNAP-20251122-1261'],
    "control phone on-device gemma": ['SNAP-20260619-7168', 'SNAP-20260530-6868', 'SNAP-20260614-7066', 'SNAP-20260619-7181', 'SNAP-20260617-7116', 'SNAP-20260619-7188', 'SNAP-20260619-7191', 'SNAP-20260619-7184', 'SNAP-20260619-7182', 'SNAP-20260619-7183', 'SNAP-20260619-7187', 'SNAP-20260615-7087', 'SNAP-20260619-7169', 'SNAP-20260619-7185', 'SNAP-20260410-5879', 'SNAP-20260619-7189', 'SNAP-20260619-7190', 'SNAP-20260617-7118', 'SNAP-20260617-7096', 'SNAP-20260410-5887'],
    "ugv nav2 slam tuning costmap": ['SNAP-20260418-6105', 'SNAP-20260416-5980', 'SNAP-20260419-6108', 'SNAP-20260429-6346', 'SNAP-20260417-6009', 'SNAP-20260426-6293', 'SNAP-20260418-6104', 'SNAP-20260427-6314', 'SNAP-20260418-6053', 'SNAP-20260408-5801', 'SNAP-20260508-6520', 'SNAP-20260418-6065', 'SNAP-20260505-6459', 'SNAP-20260410-5865', 'SNAP-20260405-5600', 'SNAP-20260504-6451', 'SNAP-20260412-5897', 'SNAP-20260416-5943', 'SNAP-20260505-6458', 'SNAP-20260407-5691'],
}


def _ids_nonop(vol, q, k=20):
    return [sid for sid, _t in _keyword_retrieve_scored(vol, q, k)]


def _ids_op(vol, q, op, k=20):
    return [sid for sid, _t in _keyword_retrieve_for_operator_scored(vol, q, k, op)]


def test_nonoperator_scorer_ranking_parity():
    vol = _vol()
    for q in QUERIES:
        expected = BASELINE_NONOP[q]
        assert expected is not None, f"baseline not pinned for {q!r}"
        got = _ids_nonop(vol, q)
        assert got == expected, f"non-op ranking drift for {q!r}: {got} != {expected}"


def test_operator_scorer_ranking_parity_system():
    vol = _vol()
    for q in QUERIES:
        expected = BASELINE_OP_SYSTEM[q]
        assert expected is not None, f"baseline not pinned for {q!r}"
        got = _ids_op(vol, q, "system")
        assert got == expected, f"op=system ranking drift for {q!r}: {got} != {expected}"


def test_operator_scorer_ranking_parity_brandon():
    vol = _vol()
    for q in QUERIES:
        expected = BASELINE_OP_BRANDON[q]
        assert expected is not None, f"baseline not pinned for {q!r}"
        got = _ids_op(vol, q, "Brandon")
        assert got == expected, f"op=Brandon ranking drift for {q!r}: {got} != {expected}"


def test_scorer_returns_text_for_topk():
    """The (sid, text) tuples must still carry real decoded text for the top-k
    results (the win decodes only top-k, but it MUST still decode them)."""
    vol = _vol()
    results = _keyword_retrieve_for_operator_scored(vol, "control phone on-device gemma", 5, "system")
    assert len(results) <= 5
    assert all(isinstance(sid, str) and sid for sid, _t in results)
    assert all(isinstance(t, str) and len(t) > 0 for _sid, t in results)


# OLD peak (commit f9dd2c8, pre-2b) measured 127.3 MB. NEW measured 38.0 MB.
PEAK_CEILING_BYTES = 55 * 1024 * 1024


def test_keyword_scorer_peak_ceiling():
    """Per-call tracemalloc PEAK of the operator scorer must sit below the ceiling.

    OLD (pre-2b): 127.3 MB because vol_bytes (~37 MB) + a fully-materialized `texts`
    list (~89 MB) coexisted for the whole scan. NEW: 38.0 MB (streamed; floored by
    vol_bytes alone). We pin an absolute ceiling of 55 MB since both versions cannot
    run in one process.
    """
    vol = _vol()
    q = "embeddings model switch reembed"
    # Warm caches (index, volume read) so we measure steady-state, not load.
    _ = keyword_retrieve_ids_for_operator(vol, q, 20, "system")

    gc.collect()
    tracemalloc.start()
    _ = keyword_retrieve_ids_for_operator(vol, q, 20, "system")
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    peak_mb = peak / 1024 / 1024
    assert peak <= PEAK_CEILING_BYTES, (
        f"keyword scorer per-call peak {peak_mb:.1f}MB exceeds ceiling "
        f"{PEAK_CEILING_BYTES / 1024 / 1024:.0f}MB (OLD pre-2b was ~127MB)"
    )


def test_shared_tfidf_helper_decode_path_agnostic():
    """Guard: the shared base TF-IDF must be identical regardless of HOW a doc's
    lowered text is decoded -- this is what keeps the operator and non-operator
    scorers from silently desyncing if one's df/idf formula is later edited.

    The non-op path decodes via vol_txt slices; the operator path decodes via
    vol_bytes byte-offsets. We model both decode shapes over one shared corpus and
    assert _streaming_tfidf_scores yields the same base score per doc for each.
    """
    docs = [
        "embeddings model switch reembed pipeline",
        "control phone on-device gemma agent",
        "embeddings reembed model registry switch",
        "ugv nav2 slam tuning costmap",
        "no matching terms here at all",
    ]
    terms = ["embeddings", "model", "switch", "reembed"]
    num_docs = len(docs)

    # Non-op-style decode: lower a substring of a joined text (offset slice).
    def decode_lc_text(i):
        return docs[i].lower()

    # Operator-style decode: lower a decoded byte slice.
    raw = [d.encode("utf-8") for d in docs]
    def decode_lc_bytes(i):
        return raw[i].decode("utf-8", "replace").lower()

    base_text = _streaming_tfidf_scores(decode_lc_text, num_docs, terms)
    base_bytes = _streaming_tfidf_scores(decode_lc_bytes, num_docs, terms)

    assert base_text == base_bytes, (
        f"shared TF-IDF diverged across decode paths: {base_text} != {base_bytes}"
    )
    # Sanity: it actually computed real TF-IDF (some nonzero, the no-term doc is 0).
    assert any(s > 0 for s in base_text)
    assert base_text[-1] == 0.0
    # Empty-terms contract: every doc scores exactly 1.0.
    assert _streaming_tfidf_scores(decode_lc_text, num_docs, []) == [1.0] * num_docs
