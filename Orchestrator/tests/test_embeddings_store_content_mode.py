"""Store-level content_mode flag ("full" | "body") — M14.3a.

The v2 embedding store records whether its chunk vectors were built from the
WHOLE envelope-inclusive snapshot text ("full", today's behavior) or from the
body-only content ("body", M14.3 — the Raw-Session-Log region only). The flag
is STORE-schema-derived (persisted in meta.json, mirroring the v1/v2 `schema`
branch), so mint, the on-device windower, and migrate all read ONE source of
truth: a fresh/rolled-back store stays correct and an existing store keeps
working (absent field -> "full").

Hermetic against tmp_path — never the live Manifest/ stores.
"""
import json

from Orchestrator.embeddings.store import VectorStore

DIMS = 4
SLUG = "unit-content-mode"
GROUP = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]


def _read_meta(tmp_path, slug=SLUG):
    return json.loads((tmp_path / slug / "meta.json").read_text(encoding="utf-8"))


def test_fresh_store_defaults_full(tmp_path):
    """A fresh store (v1 or v2) defaults to content_mode "full"."""
    v1 = VectorStore(SLUG, DIMS, tmp_path).open()
    assert v1.content_mode == "full"
    v2 = VectorStore("unit-cm-v2", DIMS, tmp_path, schema=2).open()
    assert v2.content_mode == "full"


def test_constructor_body_on_fresh_v2_store_persists(tmp_path):
    """A caller explicitly requesting "body" for a FRESH v2 store keeps it, and
    it is persisted to meta.json (reopen with no request -> still "body")."""
    store = VectorStore(SLUG, DIMS, tmp_path, schema=2, content_mode="body").open()
    assert store.content_mode == "body"
    store.append_group("SNAP-A", GROUP)

    meta = _read_meta(tmp_path)
    assert meta["content_mode"] == "body"

    reopened = VectorStore(SLUG, DIMS, tmp_path).open()
    assert reopened.content_mode == "body"


def test_absent_content_mode_field_reads_full(tmp_path):
    """Back-compat: a v2 store whose meta predates the flag reads "full"."""
    store = VectorStore(SLUG, DIMS, tmp_path, schema=2).open()
    store.append_group("SNAP-A", GROUP)
    # Simulate a pre-M14.3 meta by stripping the field back out.
    meta_path = tmp_path / SLUG / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("content_mode", None)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    reopened = VectorStore(SLUG, DIMS, tmp_path).open()
    assert reopened.content_mode == "full"


def test_disk_content_mode_wins_over_constructor(tmp_path):
    """The on-disk value wins over a conflicting constructor request (mirror of
    the schema autodetect rule)."""
    seed = VectorStore(SLUG, DIMS, tmp_path, schema=2, content_mode="body").open()
    seed.append_group("SNAP-A", GROUP)

    # Reopen requesting "full" — disk "body" must win.
    reopened = VectorStore(SLUG, DIMS, tmp_path, content_mode="full").open()
    assert reopened.content_mode == "body"


def test_v1_meta_never_gains_content_mode_key(tmp_path):
    """content_mode is a v2 concept: a v1 store's meta key set is unchanged."""
    store = VectorStore(SLUG, DIMS, tmp_path).open()
    store.append("snap-a", [1.0, 0.0, 0.0, 0.0])
    meta = _read_meta(tmp_path)
    assert "content_mode" not in meta
    assert store.content_mode == "full"
