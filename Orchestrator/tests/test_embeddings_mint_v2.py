"""Mint-path chunking + schema guard at the vector seam (M6 task 6c).

Per docs/plans/2026-07-01-retrieval-upgrade-implementation.md Task 6c /
audit amendment A4: the three checkpoint.py mint sites call
search.embed_snapshot_for_index(body), which shapes the payload to the
ACTIVE store's schema — v1 → {"embedding": vec} (today's exact single
whole-body vector), v2 → {"chunk_vectors": [v0..vn]} (chunk_snapshot
windows, ONE provider.embed call, document order). It NEVER raises: any
failure → {} and the mint completes vector-less (catch-up re-embeds).

fossils.update_snapshot_index re-validates the payload shape against the
store's CURRENT schema AT APPEND TIME — the A4 guard covers the cutover
race in BOTH directions: a bare whole-snapshot vector arriving at a
chunked (v2) store is DROPPED (it is not a 1-chunk group), and chunk
vectors arriving at a v1 store are DROPPED (chunk_vectors[0] is not a
whole-snapshot embedding). Either way the snap_id stays missing() so the
migrate diff / watcher gap-heal re-embeds it correctly.

ALL tests run against tmp_path fixtures + fake providers — never the real
Manifest/, zero network. Live production stays v1 until M6f: the v1 tests
here pin bit-identical behavior to today.
"""
import json

import numpy as np
import pytest

from Orchestrator import config, fossils
from Orchestrator.embeddings import providers, search as search_mod
from Orchestrator.embeddings.chunker import chunk_snapshot
from Orchestrator.embeddings.providers import EmbeddingProviderError
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import get_store, set_active_slug

SLUG = "gemini-embedding-001"
DIMS = EMBEDDING_MODELS[SLUG]["dims"]
TS = "2026-07-02T00:00:00Z"


def _basis(i: int) -> list[float]:
    """Distinct unit vector per index — position i survives L2-normalization,
    so row↔chunk alignment is verifiable straight from vectors.f32."""
    vec = [0.0] * DIMS
    vec[i] = 1.0
    return vec


def _long_text(n_lines: int = 400) -> str:
    """Guaranteed multi-chunk under every tokenizer backend (same recipe as
    test_chunker.py's _long_text)."""
    return "\n".join(f"line {i:05d} " + "abcdefghij" * 3 for i in range(n_lines))


class SeqProvider:
    """Fake provider: text j in a call gets basis vector j (alignment probe)."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = []  # (texts, purpose) per embed() call

    async def embed(self, texts, purpose):
        self.calls.append((list(texts), purpose))
        if self.fail:
            raise EmbeddingProviderError("fake provider: down")
        return [_basis(j) for j in range(len(texts))]


class MidFailProvider(SeqProvider):
    """Fails partway through a multi-chunk embed (provider raises mid-batch)."""

    async def embed(self, texts, purpose):
        self.calls.append((list(texts), purpose))
        raise EmbeddingProviderError("fake provider: died after chunk 1 of N")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated index + stores + provider cache (test_embeddings_mint.py's
    fixture pattern): fossils.SNAPSHOT_INDEX is bound at import time so it is
    patched on the fossils module with its mtime cache; the cached
    search._active_store is dropped so get_active_store() reopens under the
    patched dir."""
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


def _activate_v2(stores_dir):
    """Make the active store schema-2 (M6f cutover state, pre-cutover opt-in)."""
    return get_store(SLUG, base_dir=stores_dir, schema=2)


def _install(fake):
    providers._instances[SLUG] = fake
    return fake


def _mint(snap_id, **kwargs):
    fossils.update_snapshot_index(snap_id, 1000, 1999, "Brandon", TS, **kwargs)


def _entry(index_path, snap_id):
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert snap_id in index
    return index[snap_id]


def _rows_matrix(stores_dir):
    return np.fromfile(
        stores_dir / SLUG / "vectors.f32", dtype="<f4"
    ).reshape(-1, DIMS)


