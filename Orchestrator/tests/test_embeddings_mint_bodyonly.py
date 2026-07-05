"""Mint path chunks BODY-ONLY when the active store is content_mode="body" (M14.3b).

When the active v2 store is body-mode, embed_snapshot_for_index must chunk
extract_snapshot_content(text) (the Raw-Session-Log body) — for BOTH the
chunk windows AND the ordinal-0 whole-doc vector — so every stored vector
scores clean content, never the near-identical bookkeeping envelope. A
"full"-mode store is byte-identical to today (the safety property that lets
this code land before the 14.4 data cutover).

Mint (here) and migrate (test_embeddings_migrate_bodyonly) share ONE helper —
chunker.chunks_for_snapshot — so new-mint and re-embedded chunks can never
diverge. Hermetic: tmp_path stores + fake provider, zero network.
"""
import numpy as np
import pytest

from Orchestrator import config, fossils
from Orchestrator.embeddings import providers, search as search_mod
from Orchestrator.embeddings.chunker import chunk_snapshot
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import get_store, set_active_slug

SLUG = "gemini-embedding-001"
DIMS = EMBEDDING_MODELS[SLUG]["dims"]

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


def _basis(i: int) -> list[float]:
    vec = [0.0] * DIMS
    vec[i] = 1.0
    return vec


class SeqProvider:
    def __init__(self):
        self.calls = []  # (texts, purpose) per embed() call

    async def embed(self, texts, purpose):
        self.calls.append((list(texts), purpose))
        return [_basis(j) for j in range(len(texts))]


@pytest.fixture
def env(tmp_path, monkeypatch):
    index_path = tmp_path / "snapshot_index.json"
    stores_dir = tmp_path / "embeddings"
    monkeypatch.setattr(fossils, "SNAPSHOT_INDEX", index_path)
    monkeypatch.setattr(fossils, "_index_cache", None)
    monkeypatch.setattr(fossils, "_index_cache_mtime", 0.0)
    monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(stores_dir))
    monkeypatch.setattr(search_mod, "_active_store", None)
    providers._instances.clear()
    set_active_slug(SLUG, base_dir=stores_dir)
    yield index_path, stores_dir
    providers._instances.clear()
    search_mod._active_store = None


def _install(fake):
    providers._instances[SLUG] = fake
    return fake


def test_body_mode_chunks_drop_the_envelope_including_ordinal_zero(env):
    _, stores_dir = env
    get_store(SLUG, base_dir=stores_dir, schema=2, content_mode="body")
    fake = _install(SeqProvider())
    text = _snapshot()
    body = fossils.extract_snapshot_content(text)
    expected_chunks = chunk_snapshot(body, model_key=SLUG)
    assert len(expected_chunks) > 1  # sanity: the body must multi-chunk

    payload = search_mod.embed_snapshot_for_index(text)

    assert list(payload) == ["chunk_vectors"]
    # ONE provider.embed call; ordinal 0 is the BODY (not the full text).
    assert len(fake.calls) == 1
    embedded, purpose = fake.calls[0]
    assert purpose == "document"
    assert embedded == [body] + expected_chunks
    assert embedded[0] == body
    assert embedded[0] != text
    # The bookkeeping envelope head is absent from EVERY embedded text.
    for chunk in embedded:
        assert "CROSS-FILE BEACON" not in chunk
        assert "START SNAPSHOT" not in chunk
        assert "VOLUME TRACKER" not in chunk
        assert "GAUGES" not in chunk


def test_full_mode_still_passes_the_whole_envelope_text(env):
    """Safety property: a "full"-mode store is byte-identical to today —
    ordinal 0 is the WHOLE text, envelope included."""
    _, stores_dir = env
    get_store(SLUG, base_dir=stores_dir, schema=2)  # default content_mode="full"
    fake = _install(SeqProvider())
    text = _snapshot()
    expected_chunks = chunk_snapshot(text, model_key=SLUG)

    payload = search_mod.embed_snapshot_for_index(text)

    assert list(payload) == ["chunk_vectors"]
    embedded, _ = fake.calls[0]
    assert embedded == [text] + expected_chunks
    assert embedded[0] == text
    assert "CROSS-FILE BEACON" in embedded[0]


def test_body_mode_short_body_single_chunk_no_ordinal_zero_prepend(env):
    """A body that fits one chunk lands as a group of one — the body itself,
    with no whole-doc prepend (matches full-mode single-chunk behavior)."""
    _, stores_dir = env
    get_store(SLUG, base_dir=stores_dir, schema=2, content_mode="body")
    fake = _install(SeqProvider())
    text = _ENVELOPE + "- [1] user: tiny body\n=== END SNAPSHOT — SNAP-X ===\n"
    body = fossils.extract_snapshot_content(text)
    assert len(chunk_snapshot(body, model_key=SLUG)) == 1

    payload = search_mod.embed_snapshot_for_index(text)

    embedded, _ = fake.calls[0]
    assert embedded == [body]
    assert "CROSS-FILE BEACON" not in embedded[0]
    assert payload == {"chunk_vectors": [_basis(0)]}
