"""window_snapshot_text re-derives against the store's content_mode (M14.3c).

The on-device windower maps a stored best_ordinal back to a char span by
RE-DERIVING chunk_snapshot(...) and locating the winning chunk. The stored
ordinals came from chunks_for_snapshot(text, mode), so the windower MUST
re-chunk the same text: full-mode → chunk the whole text; body-mode → chunk
extract_snapshot_content(text) (the body). Otherwise every ordinal shifts by
the envelope chunk count and best_ordinal silently points at the wrong passage.

Span-location + windowing stay on the FULL text (the body is a suffix, so
text.find(body_chunk) still locates it and the START/END markers are
preserved). content_mode is an injectable param (None → resolve from the
active store) so these tests need no live store.

Hermetic: pure in-process, model_key=None (floor tokenizer), zero network.
"""
from Orchestrator.embeddings.chunker import chunk_snapshot
from Orchestrator.fossils import extract_snapshot_content, window_snapshot_text

_ENVELOPE = (
    "=== START SNAPSHOT — UTC 2026-07-04T22:37:21Z — SNAP-20260704-7980 (7.1.0) ===\n"
    "CROSS-FILE BEACON\n"
    "Tail lock confirmed\n"
    "VOLUME TRACKER\nTail: SNAP-20260704-7979\n"
    "GAUGES\nOPERATOR: Anna\n\n"
    "SNAPSHOT BODY\n\nKernel Index\n- Current: SNAP-20260704-7980\n\n"
    "Raw Session Log\n"
)


def _snapshot(n_lines: int = 400) -> str:
    body = "\n".join(
        f"- [{i}] user: line {i:05d} " + "abcdefghij" * 3 for i in range(n_lines)
    )
    return _ENVELOPE + body + "\n=== END SNAPSHOT — SNAP-20260704-7980 ===\n"


def _mid_line(chunk: str) -> str:
    lines = [ln for ln in chunk.splitlines() if ln.strip()]
    return lines[len(lines) // 2]


BUDGET = 3000


def test_body_mode_windows_the_correct_body_chunk_start_preserved():
    text = _snapshot()
    body = extract_snapshot_content(text)
    body_chunks = chunk_snapshot(body, model_key=None)
    assert len(body_chunks) > 1  # sanity: body must multi-chunk
    i = len(body_chunks) // 2
    best_ordinal = i + 1  # group ordinal i+1 == chunker chunk i

    result = window_snapshot_text(
        text, best_ordinal, BUDGET, model_key=None, content_mode="body"
    )

    assert len(result) <= BUDGET
    # The START marker line is always preserved (provenance parsing).
    assert result.splitlines()[0].startswith("=== START SNAPSHOT")
    # The window centers on body_chunks[i] — a line from its middle is present.
    assert _mid_line(body_chunks[i]) in result


def test_body_mode_differs_from_full_mode_for_same_ordinal():
    """Proof the mode actually re-anchors: full-mode chunking of the SAME text
    yields a DIFFERENT chunk at the same ordinal (the envelope shifts spans),
    so the windows differ."""
    text = _snapshot()
    body = extract_snapshot_content(text)
    body_chunks = chunk_snapshot(body, model_key=None)
    full_chunks = chunk_snapshot(text, model_key=None)
    i = len(body_chunks) // 2
    best_ordinal = i + 1

    body_win = window_snapshot_text(
        text, best_ordinal, BUDGET, model_key=None, content_mode="body"
    )
    full_win = window_snapshot_text(
        text, best_ordinal, BUDGET, model_key=None, content_mode="full"
    )
    # full-mode windows full_chunks[i]; its middle line is present there.
    assert _mid_line(full_chunks[i]) in full_win
    # The two mode's windows are genuinely different spans.
    assert body_win != full_win


def test_full_mode_is_unchanged_behavior():
    """content_mode="full" is byte-identical to the previous windower (which
    always chunked the whole text) — the safety property before 14.4 cutover."""
    text = _snapshot()
    full_chunks = chunk_snapshot(text, model_key=None)
    i = len(full_chunks) // 2
    best_ordinal = i + 1

    explicit_full = window_snapshot_text(
        text, best_ordinal, BUDGET, model_key=None, content_mode="full"
    )
    # No content_mode + no resolvable store must ALSO default to full behavior.
    # (extract via the resolver — with the real box store it's full; here we
    # assert the explicit-full path against the mid-line contract.)
    assert _mid_line(full_chunks[i]) in explicit_full
    assert explicit_full.splitlines()[0].startswith("=== START SNAPSHOT")


def test_store_unavailable_defaults_full_never_raises(monkeypatch):
    """content_mode=None resolves from the active store; if that raises, the
    windower degrades to full-mode (never raises)."""
    from Orchestrator.embeddings import search as search_mod

    def _boom():
        raise ValueError("simulated corrupt store")

    monkeypatch.setattr(search_mod, "get_active_store", _boom)

    text = _snapshot()
    full_chunks = chunk_snapshot(text, model_key=None)
    i = len(full_chunks) // 2
    resolved = window_snapshot_text(text, i + 1, BUDGET, model_key=None)
    explicit_full = window_snapshot_text(
        text, i + 1, BUDGET, model_key=None, content_mode="full"
    )
    assert resolved == explicit_full


def test_ordinal_zero_and_short_text_unchanged():
    # best_ordinal 0 / None -> head truncation regardless of mode; short text
    # (<= budget) returned whole.
    text = _snapshot()
    assert window_snapshot_text(text, 0, BUDGET, content_mode="body").startswith(
        "=== START SNAPSHOT"
    )
    short = _ENVELOPE + "- [1] tiny\n"
    assert window_snapshot_text(short, 3, 99999, content_mode="body") == short
