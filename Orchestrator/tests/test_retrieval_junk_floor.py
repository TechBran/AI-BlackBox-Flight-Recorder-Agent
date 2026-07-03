"""Per-model registry junk_floor, flag-gated (WI-3 / M9, audit A8).

The junk floor drops obvious noise below the semantic candidates; it is NEVER
relevance selection (the measured relevance/noise band gap on the live
chunk-max store is +0.013 — far too thin to select on; ranking does that).
Resolution precedence, implemented by retrieval._resolve_junk_floor:

    [retrieval] registry_floor_enabled (default FALSE)
        AND the store's model declares a non-null registry junk_floor
        -> use the per-model calibrated NOISE floor
    else
        -> the global [retrieval] junk_floor (default 0.40)

Flag off (the shipped default) must be BYTE-IDENTICAL to the historical
single-knob behavior — the registry values are inert until an operator flips
the flag. Resolution keys on the STORE's slug (not the active pointer), so
the eval seam (retrieve(store=...), M4) benches a candidate arm with the
floor that arm would ship with.

Permanent regression guard (audit A8 wipe scenario): qwen3-0.6b scores
on-topic hits ~0.45, so a gemini-band floor (0.54/0.55) EMPTIES the
phone-lean semantic-only profile; qwen's own 0.35 floor must keep those hits.
"""
import math

import pytest

import Orchestrator.retrieval as retrieval
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.tests.test_retrieval_core import _with_retrieval_keys
from Orchestrator.tests.test_retrieval_store_override import FakeStore


def _row(cos: float) -> list[float]:
    """Unit 4-dim vector whose cosine against the query [1,0,0,0] is `cos`."""
    return [cos, math.sqrt(1.0 - cos * cos), 0.0, 0.0]


# HI clears every floor in play; MID (0.45) sits between the global 0.40 and
# gemini-2's registry 0.55 — the discriminating probe; LO (0.20) is sub-noise
# junk under every floor.
ROWS = [("SNAP-HI", _row(0.70)), ("SNAP-MID", _row(0.45)), ("SNAP-LO", _row(0.20))]

# Equal timestamps: recency boosts cancel, ranking is pure semantic rank order.
FAKE_INDEX = {
    sid: {"operator": "alice", "timestamp": "2026-06-01T00:00:00Z"}
    for sid in ("SNAP-HI", "SNAP-MID", "SNAP-LO", "SNAP-Q1", "SNAP-Q2", "SNAP-Q3")
}


@pytest.fixture()
def hermetic(monkeypatch):
    """No network, no live store, no volume — pin every external seam."""
    monkeypatch.setattr(
        retrieval._emb, "generate_embedding_sync",
        lambda text, purpose="query": [1.0, 0.0, 0.0, 0.0],
    )
    monkeypatch.setattr(retrieval, "load_snapshot_index", lambda: dict(FAKE_INDEX))
    monkeypatch.setattr(
        retrieval, "keyword_retrieve_ids_for_operator", lambda vol, q, n, op: []
    )


def _set_active(monkeypatch, store):
    monkeypatch.setattr(retrieval._emb, "get_active_store", lambda: store)


# ── resolution precedence matrix (unit level) ─────────────────────────────────

@pytest.mark.parametrize("flag,slug,global_floor,expected", [
    (None,    "gemini-embedding-2",   "0.40", 0.40),  # flag ABSENT -> global
    ("false", "gemini-embedding-2",   "0.40", 0.40),  # flag off -> global
    ("true",  "gemini-embedding-2",   "0.40", 0.55),  # flag on + non-null -> registry
    ("true",  "gemini-embedding-001", "0.40", 0.40),  # flag on + NULL floor -> global
    ("true",  "gemini-embedding-001", "0.50", 0.50),  # null-floor fallback is the CONFIG value
    ("true",  "qwen3-embedding-0.6b", "0.40", 0.35),
    ("true",  "qwen3-embedding-8b",   "0.40", 0.35),
    ("true",  "no-such-model",        "0.40", 0.40),  # unknown slug -> global
    ("true",  None,                   "0.40", 0.40),  # slug-less store -> global
])
def test_resolution_precedence_matrix(flag, slug, global_floor, expected):
    store = FakeStore([], slug=slug)
    with _with_retrieval_keys(registry_floor_enabled=flag, junk_floor=global_floor):
        assert retrieval._resolve_junk_floor(store) == pytest.approx(expected)


# ── flag OFF: registry values are inert (byte-identical pin) ──────────────────

