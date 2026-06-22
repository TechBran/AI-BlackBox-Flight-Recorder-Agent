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

----------------------------------------------------------------------------------
FRAGILE-TEST FIX (ranking parity): the two ranking-parity tests below previously pinned
EXACT snap_id orderings captured from the scorers run over the LIVE PRODUCTION VOLUME
(Volumes/SNAPSHOT_VOLUME.txt). That volume GROWS every time a snapshot is minted, so a
newly-minted snapshot relevant to a test query (e.g. SNAP-20260621-7209 about
"memory retrieval / embeddings model migration") would slot into the middle of a pinned
ordering and break the assertion -- even though the SCORER CODE is unchanged and
correct. The failure was a property of the test's INPUT (non-deterministic live data),
not the code under test.

The fix: run the scorers over a FIXED, in-test SYNTHETIC corpus (see SYNTHETIC_SNAPS
below) instead of the live volume. The expected orderings are now STABLE because the
input is frozen -- minting real snapshots can never change them. The corpus is small but
deliberately constructed so the ranking still exercises the full pipeline: TF-IDF
(df/idf over the corpus), exact-phrase bigram/trigram boosts, the operator-path recency
bonus, operator filtering, and the operator re-score refinement stage. See the per-test
docstrings for exactly which ranking signal each assertion guards.

PEAK measurement (test_keyword_scorer_peak_ceiling):
  Measured with tracemalloc in this same process, steady-state (caches warm).
  OLD peak (commit f9dd2c8, before 2b): 127.3 MB for
    keyword_retrieve_ids_for_operator(vol, q, 20, "system").
  NEW peak (this change): 38.0 MB (now floored by vol_bytes alone, the ~89 MB
    materialized `texts` copy is gone). The non-operator scorer dropped 91.6 -> 2.4 MB.
  Since both versions can't run in one process, we pin an ABSOLUTE post-change
  ceiling of 55 MB (comfortably above the measured 38 MB, far below the OLD 127 MB).
  This test still reads the LIVE volume on purpose: it measures the per-call memory
  high-water mark, which is a function of the real corpus SIZE, not of any ordering.
