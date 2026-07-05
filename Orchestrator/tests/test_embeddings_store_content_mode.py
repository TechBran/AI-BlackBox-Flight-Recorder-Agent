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

from Orchestrator import fossils
from Orchestrator.embeddings.chunker import chunk_snapshot, chunks_for_snapshot
from Orchestrator.embeddings.store import VectorStore

DIMS = 4
SLUG = "unit-content-mode"
GROUP = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]

# A full envelope-inclusive snapshot (BEACON/TRACKER/GAUGES header → SNAPSHOT
# BODY → Raw Session Log body), long enough that the chunker emits >1 chunk so
# the ordinal-0 whole-doc prepend + windowing paths are both exercised.
_ENVELOPE_TEXT = (
    "=== START SNAPSHOT — UTC 2026-07-04T00:00:00Z — SNAP-M13 ===\n"
    "CROSS-FILE BEACON\nTail lock confirmed\n"
    "VOLUME TRACKER\nTail: SNAP-0\nGAUGES\nOPERATOR: Anna\n\n"
    "SNAPSHOT BODY\n\nKernel Index\n- Current: SNAP-M13\n\nRaw Session Log\n"
    + "\n".join(f"- [{i}] user: line {i:05d} " + "abcdefghij" * 4
                for i in range(400))
    + "\n=== END SNAPSHOT — SNAP-M13 ===\n"
)


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


# ── M13 consolidation: fresh-box / content_mode-absent back-compat ────────────
# The M14.4 re-embed was ABANDONED — the ACTIVE store stays content_mode-absent
# (→ "full"), so the M14.3 body-only infra must be byte-identically INERT on a
# full store. These pin that a store predating the flag opens "full" AND that
# the shared chunk helper + the windower behave exactly as pre-M14.3 there
# (envelope KEPT — zero body-only stripping).

def test_full_mode_chunks_are_byte_identical_to_pre_m14_3():
    """chunks_for_snapshot(text, "full") == the exact pre-M14.3 inline behavior
    ([text] + chunk_snapshot(text)), envelope included at ordinal 0. BOTH the
    mint seam (embed_snapshot_for_index) and migrate (chunk_group_batches)
    delegate to this ONE helper, so full-mode parity here IS their parity."""
    expected = [_ENVELOPE_TEXT] + chunk_snapshot(_ENVELOPE_TEXT, model_key=None)
    assert len(expected) > 1                       # sanity: ordinal-0 prepend applies
    got = chunks_for_snapshot(_ENVELOPE_TEXT, model_key=None, content_mode="full")
    assert got == expected
    assert "CROSS-FILE BEACON" in got[0]           # envelope PRESENT (no stripping)


def test_absent_content_mode_store_reads_full_and_feeds_full_to_the_helper(tmp_path):
    """A v2 store whose meta predates the flag opens "full" (the abandoned-14.4
    state — the active store is content_mode-absent), and threading that
    resolved mode through the shared chunk helper keeps the envelope, exactly
    as pre-M14.3."""
    store = VectorStore(SLUG, DIMS, tmp_path, schema=2).open()
    store.append_group("SNAP-A", GROUP)
    meta_path = tmp_path / SLUG / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.pop("content_mode", None)                 # simulate a pre-M14.3 store
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    reopened = VectorStore(SLUG, DIMS, tmp_path).open()
    assert reopened.content_mode == "full"
    chunks = chunks_for_snapshot(_ENVELOPE_TEXT, model_key=None,
                                 content_mode=reopened.content_mode)
    assert chunks == [_ENVELOPE_TEXT] + chunk_snapshot(_ENVELOPE_TEXT,
                                                       model_key=None)


def test_full_mode_window_is_byte_identical_and_the_body_param_is_inert(monkeypatch):
    """window_snapshot_text on a full/fresh store: the explicit content_mode="full"
    output equals the content_mode-DEFAULT (None) output — the body-only param is
    inert. A fresh box (no active store) degrades the None default to "full", so
    the window keeps the START marker and the envelope-anchored span, exactly as
    pre-M14.3 (before content_mode existed)."""
    ordinal, budget = 2, 2000
    full = fossils.window_snapshot_text(_ENVELOPE_TEXT, ordinal, budget,
                                        model_key=None, content_mode="full")
    # Simulate the fresh box: the active-store resolver degrades to "full".
    monkeypatch.setattr(fossils, "_resolve_active_content_mode", lambda: "full")
    default = fossils.window_snapshot_text(_ENVELOPE_TEXT, ordinal, budget,
                                           model_key=None)
    assert full == default                         # content_mode param inert here
    assert len(full) <= budget
    assert full.startswith("=== START SNAPSHOT")   # START marker always preserved