def test_flag_off_registry_floor_present_but_global_040_used(hermetic, monkeypatch):
    """gemini-2 SHIPS junk_floor 0.55 in the registry, but with the flag off
    (the default) MID at cos 0.45 must still be returned — the registry value
    is inert and the global 0.40 governs, byte-identical to pre-M9. Flag
    absent and flag explicitly false must produce the same results."""
    assert EMBEDDING_MODELS["gemini-embedding-2"]["junk_floor"] == 0.55  # pin shipped value
    _set_active(monkeypatch, FakeStore(ROWS, slug="gemini-embedding-2"))

    with _with_retrieval_keys(registry_floor_enabled=None, junk_floor="0.40"):
        absent = retrieval.retrieve("test query", "system", k=5)
    with _with_retrieval_keys(registry_floor_enabled="false", junk_floor="0.40"):
        explicit_off = retrieval.retrieve("test query", "system", k=5)

    # Identical ranking both ways (scores compared with approx: the recency
    # boost reads the wall clock, which drifts ~1e-13 between the two calls).
    assert [sid for sid, _ in absent] == [sid for sid, _ in explicit_off]
    assert [s for _, s in absent] == pytest.approx([s for _, s in explicit_off])
    assert [sid for sid, _ in absent] == ["SNAP-HI", "SNAP-MID"]  # LO < 0.40 dropped


# ── flag ON: per-model floors apply on the ACTIVE store ──────────────────────

def test_flag_on_active_gemini2_applies_055(hermetic, monkeypatch):
    _set_active(monkeypatch, FakeStore(ROWS, slug="gemini-embedding-2"))
    with _with_retrieval_keys(registry_floor_enabled="true", junk_floor="0.40"):
        results = retrieval.retrieve("test query", "system", k=5)
    # MID (0.45) now sits below the 0.55 registry floor.
    assert [sid for sid, _ in results] == ["SNAP-HI"]


def test_flag_on_null_floor_model_falls_back_to_global(hermetic, monkeypatch):
    _set_active(monkeypatch, FakeStore(ROWS, slug="gemini-embedding-001"))
    with _with_retrieval_keys(registry_floor_enabled="true", junk_floor="0.40"):
        results = retrieval.retrieve("test query", "system", k=5)
    assert [sid for sid, _ in results] == ["SNAP-HI", "SNAP-MID"]


# ── the phone-lean wipe scenario (audit A8) — permanent regression guard ──────

@pytest.mark.parametrize("slug", ["qwen3-embedding-0.6b", "qwen3-embedding-8b"])
def test_phone_lean_qwen_floor_keeps_on_topic_hits(hermetic, monkeypatch, slug):
    """On-topic qwen-like scores (~0.43–0.47) with the registry floor ACTIVE:
    semantic-only retrieve must return NON-EMPTY. Every row deliberately sits
    BELOW 0.54 (the gemini-band floor that wiped the phone-lean profile in
    the audit's live probe) and ABOVE 0.35 (qwen's own noise floor) — if a
    future edit pushes the qwen floors into the gemini band, this fails."""
    assert EMBEDDING_MODELS[slug]["junk_floor"] == 0.35  # pin shipped value
    rows = [("SNAP-Q1", _row(0.47)), ("SNAP-Q2", _row(0.45)), ("SNAP-Q3", _row(0.43))]
    _set_active(monkeypatch, FakeStore(rows, slug=slug))

    with _with_retrieval_keys(registry_floor_enabled="true", junk_floor="0.40"):
        results = retrieval.retrieve(
            "test query", "system", k=3, include_keyword=False
        )

    assert results, "phone-lean semantic-only retrieve WIPED with the qwen floor active"
    assert {sid for sid, _ in results} == {"SNAP-Q1", "SNAP-Q2", "SNAP-Q3"}


# ── eval seam: store= override resolves the OVERRIDE slug's floor ─────────────

def test_store_override_resolves_override_slugs_floor(hermetic, monkeypatch):
    """The floor must come from the OVERRIDE store's slug — an eval arm is
    benched with the floor it would ship with, and the active pointer is never
    consulted (get_active_store is patched to raise)."""
    def _boom():
        raise AssertionError("get_active_store() consulted despite store= override")
    monkeypatch.setattr(retrieval._emb, "get_active_store", _boom)

    rows = [("SNAP-MID", _row(0.45))]
    with _with_retrieval_keys(registry_floor_enabled="true", junk_floor="0.40"):
        kept = retrieval.retrieve(
            "test query", "system", k=5,
            store=FakeStore(rows, slug="qwen3-embedding-0.6b"),  # floor 0.35
        )
        dropped = retrieval.retrieve(
            "test query", "system", k=5,
            store=FakeStore(rows, slug="gemini-embedding-2"),  # floor 0.55
        )

    assert [sid for sid, _ in kept] == ["SNAP-MID"]  # 0.45 >= 0.35
    assert dropped == []                             # 0.45 < 0.55