# ── embed_snapshot_for_index: schema-shaped payloads ─────────────────────────

def test_v1_active_returns_single_embedding_document_purpose(env):
    """v1 active → whole (clamped) body embedded ONCE — today's exact shape."""
    fake = _install(SeqProvider())

    payload = search_mod.embed_snapshot_for_index("short snapshot body")

    assert list(payload) == ["embedding"]
    assert payload["embedding"] == _basis(0)
    assert fake.calls == [(["short snapshot body"], "document")]


def test_v2_active_returns_aligned_chunk_vectors_one_provider_call(env):
    _, stores_dir = env
    _activate_v2(stores_dir)
    fake = _install(SeqProvider())
    text = _long_text()
    chunks = chunk_snapshot(text, model_key=SLUG)
    assert len(chunks) > 1  # sanity: the fixture text must multi-chunk

    payload = search_mod.embed_snapshot_for_index(text)

    assert list(payload) == ["chunk_vectors"]
    vectors = payload["chunk_vectors"]
    # M6f iteration 2: multi-chunk group = whole-doc vector + n chunks
    assert len(vectors) == len(chunks) + 1
    # ONE provider.embed call: the WHOLE body FIRST (ordinal 0), then the
    # chunks in document order — the first embedded text must be the whole
    # body, never a chunk (the provider's M5 clamp bounds its length).
    assert fake.calls == [([text] + chunks, "document")]
    assert fake.calls[0][0][0] == text
    # vector i belongs to text i (SeqProvider tags by position): vector 0 is
    # the whole-doc embed, vector i+1 is chunk i
    for i, vec in enumerate(vectors):
        assert vec == _basis(i)


def test_v2_active_short_text_single_chunk_group_of_one(env):
    _, stores_dir = env
    _activate_v2(stores_dir)
    fake = _install(SeqProvider())

    payload = search_mod.embed_snapshot_for_index("short snapshot body")

    assert payload == {"chunk_vectors": [_basis(0)]}
    assert fake.calls == [(["short snapshot body"], "document")]


def test_v2_multichunk_mint_lands_whole_doc_vector_at_ordinal_zero(env):
    """M6f iteration 2 end-to-end: a minted multi-chunk snapshot's group has
    n+1 rows and row 0 (ordinal 0) is the WHOLE-document vector — the store's
    max-cosine collapse then scores max(whole, chunks)."""
    _, stores_dir = env
    store = _activate_v2(stores_dir)
    fake = _install(SeqProvider())
    text = _long_text()
    chunks = chunk_snapshot(text, model_key=SLUG)
    assert len(chunks) > 1
    snap_id = "SNAP-20260702-0042"

    payload = search_mod.embed_snapshot_for_index(text)
    _mint(snap_id, **payload)

    assert store.rows == len(chunks) + 1
    ordinals = json.loads(
        (stores_dir / SLUG / "ordinals.json").read_text(encoding="utf-8")
    )
    assert ordinals == list(range(len(chunks) + 1))
    # SeqProvider tags by position: text 0 was the whole body, so the row at
    # ordinal 0 must be its vector (basis 0) — a whole-doc query tops it
    matrix = _rows_matrix(stores_dir)
    assert int(np.argmax(matrix[0])) == 0
    assert fake.calls[0][0][0] == text  # first embedded text = whole body
    hits = store.search(_basis(0), k=1)
    assert hits == [(snap_id, pytest.approx(1.0, abs=1e-5))]


def test_provider_failure_v1_returns_empty_dict_never_raises(env, capsys):
    """Provider down on a v1 store → {} + [EMBEDDING] log, no exception."""
    _install(SeqProvider(fail=True))
    assert search_mod.embed_snapshot_for_index("body") == {}
    assert "[EMBEDDING]" in capsys.readouterr().out


