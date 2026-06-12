"""Pluggable embeddings — mint path writes to the active store (Task 6).

Per docs/plans/2026-06-11-pluggable-embeddings.md Task 6:
fossils.update_snapshot_index is the SINGLE vector write seam (all three
checkpoint.py mint sites pass embedding= through it). With a vector it must
append to the active VectorStore and keep the JSON entry slim (no "embedding"
key — ever). A mint must NEVER fail because of the vector layer: corrupt
store, dims mismatch (cutover race) and non-finite vectors are logged and
dropped (the migration job's catch-up loop re-embeds them later).

ALL tests run against tmp_path fixtures — never the real Manifest/.
"""
import json

import numpy as np
import pytest

from Orchestrator import config, fossils
from Orchestrator.embeddings import search as search_mod
from Orchestrator.embeddings.store import get_store, set_active_slug

DIMS = 3072
SLUG = "gemini-embedding-001"


def _vec(seed=0):
    rng = np.random.default_rng(seed)
    return [float(x) for x in rng.standard_normal(DIMS)]


@pytest.fixture
def mint_env(tmp_path, monkeypatch):
    """Isolated index + stores: fossils.SNAPSHOT_INDEX is bound at import time
    (from Orchestrator.config import SNAPSHOT_INDEX) so it must be patched on
    the fossils module itself, alongside its mtime cache globals. The store
    side reads config.EMBEDDINGS_STORES_DIR dynamically; the cached
    search._active_store must be dropped so get_active_store() reopens under
    the patched dir."""
    index_path = tmp_path / "snapshot_index.json"
    stores_dir = tmp_path / "embeddings"
    monkeypatch.setattr(fossils, "SNAPSHOT_INDEX", index_path)
    monkeypatch.setattr(fossils, "_index_cache", None)
    monkeypatch.setattr(fossils, "_index_cache_mtime", 0.0)
    monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(stores_dir))
    monkeypatch.setattr(search_mod, "_active_store", None)
    set_active_slug(SLUG, base_dir=stores_dir)
    return index_path, stores_dir


def _mint(snap_id, **kwargs):
    fossils.update_snapshot_index(
        snap_id, 1000, 1999, "Brandon", "2026-06-11T00:00:00Z", **kwargs
    )


def _load_entry(index_path, snap_id):
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert snap_id in index
    return index[snap_id]


# ── happy path ───────────────────────────────────────────────────────────────

def test_mint_with_vector_appends_to_store_index_stays_slim(mint_env):
    index_path, stores_dir = mint_env
    snap_id = "SNAP-20260611-0001"
    emb = _vec(seed=1)

    _mint(snap_id, embedding=emb)

    store = get_store(SLUG, base_dir=stores_dir)
    assert store.ids() == {snap_id}
    # the stored row IS the minted vector: self-similarity scores ~1.0
    top = store.search(emb, k=1)
    assert top[0][0] == snap_id
    assert top[0][1] == pytest.approx(1.0, abs=1e-5)

    entry = _load_entry(index_path, snap_id)
    assert entry == {
        "byte_start": 1000,
        "byte_end": 1999,
        "operator": "Brandon",
        "timestamp": "2026-06-11T00:00:00Z",
        "type": "normal",
    }
    assert "embedding" not in entry  # slim forever


def test_mint_with_none_embedding_writes_entry_store_untouched(mint_env):
    index_path, stores_dir = mint_env
    snap_id = "SNAP-20260611-0002"

    _mint(snap_id, embedding=None)

    entry = _load_entry(index_path, snap_id)
    assert "embedding" not in entry
    assert entry["byte_start"] == 1000
    assert get_store(SLUG, base_dir=stores_dir).count == 0


# ── guard rails: a mint must NEVER fail because of the vector layer ──────────

def test_wrong_dims_vector_dropped_with_log(mint_env, capsys):
    """Cutover race: embedding generated under the old model, store swapped
    before the index update. Entry still lands; vector dropped + logged."""
    index_path, stores_dir = mint_env
    snap_id = "SNAP-20260611-0003"

    _mint(snap_id, embedding=[0.5] * 768)

    entry = _load_entry(index_path, snap_id)
    assert "embedding" not in entry
    assert get_store(SLUG, base_dir=stores_dir).count == 0
    out = capsys.readouterr().out
    assert f"minted without vector" in out
    assert "768" in out and str(DIMS) in out


def test_store_unavailable_entry_still_written(mint_env, monkeypatch, capsys):
    """get_active_store() raising (corrupt store dir, dims-mismatched meta)
    must not break the mint."""
    index_path, _ = mint_env
    snap_id = "SNAP-20260611-0004"

    def _boom():
        raise ValueError("simulated corrupt store")

    monkeypatch.setattr(search_mod, "get_active_store", _boom)

    _mint(snap_id, embedding=_vec(seed=4))

    entry = _load_entry(index_path, snap_id)
    assert "embedding" not in entry
    out = capsys.readouterr().out
    assert "active store unavailable" in out
    assert snap_id in out and "simulated corrupt store" in out


def test_non_finite_vector_dropped_entry_still_written(mint_env, capsys):
    """Correct dims but NaN inside: store.append raises internally; the mint
    catches, logs and continues without the vector."""
    index_path, stores_dir = mint_env
    snap_id = "SNAP-20260611-0005"
    emb = _vec(seed=5)
    emb[7] = float("nan")

    _mint(snap_id, embedding=emb)

    entry = _load_entry(index_path, snap_id)
    assert "embedding" not in entry
    assert get_store(SLUG, base_dir=stores_dir).count == 0
    out = capsys.readouterr().out
    assert "minted without vector" in out
    assert "non-finite" in out


# ── existing behavior preserved ──────────────────────────────────────────────

def test_media_artifacts_passthrough_alongside_vector(mint_env):
    index_path, stores_dir = mint_env
    snap_id = "SNAP-20260611-0006"
    artifacts = [{
        "type": "image",
        "url": "/ui/uploads/sunset_abc123.png",
        "task_id": "task-42",
        "prompt": "a sunset",
        "model": "imagen",
    }]

    _mint(snap_id, embedding=_vec(seed=6), media_artifacts=artifacts)

    entry = _load_entry(index_path, snap_id)
    assert entry["media_artifacts"] == artifacts
    assert "embedding" not in entry
    assert get_store(SLUG, base_dir=stores_dir).ids() == {snap_id}