"""
import gc
import tracemalloc

import Orchestrator.fossils as fossils
from Orchestrator.fossils import (
    _keyword_retrieve_scored,
    _keyword_retrieve_for_operator_scored,
    _streaming_tfidf_scores,
    keyword_retrieve_ids_for_operator,
    read_volume_bytes,
)
from Orchestrator.config import VOL_PATH, START_RX


def _vol():
    return read_volume_bytes(VOL_PATH).decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# FIXED synthetic corpus (the fragile-test fix).
#
# Each entry is (snap_id, utc, operator, body). The blocks are assembled into a
# valid volume via _make_block() below: a `=== START SNAPSHOT - UTC ... - <snap_id> ...
# ===` marker (em-dash separators + a trailing ` (2.0.0)` version, exactly as the live
# volume writes them, so START_RX/END_RX parse them byte-for-byte like real snapshots),
# an OPERATOR: line, the body, and a matching END marker.
#
# The corpus is ORDERED OLDEST -> NEWEST (index 0 is oldest). The operator scorer sorts
# its candidates by snap_id and applies a recency bonus where the LAST element is the
# newest -- the snap_ids are chosen so lexical sort == chronological order, so index
# position == recency rank.
#
# Query under test: "embeddings model switch reembed".
#   _extract_terms stems this to ['embedding', 'model', 'switch', 'reemb'];
#   the bigram/trigram phrase boosts match the RAW words ("embeddings model", etc.), so
#   only a doc containing the literal phrase "embeddings model switch reembed" gets them.
#
# Doc roles (deliberate, to exercise distinct ranking signals):
#   201 (system, oldest)  : keyword-only doc; IDENTICAL keyword content to 205.
#   202 (Brandon)         : off-topic ("control phone ... gemma") -> scores 0, dropped.
#   203 (system)          : the ONLY doc with the literal phrase -> wins on bigram/
#                           trigram boost regardless of recency. Always rank #1.
#   204 (Brandon)         : scattered single-term hits, no exact phrase, no "reemb".
#   205 (system, newest)  : keyword-IDENTICAL to 201.
#
# Why this set proves each signal:
#   * 203 first  -> exact-phrase (bigram/trigram) boost dominates TF-IDF.
#   * 201 vs 205 -> identical TF-IDF; the NON-OP scorer has NO recency tiebreak, so the
#     stable sort keeps corpus order (201 before 205); the OPERATOR scorer's recency
#     bonus flips them (205 before 201). Same two docs, opposite order across the two
#     scorers == direct proof the operator recency stage is live and distinct.
#   * Brandon path returns ONLY 204 -> operator filtering + re-score stage drop the
#     system-only docs (201/203/205) and the off-topic Brandon doc (202).
# ---------------------------------------------------------------------------
SYNTHETIC_SNAPS = [
    ("SNAP-20260101-201", "2026-01-01T00:00:00Z", "system",
     "embedding registry cache query notes. embedding cache query."),
    ("SNAP-20260102-202", "2026-01-02T00:00:00Z", "Brandon",
     "control phone on-device gemma agent unrelated."),
    ("SNAP-20260103-203", "2026-01-03T00:00:00Z", "system",
     "full phrase embeddings model switch reembed appears here once."),
    ("SNAP-20260104-204", "2026-01-04T00:00:00Z", "Brandon",
     "embedding model switch words scattered, no reembed, no exact phrase."),
    ("SNAP-20260105-205", "2026-01-05T00:00:00Z", "system",
     "embedding registry cache query notes. embedding cache query."),
]

SYNTH_QUERY = "embeddings model switch reembed"

# Deterministic expected orderings over the FIXED corpus above (k=20 returns all
# nonzero-scoring docs). Verified by running the scorers on the synthetic corpus; they
# are STABLE because the corpus is frozen (immune to live-volume growth / new mints).
EXPECTED_NONOP = ["SNAP-20260103-203", "SNAP-20260104-204",
                  "SNAP-20260101-201", "SNAP-20260105-205"]
EXPECTED_OP_SYSTEM = ["SNAP-20260103-203", "SNAP-20260104-204",
                      "SNAP-20260105-205", "SNAP-20260101-201"]
EXPECTED_OP_BRANDON = ["SNAP-20260104-204"]


def _make_block(snap_id, utc, operator, body):
    """Assemble one well-formed snapshot block (matches the live volume format:
    em-dash separators, trailing ` (2.0.0)` version, CRLF line endings)."""
    start = f"=== START SNAPSHOT — UTC {utc} — {snap_id} (2.0.0) ===\r\n"
    end = f"=== END SNAPSHOT — {snap_id} — UTC {utc} ===\r\n"
    return f"{start}OPERATOR: {operator}\r\n{body}\r\n{end}"


def _build_synthetic_corpus():
    """Return (vol_txt, vol_bytes, index) for the fixed synthetic corpus.

    `index` mirrors the production snapshot_index shape ({snap_id: {byte_start,
    byte_end, operator, timestamp}}) with byte offsets that slice `vol_bytes` back to
    each block -- this is what the operator scorer reads internally, so monkeypatching
    load_snapshot_index/read_volume_bytes to return these makes the operator path score
    the SAME fixed docs the non-operator path sees.
    """
    vol_bytes = b""
    index = {}
    for snap_id, utc, operator, body in SYNTHETIC_SNAPS:
        block_bytes = _make_block(snap_id, utc, operator, body).encode("utf-8")
        start = len(vol_bytes)
        vol_bytes += block_bytes
        end = len(vol_bytes)
        index[snap_id] = {
            "byte_start": start,
            "byte_end": end,
            "operator": operator,
            "timestamp": utc,
        }
    return vol_bytes.decode("utf-8"), vol_bytes, index


def _ids_nonop(vol, q, k=20):
    return [sid for sid, _t in _keyword_retrieve_scored(vol, q, k)]


def _ids_op(vol, q, op, k=20):
    return [sid for sid, _t in _keyword_retrieve_for_operator_scored(vol, q, k, op)]


def test_synthetic_corpus_is_well_formed():
    """Guard the fixture itself: every synthetic block must parse via the SAME
    START_RX the scorers use, and its byte offsets must slice back to the right id.
    If this breaks, the ranking assertions below would be meaningless."""
    vol_txt, vol_bytes, index = _build_synthetic_corpus()
    assert len(index) == len(SYNTHETIC_SNAPS)
    for snap_id, meta in index.items():
        chunk = vol_bytes[meta["byte_start"]:meta["byte_end"]].decode("utf-8")
        m = START_RX.search(chunk)
        assert m is not None, f"START marker did not parse for {snap_id}"
        assert m.group("snap") == snap_id, (
            f"byte offsets point at {m.group('snap')!r}, expected {snap_id!r}"
        )
    # The non-operator scorer's own span splitter must find every block too.
    spans = fossils.split_snapshot_spans(vol_txt)
    assert len(spans) == len(SYNTHETIC_SNAPS)


def test_nonoperator_scorer_ranking_parity():
    """Non-operator scorer ranking over the FIXED synthetic corpus.

    Guards TF-IDF + exact-phrase (bigram/trigram) boosts and the absence of a recency
    tiebreak: 203 wins on the literal-phrase boost; the keyword-identical 201/205 pair
    keeps corpus order (oldest-first) because this scorer applies NO recency bonus.
    Deterministic + immune to live-volume growth (input is the frozen corpus, not
    Volumes/SNAPSHOT_VOLUME.txt)."""
    vol_txt, _vol_bytes, _index = _build_synthetic_corpus()
    got = _ids_nonop(vol_txt, SYNTH_QUERY)
    assert got == EXPECTED_NONOP, f"non-op ranking drift: {got} != {EXPECTED_NONOP}"


def test_operator_scorer_ranking_parity_system(monkeypatch):
    """Operator scorer (op="system", sees all) over the FIXED synthetic corpus.

    The operator scorer reads load_snapshot_index() + read_volume_bytes(VOL_PATH)
    internally; we monkeypatch BOTH (on the Orchestrator.fossils module, where the
    scorer looks them up) to return the synthetic index + bytes, so it scores the same
    fixed docs deterministically.

    Guards the operator-path RECENCY bonus + re-score stage: the keyword-identical
    201/205 pair is ordered NEWEST-first here (205 before 201) -- the opposite of the
    non-operator scorer above -- which can ONLY come from the operator recency bonus.
    203 still leads on the exact-phrase boost. Immune to live-volume growth (the live
    volume is never read for this assertion)."""
    _vol_txt, vol_bytes, index = _build_synthetic_corpus()
    monkeypatch.setattr(fossils, "load_snapshot_index", lambda: index)
    monkeypatch.setattr(fossils, "read_volume_bytes", lambda _path: vol_bytes)

    # vol_txt arg is unused by the index path of the operator scorer; pass empty to
    # prove the assertion does NOT depend on the live volume text.
    got = _ids_op("", SYNTH_QUERY, "system")
    assert got == EXPECTED_OP_SYSTEM, (
        f"op=system ranking drift: {got} != {EXPECTED_OP_SYSTEM}"
    )


def test_operator_scorer_ranking_parity_brandon(monkeypatch):
    """Operator scorer (op="Brandon") over the FIXED synthetic corpus.

    Guards operator FILTERING + the re-score refinement stage: only Brandon's relevant
    doc (204) survives -- the system-only docs (201/203/205) and Brandon's off-topic doc
    (202, which scores 0) are dropped. Deterministic + immune to live-volume growth."""
    _vol_txt, vol_bytes, index = _build_synthetic_corpus()
    monkeypatch.setattr(fossils, "load_snapshot_index", lambda: index)
    monkeypatch.setattr(fossils, "read_volume_bytes", lambda _path: vol_bytes)

    got = _ids_op("", SYNTH_QUERY, "Brandon")
    assert got == EXPECTED_OP_BRANDON, (
        f"op=Brandon ranking drift: {got} != {EXPECTED_OP_BRANDON}"
    )


def test_scorer_returns_text_for_topk(monkeypatch):
    """The (sid, text) tuples must still carry real decoded text for the top-k
    results (the win decodes only top-k, but it MUST still decode them).

    Runs over the FIXED synthetic corpus so the contract is checked deterministically
    against known docs (not the live volume)."""
    _vol_txt, vol_bytes, index = _build_synthetic_corpus()
    monkeypatch.setattr(fossils, "load_snapshot_index", lambda: index)
    monkeypatch.setattr(fossils, "read_volume_bytes", lambda _path: vol_bytes)

    results = _keyword_retrieve_for_operator_scored("", SYNTH_QUERY, 5, "system")
    assert len(results) <= 5
    assert results, "expected at least one scored result over the synthetic corpus"
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

    This test reads the LIVE volume on purpose: the per-call high-water mark is a
    function of the real corpus SIZE, not of any (mint-sensitive) ordering, so it is
    not fragile to new snapshots.
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