def test_provider_failure_v2_returns_empty_dict_never_raises(env, capsys):
    """Provider down on a v2 store → {} + [EMBEDDING] log, no exception."""
    _, stores_dir = env
    _activate_v2(stores_dir)
    _install(SeqProvider(fail=True))
    assert search_mod.embed_snapshot_for_index(_long_text()) == {}
    assert "[EMBEDDING]" in capsys.readouterr().out


def test_provider_failure_mid_chunks_returns_empty_dict(env, capsys):
    """A mid-batch provider death must yield {} (never a partial vector list)."""
    _, stores_dir = env
    _activate_v2(stores_dir)
    _install(MidFailProvider())

    assert search_mod.embed_snapshot_for_index(_long_text()) == {}
    assert "[EMBEDDING]" in capsys.readouterr().out


def test_store_unavailable_returns_empty_dict(env, monkeypatch, capsys):
    def _boom():
        raise ValueError("simulated corrupt store")

    monkeypatch.setattr(search_mod, "get_active_store", _boom)

    assert search_mod.embed_snapshot_for_index("body") == {}
    assert "[EMBEDDING]" in capsys.readouterr().out


def test_generate_embedding_sync_untouched_by_chunking(env):
    """Audit A7 ratchet: the single-vector path other callers (ToolVault,
    watcher probe, queries) rely on must NEVER chunk — one text in, one
    provider text out, even with a v2 store active."""
    _, stores_dir = env
    _activate_v2(stores_dir)
    fake = _install(SeqProvider())
    text = _long_text()

    vec = search_mod.generate_embedding_sync(text, purpose="document")

    assert vec == _basis(0)
    assert len(fake.calls) == 1
    assert len(fake.calls[0][0]) == 1  # exactly one text — never chunked


# ── update_snapshot_index: v2 chunk groups land whole ────────────────────────

def test_mint_v2_lands_full_contiguous_group_in_document_order(env):
    index_path, stores_dir = env
    store = _activate_v2(stores_dir)
    snap_id = "SNAP-20260702-0001"
    vectors = [_basis(0), _basis(1), _basis(2)]

    _mint(snap_id, chunk_vectors=vectors)

    assert store.ids() == {snap_id}
    assert store.rows == 3
    ordinals = json.loads(
        (stores_dir / SLUG / "ordinals.json").read_text(encoding="utf-8")
    )
    assert ordinals == [0, 1, 2]
    matrix = _rows_matrix(stores_dir)
    for row in range(3):  # row j is chunk j — document order preserved
        assert int(np.argmax(matrix[row])) == row
    assert store.missing([snap_id]) == []  # snapshot-currency: fully present

    entry = _entry(index_path, snap_id)
    assert "embedding" not in entry and "chunk_vectors" not in entry  # slim


def test_mint_v2_end_to_end_search_returns_snapshot_once(env):
    """A minted multi-chunk snapshot is findable and collapses to ONE hit."""
    _, stores_dir = env
    store = _activate_v2(stores_dir)
    snap_id = "SNAP-20260702-0002"

    _mint(snap_id, chunk_vectors=[_basis(0), _basis(1)])

    hits = store.search(_basis(1), k=5)
    assert hits == [(snap_id, pytest.approx(1.0, abs=1e-5))]


# ── update_snapshot_index: v1 stays bit-identical to today ──────────────────

def test_mint_v1_single_row_exact_todays_append_path(env, capsys):
    index_path, stores_dir = env
    snap_id = "SNAP-20260702-0003"
    emb = _basis(7)

    _mint(snap_id, embedding=emb)

    store = get_store(SLUG, base_dir=stores_dir)
    assert store.schema == 1
    assert store.ids() == {snap_id}
    assert store.rows == 1
    # v1 stores never grow an ordinals sidecar
    assert not (stores_dir / SLUG / "ordinals.json").exists()
    top = store.search(emb, k=1)
    assert top[0][0] == snap_id
    assert top[0][1] == pytest.approx(1.0, abs=1e-5)
    # the exact single-append log line — pins that today's code path ran
    out = capsys.readouterr().out
    assert f"Stored embedding for {snap_id} in {SLUG} store ({DIMS} dimensions)" in out

    entry = _entry(index_path, snap_id)
    assert "embedding" not in entry and "chunk_vectors" not in entry


