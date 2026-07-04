"""extract_snapshot_content() — body-only text for ranking (M14.1).

Snapshots lead with a fixed bookkeeping envelope (=== START SNAPSHOT … === /
CROSS-FILE BEACON / VOLUME TRACKER / GAUGES) that is near-identical across every
snapshot and measurably sabotages cross-encoder reranking (Vertex recall@10
0.654 -> 0.846 / +29% on body-only text — 2026-07-04 eval). This helper strips
that leading envelope, returning from the first content marker
("Raw Session Log", else "SNAPSHOT BODY") onward; a snapshot without either
marker (or non-snapshot text) is returned unchanged, never reduced to empty.
"""
from Orchestrator.fossils import extract_snapshot_content


# A realistic snapshot: the full bookkeeping envelope followed by SNAPSHOT BODY
# (Kernel Index + Context Provenance) then the Raw Session Log turns.
FULL_SNAPSHOT = """\
=== START SNAPSHOT — UTC 2026-07-04T22:37:21Z — SNAP-20260704-7980 (7.1.0) ===
CROSS-FILE BEACON
===============================================================================
Tail-first sweep resolved tip = SNAP-20260704-7979
COUNT=1 | TARGET_ID=SNAP-20260704-7980
UFL: OUTSIDE-JUNK IGNORED | BYTES_AFTER_END=0 | BYTES_BEFORE_START=0
Result: Tail lock confirmed
===============================================================================

VOLUME TRACKER
Tail: SNAP-20260704-7979
Next: TBD
Mode: NORMAL

GAUGES
CONTINUITY: TURNS
TOKENS (since last mint): prompt=54170, completion=2155, total=56325
MODEL: gemini-3.5-flash
OPERATOR: Anna
MODE: Normal

SNAPSHOT BODY

Kernel Index
- Tail: SNAP-20260704-7979
- Current: SNAP-20260704-7980
- Volume: Appliance/Overseer

Context Provenance
- GM_EXCERPT: yes
- Recent fossils: SNAP-20260704-7978, SNAP-20260704-7979
- Relevant fossils: SNAP-20260321-4647, SNAP-20251119-1101

Raw Session Log
- [1] 2026-07-04T22:37:21Z operator=Anna user: how do I add a reranker seam?
- [2] 2026-07-04T22:37:21Z operator=Anna assistant: add a provider abstraction in rerank.py.

Release Notes
- Added cross-encoder rerank provider abstraction with latency preflight.
=== END SNAPSHOT — SNAP-20260704-7980 ===
"""


def test_returns_from_raw_session_log_and_drops_envelope():
    result = extract_snapshot_content(FULL_SNAPSHOT)
    # Starts exactly at the Raw Session Log marker's line.
    assert result.startswith("Raw Session Log")
    # Keeps the content-bearing turns + the Release Notes summary after them.
    assert "how do I add a reranker seam?" in result
    assert "add a provider abstraction in rerank.py." in result
    assert "Release Notes" in result
    assert "Added cross-encoder rerank provider abstraction" in result
    # Drops the worst envelope offenders entirely.
    assert "CROSS-FILE BEACON" not in result
    assert "VOLUME TRACKER" not in result
    assert "GAUGES" not in result
    assert "START SNAPSHOT" not in result
    # The SNAPSHOT BODY / Kernel Index bookkeeping (above Raw Session Log) is
    # also dropped when Raw Session Log is present.
    assert "Kernel Index" not in result
    assert "Context Provenance" not in result


def test_falls_back_to_snapshot_body_when_no_raw_session_log():
    # A snapshot missing "Raw Session Log" falls back to "SNAPSHOT BODY".
    no_rsl = FULL_SNAPSHOT.replace("Raw Session Log", "Session Transcript")
    result = extract_snapshot_content(no_rsl)
    assert result.startswith("SNAPSHOT BODY")
    # Fallback still drops the worst offenders above SNAPSHOT BODY.
    assert "CROSS-FILE BEACON" not in result
    assert "VOLUME TRACKER" not in result
    assert "GAUGES" not in result
    # But keeps the Kernel Index / content below SNAPSHOT BODY.
    assert "Kernel Index" in result
    assert "Session Transcript" in result


def test_neither_marker_returns_full_text_unchanged():
    plain = "just some arbitrary text\nwith no snapshot markers at all\n"
    assert extract_snapshot_content(plain) == plain


def test_empty_input_returns_empty():
    assert extract_snapshot_content("") == ""


def test_first_occurrence_of_raw_session_log_is_used():
    # "Raw Session Log" as the header, then referenced again inside a later
    # turn. First-occurrence handling must cut at the HEADER so the turns
    # between the two occurrences survive (a last-occurrence cut would drop
    # them).
    text = (
        "GAUGES\nOPERATOR: Anna\n\n"
        "SNAPSHOT BODY\n\n"
        "Raw Session Log\n"
        "- [1] user: earlier turn before the reference\n"
        "- [2] assistant: see the Raw Session Log format for details\n"
    )
    result = extract_snapshot_content(text)
    assert result.startswith("Raw Session Log")
    assert "earlier turn before the reference" in result
    assert "GAUGES" not in result
    # Both occurrences of the marker survive (cut was at the first).
    assert result.count("Raw Session Log") == 2


def test_snapshot_body_appears_only_once_even_if_marker_repeats():
    # SNAPSHOT BODY fallback also uses the FIRST occurrence.
    text = (
        "GAUGES\nMODEL: x\n\n"
        "SNAPSHOT BODY\n"
        "content mentioning SNAPSHOT BODY again downstream\n"
    )
    result = extract_snapshot_content(text)
    assert result.startswith("SNAPSHOT BODY")
    assert "content mentioning SNAPSHOT BODY again downstream" in result
    assert "GAUGES" not in result


def test_never_raises_on_non_string_like_input():
    # Contract: never raises. None is falsy -> returned as-is (empty-in path).
    assert extract_snapshot_content(None) is None


def test_marker_line_with_leading_whitespace_snaps_to_line_start():
    # Robustness: if the marker line carries indentation, the cut snaps to the
    # start of that line (drops the envelope above it).
    text = "CROSS-FILE BEACON\nnoise\n   Raw Session Log\n- [1] user: hi\n"
    result = extract_snapshot_content(text)
    assert "CROSS-FILE BEACON" not in result
    assert "Raw Session Log" in result
    assert "- [1] user: hi" in result


def test_real_decoded_snapshot_drops_envelope_keeps_turns():
    # End-to-end against a real decoded snapshot from the live volume (skipped
    # on a fresh box with no index / no snapshots).
    import pytest

    from Orchestrator.fossils import load_snapshot_index

    try:
        from Orchestrator.retrieval import _decode_snapshot_text
    except Exception:  # pragma: no cover - retrieval import guard
        pytest.skip("retrieval module unavailable")

    idx = load_snapshot_index()
    if not idx:
        pytest.skip("no snapshot index on this box")
    # Newest snapshot with a decodable body.
    text = None
    for sid in sorted(idx.keys(), reverse=True):
        text = _decode_snapshot_text(idx[sid])
        if text:
            break
    if not text or "Raw Session Log" not in text:
        pytest.skip("no decodable snapshot with the Raw Session Log marker")

    result = extract_snapshot_content(text)
    assert result.startswith("Raw Session Log")
    assert "CROSS-FILE BEACON" not in result
    assert "VOLUME TRACKER" not in result
    assert len(result) < len(text)  # envelope actually stripped
