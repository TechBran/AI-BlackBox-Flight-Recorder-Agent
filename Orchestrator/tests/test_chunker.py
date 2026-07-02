"""Snapshot chunker tests (M6 task 6b) — hermetic and offline.

model_key backends: None → chars/2 floor; qwen3 slug → vendored exact
tokenizer (degrades to floor if the lib is absent — assertions hold either
way); gemini slug → "remote:gemini" spec has no local backend ⇒ floor,
never network.
"""
import pytest

from Orchestrator import config
from Orchestrator.embeddings.chunker import chunk_snapshot
from Orchestrator.tokenization import estimate_tokens

BUDGET = config.CFG.getint("retrieval", "chunk_tokens", fallback=1024)
OVERLAP_PCT = config.CFG.getint("retrieval", "chunk_overlap_pct", fallback=15)

MODEL_KEYS = [None, "qwen3-embedding-0.6b", "gemini-embedding-2"]


def _long_text(n_lines=400):
    # unique numbered lines: every multi-line chunk is a unique substring, so
    # text.index(chunk) recovers its true position unambiguously
    return "\n".join(f"line {i:05d} " + "abcdefghij" * 3 for i in range(n_lines))


def _positions(text, chunks):
    """True start offset of each chunk (chunks are verbatim substrings)."""
    positions = []
    for chunk in chunks:
        assert chunk in text, "chunk is not a verbatim substring of the input"
        positions.append(text.index(chunk))
    return positions


# ── identity / degenerate inputs ─────────────────────────────────────────────

def test_short_text_identity():
    assert chunk_snapshot("hello world") == ["hello world"]
    for model_key in MODEL_KEYS:
        assert chunk_snapshot("a short snapshot", model_key) == ["a short snapshot"]


def test_empty_returns_empty():
    assert chunk_snapshot("") == []
    assert chunk_snapshot(None) == []


def test_garbage_never_raises():
    assert chunk_snapshot(b"bytes snapshot") == ["bytes snapshot"]
    assert chunk_snapshot(42) == ["42"]
    assert chunk_snapshot(["a", "b"])  # stringified, not raised
    emoji = "🚀🔥" * 2000  # astral chars: slicing/encoding must not raise
    for model_key in MODEL_KEYS:
        chunks = chunk_snapshot(emoji, model_key)
        assert chunks and "".join(dict.fromkeys("".join(chunks))) == "🚀🔥"


# ── budget ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model_key", MODEL_KEYS)
def test_every_chunk_within_budget(model_key):
    text = _long_text()
    chunks = chunk_snapshot(text, model_key)
    assert len(chunks) > 1  # 400 unique lines cannot fit one window
    for chunk in chunks:
        assert estimate_tokens(chunk, model_key) <= BUDGET


# ── overlap / coverage / determinism ─────────────────────────────────────────

@pytest.mark.parametrize("model_key", MODEL_KEYS)
def test_full_coverage_no_gaps(model_key):
    text = _long_text()
    chunks = chunk_snapshot(text, model_key)
    positions = _positions(text, chunks)
    assert positions[0] == 0
    assert positions == sorted(positions)
    for i in range(1, len(chunks)):
        # next chunk starts AT or BEFORE the previous chunk's end — no gaps
        assert positions[i] <= positions[i - 1] + len(chunks[i - 1])
        assert positions[i] > positions[i - 1]  # and always makes progress
    assert positions[-1] + len(chunks[-1]) == len(text)
    # concatenation minus overlaps reconstructs the text exactly
    rebuilt = chunks[0]
    for i in range(1, len(chunks)):
        overlap = positions[i - 1] + len(chunks[i - 1]) - positions[i]
        rebuilt += chunks[i][overlap:]
    assert rebuilt == text


def test_consecutive_chunks_overlap_configured_pct():
    text = _long_text()
    chunks = chunk_snapshot(text, None)
    positions = _positions(text, chunks)
    # every consecutive pair overlaps by ~OVERLAP_PCT of the leading chunk
    # (exactly floor(len*pct/100) by construction; final pair can only be
    # larger when the tail window re-covers earlier text)
    for i in range(len(chunks) - 1):
        overlap = positions[i] + len(chunks[i]) - positions[i + 1]
        frac = overlap / len(chunks[i])
        if i < len(chunks) - 2:
            assert frac == pytest.approx(OVERLAP_PCT / 100, abs=0.02)
        else:
            assert frac >= OVERLAP_PCT / 100 - 0.02


def test_deterministic():
    text = _long_text()
    for model_key in MODEL_KEYS:
        assert chunk_snapshot(text, model_key) == chunk_snapshot(text, model_key)


def test_newline_boundary_preferred():
    # 42-char lines: every floor window's last 10% (~200 chars) contains a
    # newline, so every non-final chunk should break at one
    chunks = chunk_snapshot(_long_text(), None)
    assert len(chunks) > 2
    for chunk in chunks[:-1]:
        assert chunk.endswith("\n")


def test_gemini_slug_uses_floor_path():
    # "remote:gemini" has no local tokenizer backend — identical to the floor
    text = _long_text()
    assert chunk_snapshot(text, "gemini-embedding-2") == chunk_snapshot(text, None)