# ── the A4 schema guard, both directions ─────────────────────────────────────

def test_bare_embedding_into_v2_store_dropped_logged_still_missing(env, capsys):
    """The exact failure A4 exists to prevent: a whole-snapshot vector must
    never masquerade as a 1-chunk group in a chunked store."""
    index_path, stores_dir = env
    store = _activate_v2(stores_dir)
    snap_id = "SNAP-20260702-0004"

    _mint(snap_id, embedding=_basis(0))

    assert store.rows == 0
    assert store.missing([snap_id]) == [snap_id]  # catch-up will re-embed
    out = capsys.readouterr().out
    assert (
        f"[EMBEDDING] {snap_id}: whole-snapshot vector vs chunked store"
        " — dropped; catch-up re-embeds" in out
    )
    entry = _entry(index_path, snap_id)  # the mint itself still landed
    assert "embedding" not in entry


def test_chunk_vectors_into_v1_store_dropped_logged_still_missing(env, capsys):
    """Mirror direction: chunk_vectors[0] is NOT a whole-snapshot embedding —
    dropping beats silently appending a wrong-semantics row."""
    index_path, stores_dir = env
    snap_id = "SNAP-20260702-0005"

    _mint(snap_id, chunk_vectors=[_basis(0), _basis(1)])

    store = get_store(SLUG, base_dir=stores_dir)
    assert store.schema == 1
    assert store.rows == 0
    assert store.missing([snap_id]) == [snap_id]
    out = capsys.readouterr().out
    assert (
        f"[EMBEDDING] {snap_id}: chunk vectors vs whole-snapshot store"
        " — dropped; catch-up re-embeds" in out
    )
    entry = _entry(index_path, snap_id)
    assert "chunk_vectors" not in entry


# ── never-raise guarantees on the new group path ─────────────────────────────

def test_wrong_dims_chunk_drops_whole_group_with_race_log(env, capsys):
    """Cutover race: chunks embedded under the old model, store swapped before
    the index update. All-or-nothing — never a partial group."""
    index_path, stores_dir = env
    store = _activate_v2(stores_dir)
    snap_id = "SNAP-20260702-0006"

    _mint(snap_id, chunk_vectors=[_basis(0), [0.5] * 768])

    assert store.rows == 0
    assert store.missing([snap_id]) == [snap_id]
    out = capsys.readouterr().out
    assert "minted without vector" in out
    assert "768" in out and str(DIMS) in out
    _entry(index_path, snap_id)  # entry still written


def test_append_group_raising_is_caught_mint_completes(env, capsys):
    """Non-finite chunk → append_group raises internally; the mint logs and
    completes vector-less."""
    index_path, stores_dir = env
    store = _activate_v2(stores_dir)
    snap_id = "SNAP-20260702-0007"
    bad = _basis(1)
    bad[7] = float("nan")

    _mint(snap_id, chunk_vectors=[_basis(0), bad])

    assert store.rows == 0
    out = capsys.readouterr().out
    assert "minted without vector" in out
    _entry(index_path, snap_id)


def test_store_unavailable_with_chunk_vectors_entry_still_written(
    env, monkeypatch, capsys
):
    index_path, _ = env
    snap_id = "SNAP-20260702-0008"

    def _boom():
        raise ValueError("simulated corrupt store")

    monkeypatch.setattr(search_mod, "get_active_store", _boom)

    _mint(snap_id, chunk_vectors=[_basis(0)])

    out = capsys.readouterr().out
    assert "active store unavailable" in out
    _entry(index_path, snap_id)


# ── checkpoint mint sites drive the new helper end-to-end ────────────────────

