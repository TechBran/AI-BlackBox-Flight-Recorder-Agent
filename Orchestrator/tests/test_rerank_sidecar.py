"""rerank.json selection sidecar (Orchestrator/embeddings/store.py, M4 Task 4.1).

Mirror of the placement.json accessors (test_embeddings_placement.py's store
section): a SINGLE selection object {enabled, provider, model} written beside
placement.json / keep_alive.json under EMBEDDINGS_STORES_DIR, fail-open on a
missing/corrupt/non-dict file, atomic write (tmp + os.replace). Unlike
placement (a per-slug map), the whole file IS the selection. All tests run
against tmp_path — never the real Manifest/.
"""
import json

import pytest

from Orchestrator.embeddings import store


@pytest.fixture
def stores_dir(tmp_path):
    return tmp_path / "embeddings"


SELECTION = {"enabled": True, "provider": "voyage", "model": "voyage-rerank-2.5"}


# ── round-trip ────────────────────────────────────────────────────────────────

def test_set_then_get_roundtrip(stores_dir):
    written = store.set_rerank_selection(SELECTION, base_dir=stores_dir)
    assert written == SELECTION
    assert store.get_rerank_selection(base_dir=stores_dir) == SELECTION


def test_the_whole_file_is_the_selection_not_keyed_by_slug(stores_dir):
    """Unlike placement.json (a {slug: value} map), rerank.json's top-level
    object IS the selection — a second write REPLACES it wholesale."""
    store.set_rerank_selection(SELECTION, base_dir=stores_dir)
    replacement = {"enabled": False, "provider": "null", "model": "x"}
    store.set_rerank_selection(replacement, base_dir=stores_dir)
    on_disk = json.loads((stores_dir / store.RERANK_FILE).read_text())
    assert on_disk == replacement
    assert store.get_rerank_selection(base_dir=stores_dir) == replacement


def test_set_creates_parent_dirs(stores_dir):
    """mkdir(parents=True) — a fresh box has no Manifest/embeddings yet."""
    assert not stores_dir.exists()
    store.set_rerank_selection(SELECTION, base_dir=stores_dir)
    assert (stores_dir / store.RERANK_FILE).exists()


# ── fail-open reads ───────────────────────────────────────────────────────────

def test_get_missing_file_returns_none(stores_dir):
    assert store.get_rerank_selection(base_dir=stores_dir) is None


def test_get_missing_dir_returns_none(tmp_path):
    """base_dir that isn't a directory (NotADirectoryError) fails open."""
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("x")
    assert store.get_rerank_selection(base_dir=not_a_dir) is None


def test_get_corrupt_json_returns_none(stores_dir):
    stores_dir.mkdir(parents=True, exist_ok=True)
    (stores_dir / store.RERANK_FILE).write_text("{not json", encoding="utf-8")
    assert store.get_rerank_selection(base_dir=stores_dir) is None


def test_get_non_dict_json_returns_none(stores_dir):
    """A valid-JSON but wrong-shape file (list/scalar) fails open to None —
    the selection must be an object."""
    stores_dir.mkdir(parents=True, exist_ok=True)
    for payload in ("[1, 2, 3]", "\"a string\"", "42", "null"):
        (stores_dir / store.RERANK_FILE).write_text(payload, encoding="utf-8")
        assert store.get_rerank_selection(base_dir=stores_dir) is None


# ── atomicity ─────────────────────────────────────────────────────────────────

def test_atomic_write_leaves_no_tmp_and_full_content(stores_dir):
    """_atomic_write_json (tmp + os.replace) — after the write the final file
    holds the complete selection and no .tmp sibling lingers."""
    store.set_rerank_selection(SELECTION, base_dir=stores_dir)
    path = stores_dir / store.RERANK_FILE
    assert path.exists()
    assert not path.with_name(path.name + ".tmp").exists()
    assert json.loads(path.read_text()) == SELECTION