@pytest.fixture
def mint_site(env, monkeypatch, tmp_path):
    """mint_with_content with its volume I/O stubbed: the REAL
    embed_snapshot_for_index + REAL fossils.update_snapshot_index run against
    the fixture store; only the ledger plumbing is faked."""
    from Orchestrator import checkpoint

    monkeypatch.setattr(checkpoint, "verify_gm_or_halt", lambda: None)
    monkeypatch.setattr(checkpoint, "read_text_safe", lambda _p: "")
    monkeypatch.setattr(
        checkpoint, "parse_tail", lambda _txt: {"tail_id": "SNAP-20260702-0100"}
    )
    monkeypatch.setattr(
        checkpoint, "next_snap_id_from_tail", lambda _tid: "SNAP-20260702-0101"
    )
    monkeypatch.setattr(checkpoint, "append_snapshot_text", lambda _b: None)
    monkeypatch.setattr(checkpoint, "VOL_PATH", tmp_path / "nonexistent-vol.txt")
    monkeypatch.setattr(
        checkpoint, "archive_volume", lambda: ("/tmp/arc.txt", "deadbeef", TS)
    )
    return checkpoint


def test_mint_with_content_v2_store_gets_full_chunk_group(env, mint_site, monkeypatch):
    index_path, stores_dir = env
    store = _activate_v2(stores_dir)
    fake = _install(SeqProvider())
    body = _long_text()
    monkeypatch.setattr(
        mint_site, "render_snapshot_body_v71", lambda *a, **k: body
    )
    expected_chunks = chunk_snapshot(body, model_key=SLUG)
    assert len(expected_chunks) > 1

    result = mint_site.mint_with_content("Brandon", "ignored", reason="TEST")

    assert result["snap_id"] == "SNAP-20260702-0101"
    # M6f iteration 2: the whole body leads the provider call (ordinal 0)
    assert fake.calls == [([body] + expected_chunks, "document")]
    assert store.ids() == {"SNAP-20260702-0101"}
    assert store.rows == len(expected_chunks) + 1
    ordinals = json.loads(
        (stores_dir / SLUG / "ordinals.json").read_text(encoding="utf-8")
    )
    assert ordinals == list(range(len(expected_chunks) + 1))
    entry = _entry(index_path, "SNAP-20260702-0101")
    assert "embedding" not in entry and "chunk_vectors" not in entry


def test_mint_with_content_v1_store_gets_single_vector(
    env, mint_site, monkeypatch, capsys
):
    index_path, stores_dir = env
    fake = _install(SeqProvider())
    body = "a short snapshot body"
    monkeypatch.setattr(
        mint_site, "render_snapshot_body_v71", lambda *a, **k: body
    )

    result = mint_site.mint_with_content("Brandon", "ignored", reason="TEST")

    store = get_store(SLUG, base_dir=stores_dir)
    assert store.schema == 1
    assert fake.calls == [([body], "document")]
    assert store.ids() == {"SNAP-20260702-0101"}
    assert store.rows == 1
    # journalctl-verifiable success line (CLAUDE.md mint-verification contract)
    out = capsys.readouterr().out
    assert f"Successfully generated embedding ({DIMS} dimensions)" in out
    _entry(index_path, result["snap_id"])


def test_mint_with_content_provider_down_mint_still_completes(
    env, mint_site, monkeypatch, capsys
):
    index_path, stores_dir = env
    _activate_v2(stores_dir)
    _install(SeqProvider(fail=True))
    monkeypatch.setattr(
        mint_site, "render_snapshot_body_v71", lambda *a, **k: "body text"
    )

    result = mint_site.mint_with_content("Brandon", "ignored", reason="TEST")

    assert result["snap_id"] == "SNAP-20260702-0101"  # mint never fails on vectors
    store = get_store(SLUG, base_dir=stores_dir)
    assert store.rows == 0
    out = capsys.readouterr().out
    assert "Warning: Failed to generate embedding" in out
    _entry(index_path, result["snap_id"])
