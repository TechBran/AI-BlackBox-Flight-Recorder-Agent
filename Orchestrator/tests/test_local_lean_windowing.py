"""M8 / WI-7a — matched-chunk windowing for window-bound profiles.

Cloud surfaces deliver snapshots WHOLE (WI-10/M7); the ONE genuinely
window-bound profile is the on-device phone lean path (6,144-token engine
window). These tests pin the three additive seams that make its delivery
window-smart instead of head-blind:

1. Store provenance: ``VectorStore.search_with_vectors(..., with_ordinals=
   True)`` returns each winner's best-chunk ordinal (v2 collapse identity);
   the default 3-tuple contract is FROZEN.
2. Retrieval provenance: ``retrieve(..., return_provenance=True)`` annotates
   the (identical) top-k with best_ordinal; keyword-only/v1 candidates carry
   None. Default 2-tuple path byte-identical.
3. Windowed delivery: ``fossils.window_snapshot_text`` centers the delivered
   window on the matched chunk's span (re-derived via the deterministic
   chunker); ordinal 0/None -> today's head truncation. semantic_retrieve /
   hybrid_retrieve grow a keyword-use ``window_budget_chars`` (None = cloud =
   byte-identical); the search_snapshots executor bounds ONLY the on-device
   caller.

Hermetic except the two explicitly live-marked tests (skip-guarded on the
live store, mirroring test_local_lean_retrieval.py).
"""
import asyncio
from unittest import mock

import numpy as np
import pytest

# MODULE-level full-app import (mirrors the elevenlabs route tests): pytest
# imports every test module at collection, BEFORE any test fires a request on
# the shared FastAPI app — so Orchestrator.app's import-time add_middleware
# always precedes the first request, regardless of file ordering. A lazy
# in-test import instead RuntimeErrors ("Cannot add middleware after an
# application has started") whenever an earlier module's test posted first.
from Orchestrator.app import app  # noqa: F401  (used by the route test below)

import Orchestrator.fossils as fossils
import Orchestrator.retrieval as retrieval
from Orchestrator.embeddings.chunker import chunk_snapshot
from Orchestrator.embeddings.store import VectorStore
from Orchestrator.fossils import cap_chars, extract_snap_ids, window_snapshot_text

DIMS = 4
SLUG = "unit-m8-model"
QUERY_VEC = [1.0, 0.0, 0.0, 0.0]


# ── 1. store: best-chunk ordinal provenance ───────────────────────────────────

# Group layout mirrors production (M6f group policy): whole-doc vector at
# ordinal 0, chunks at ordinals 1..n. Ordinal 3 (chunk index 2) is the best
# match for QUERY_VEC; the whole-doc vector is deliberately mediocre.
GROUP_BIG = [
    [0.5, 0.5, 0.5, 0.5],   # ordinal 0: whole doc (cos ≈ 0.5)
    [0.0, 1.0, 0.0, 0.0],   # ordinal 1
    [0.0, 0.0, 1.0, 0.0],   # ordinal 2
    [1.0, 0.05, 0.0, 0.0],  # ordinal 3: BEST chunk (cos ≈ 0.999)
    [0.0, 0.0, 0.0, 1.0],   # ordinal 4
]


def _v2_store(tmp_path):
    store = VectorStore(SLUG, DIMS, tmp_path, schema=2).open()
    store.append_group("SNAP-BIG", GROUP_BIG)
    store.append_group("SNAP-ONE", [[0.6, 0.8, 0.0, 0.0]])  # single-chunk group
    return store


def test_with_ordinals_reports_best_chunk_ordinal(tmp_path):
    store = _v2_store(tmp_path)
    results = store.search_with_vectors(QUERY_VEC, k=2, with_ordinals=True)
    assert [len(r) for r in results] == [4, 4]
    sid, score, vec, ordinal = results[0]
    assert sid == "SNAP-BIG"
    assert ordinal == 3  # the specific chunk that won the collapse
    assert score == pytest.approx(0.9988, abs=1e-3)
    assert np.allclose(vec, np.asarray(GROUP_BIG[3]) / np.linalg.norm(GROUP_BIG[3]),
                       atol=1e-5)
    # single-chunk group: its one row IS the whole text -> ordinal 0
    assert results[1][0] == "SNAP-ONE"
    assert results[1][3] == 0


def test_default_three_tuple_contract_frozen(tmp_path):
    store = _v2_store(tmp_path)
    results = store.search_with_vectors(QUERY_VEC, k=2)
    assert [len(r) for r in results] == [3, 3]  # no 4th element by default


def test_v1_store_ordinals_are_none(tmp_path):
    store = VectorStore("unit-m8-v1", DIMS, tmp_path).open()  # schema 1
    store.append("SNAP-V1", [1.0, 0.0, 0.0, 0.0])
    results = store.search_with_vectors(QUERY_VEC, k=1, with_ordinals=True)
    assert results[0][0] == "SNAP-V1"
    assert results[0][3] is None  # whole-doc rows: no chunk identity
    # default contract on v1 stays 3-tuples too
    assert len(store.search_with_vectors(QUERY_VEC, k=1)[0]) == 3


# ── 2. retrieve(): return_provenance annotation ───────────────────────────────

class FakeStore:
    """VectorStore stand-in with with_ordinals support (rows carry ordinals)."""

    def __init__(self, rows, dims=4, slug=None):
        # rows: [(snap_id, vector, ordinal)]
        self.dims = dims
        self.slug = slug
        self._rows = []
        for sid, v, o in rows:
            vec = np.asarray(v, dtype=np.float32)
            n = float(np.linalg.norm(vec))
            self._rows.append((sid, vec / n if n > 0 else vec, o))

    @property
    def count(self):
        return len(self._rows)

    def search_with_vectors(self, query_vec, k, allowed_ids=None,
                            with_ordinals=False):
        q = np.asarray(query_vec, dtype=np.float32)
        n = float(np.linalg.norm(q))
        if n > 0:
            q = q / n
        scored = [
            (sid, float(vec @ q), vec.copy(), o)
            for sid, vec, o in self._rows
            if allowed_ids is None or sid in allowed_ids
        ]
        scored.sort(key=lambda t: t[1], reverse=True)
        scored = scored[:k]
        if with_ordinals:
            return scored
        return [(sid, cos, vec) for sid, cos, vec, _o in scored]


FAKE_INDEX = {
    "SNAP-A": {"operator": "alice", "timestamp": "2026-06-01T00:00:00Z"},
    "SNAP-B": {"operator": "alice", "timestamp": "2026-06-01T00:00:00Z"},
    "SNAP-KW": {"operator": "alice", "timestamp": "2026-06-01T00:00:00Z"},
}


@pytest.fixture()
def hermetic_retrieval(monkeypatch):
    monkeypatch.setattr(
        retrieval._emb, "generate_embedding_sync",
        lambda text, purpose="query": [1.0, 0.0, 0.0, 0.0],
    )
    monkeypatch.setattr(retrieval, "load_snapshot_index", lambda: dict(FAKE_INDEX))
    monkeypatch.setattr(
        retrieval, "keyword_retrieve_ids_for_operator",
        lambda vol, q, n, op: ["SNAP-KW"],
    )


def _fake_store():
    return FakeStore([
        ("SNAP-A", [1.0, 0.1, 0.0, 0.0], 3),   # semantic winner, chunk ordinal 3
        ("SNAP-B", [0.7, 0.7, 0.0, 0.0], 0),   # whole-doc vector won
    ])


def test_retrieve_provenance_annotates_ordinals(hermetic_retrieval):
    results = retrieval.retrieve(
        "q", "system", k=5, store=_fake_store(), return_provenance=True
    )
    by_id = {sid: (score, o) for sid, score, o in results}
    assert by_id["SNAP-A"][1] == 3
    assert by_id["SNAP-B"][1] == 0
    # keyword-only candidate never entered the semantic channel -> None
    assert by_id["SNAP-KW"][1] is None


def test_retrieve_default_path_unchanged_by_provenance_flag(hermetic_retrieval):
    default = retrieval.retrieve("q", "system", k=5, store=_fake_store())
    annotated = retrieval.retrieve(
        "q", "system", k=5, store=_fake_store(), return_provenance=True
    )
    assert all(len(r) == 2 for r in default)
    # identical ranking, identical scores — the flag only annotates. Scores are
    # compared approximately: the recency boost is computed from now(), which
    # drifts ~1e-13 between the two calls.
    assert [sid for sid, _s, _o in annotated] == [sid for sid, _s in default]
    for (sid_a, s_a, _o), (_sid_d, s_d) in zip(annotated, default):
        assert s_a == pytest.approx(s_d, abs=1e-9)


# ── 3. window_snapshot_text ───────────────────────────────────────────────────

START_LINE = "=== START SNAPSHOT — UTC 2026-07-03T00:00:00Z — SNAP-20260703-0001 ==="
END_LINE = "=== END SNAPSHOT — SNAP-20260703-0001 ==="
NEEDLE = "NEEDLE-PHRASE the matched fact lives exactly here"


def _big_snapshot(total_chars=30_000):
    """Synthetic ~30k snapshot: head filler, a unique needle ~70% in, tail."""
    filler = "HEAD-FILLER lorem ipsum dolor sit amet consectetur adipiscing.\n"
    body = []
    n = 0
    target_needle_at = int(total_chars * 0.7)
    needle_placed = False
    while n < total_chars:
        if not needle_placed and n >= target_needle_at:
            line = NEEDLE + "\n"
            needle_placed = True
        else:
            line = filler
        body.append(line)
        n += len(line)
    return START_LINE + "\n" + "".join(body) + END_LINE


def _needle_ordinal(text):
    """Group ordinal of the chunk containing the needle (chunk i -> ordinal i+1)."""
    chunks = chunk_snapshot(text, model_key=None)
    assert len(chunks) > 1, "fixture must be multi-chunk"
    idx = next(i for i, c in enumerate(chunks) if NEEDLE in c)
    return idx + 1


def test_window_centers_on_matched_chunk_not_head():
    text = _big_snapshot()
    budget = 4000
    ordinal = _needle_ordinal(text)
    windowed = window_snapshot_text(text, ordinal, budget, model_key=None)
    assert len(windowed) <= budget
    assert NEEDLE in windowed                      # matched chunk delivered
    assert windowed.startswith(START_LINE)         # provenance marker kept
    assert fossils._WINDOW_GAP_MARK in windowed    # elision is visible
    # the head would have been ~4k of HEAD-FILLER; the window must NOT be the head
    head = cap_chars(text, budget)
    assert NEEDLE not in head                      # sanity: head misses the fact
    assert windowed != head


def test_window_ordinal_zero_and_none_head_truncate():
    text = _big_snapshot()
    budget = 4000
    for ordinal in (0, None):
        out = window_snapshot_text(text, ordinal, budget, model_key=None)
        assert out == cap_chars(text, budget)
        assert out.startswith(START_LINE)


def test_window_under_budget_text_untouched():
    text = _big_snapshot(total_chars=2000)
    assert window_snapshot_text(text, 2, 50_000, model_key=None) == text


def test_window_out_of_range_ordinal_falls_back_to_head():
    text = _big_snapshot()
    out = window_snapshot_text(text, 9999, 4000, model_key=None)
    assert out == cap_chars(text, 4000)


def test_window_end_marker_kept_when_present():
    text = _big_snapshot()
    ordinal = _needle_ordinal(text)
    windowed = window_snapshot_text(text, ordinal, 4000, model_key=None)
    # needle sits at ~70% of 30k -> the window cannot reach the doc end, so the
    # END marker is re-attached after the gap mark
    assert windowed.rstrip().endswith(END_LINE)


# ── 4. delivery seams: semantic_retrieve / hybrid_retrieve / context builder ──

def _fake_volume(texts_by_id):
    """(index, vol_bytes) for byte-offset decode of the given snapshot texts."""
    blob = b""
    index = {}
    for sid, text in texts_by_id.items():
        b = text.encode("utf-8")
        index[sid] = {
            "operator": "alice",
            "timestamp": "2026-06-01T00:00:00Z",
            "byte_start": len(blob),
            "byte_end": len(blob) + len(b),
        }
        blob += b
    return index, blob


def test_semantic_retrieve_windows_only_when_budget_passed(monkeypatch):
    text = _big_snapshot()
    ordinal = _needle_ordinal(text)
    index, blob = _fake_volume({"SNAP-20260703-0001": text})

    captured = {}

    def _fake_retrieve(query, operator="", k=10, *, include_keyword=True,
                       store=None, query_vector=None, return_provenance=False):
        captured["return_provenance"] = return_provenance
        if return_provenance:
            return [("SNAP-20260703-0001", 0.9, ordinal)]
        return [("SNAP-20260703-0001", 0.9)]

    monkeypatch.setattr("Orchestrator.retrieval.retrieve", _fake_retrieve)
    monkeypatch.setattr(fossils, "load_snapshot_index", lambda: index)
    monkeypatch.setattr(fossils, "read_volume_bytes", lambda path: blob)
    # Hermetic model_key: the fixture ordinal was derived with model_key=None,
    # so the delivery-side re-derivation must use the same tokenizer backend.
    monkeypatch.setattr(
        "Orchestrator.embeddings.store.get_active_slug", lambda base_dir=None: None
    )

    # default (cloud) path: no provenance requested, text delivered WHOLE
    whole = fossils.semantic_retrieve("q", operator="alice", k=3)
    assert captured["return_provenance"] is False
    assert whole == [text]

    # window-bound path: provenance requested, best-chunk window delivered
    windowed = fossils.semantic_retrieve(
        "q", operator="alice", k=3, window_budget_chars=4000
    )
    assert captured["return_provenance"] is True
    assert len(windowed) == 1
    assert len(windowed[0]) <= 4000
    assert NEEDLE in windowed[0]
    assert windowed[0].startswith(START_LINE)
    # ranking/provenance ids unchanged by windowing
    assert extract_snap_ids(windowed) == extract_snap_ids(whole)


def test_hybrid_retrieve_requests_provenance_only_with_budget(monkeypatch):
    calls = []

    def _fake_retrieve(query, operator="", k=10, *, include_keyword=True,
                       store=None, query_vector=None, return_provenance=False):
        calls.append(return_provenance)
        return []

    monkeypatch.setattr("Orchestrator.retrieval.retrieve", _fake_retrieve)
    assert fossils.hybrid_retrieve("", "q", k=3, operator="alice") == []
    assert fossils.hybrid_retrieve(
        "", "q", k=3, operator="alice", window_budget_chars=2000
    ) == []
    assert calls == [False, True]


def test_build_fossil_context_passes_local_cap_as_window_budget():
    from Orchestrator import context_builder as cb
    from Orchestrator.config import CFG

    with mock.patch.object(cb, "semantic_retrieve", return_value=[]) as sem, \
         mock.patch.object(cb, "get_recent_checkpoints_for_operator", return_value=[]), \
         mock.patch.object(cb, "get_recent_fossils_for_operator", return_value=[]), \
         mock.patch.object(cb, "keyword_retrieve_for_operator", return_value=[]), \
         mock.patch.object(cb, "read_text_safe", return_value=""), \
         mock.patch.object(cb, "get_recent_media_artifacts", return_value=[]):
        cb.build_fossil_context(
            "q", "Brandon", provider="local",
            semantic_k=3, checkpoint_count=0,
            include_recent=False, include_keyword=False, include_media=False,
        )
        local_budget = sem.call_args.kwargs["window_budget_chars"]
        cb.build_fossil_context("q", "Brandon", provider="anthropic")
        cloud_budget = sem.call_args.kwargs["window_budget_chars"]

    assert local_budget == CFG.getint("context", "max_fossil_chars", fallback=10000)
    assert cloud_budget is None  # cloud stays WHOLE (WI-10/M7)


# ── 5. search_snapshots executor: on-device caller bound ─────────────────────

def _run_search_executor(caller, limit=5):
    import importlib.util
    from pathlib import Path

    from Orchestrator.toolvault.context import ToolContext

    mod_path = (
        Path(__file__).resolve().parents[2]
        / "ToolVault" / "tools" / "search_snapshots" / "executor.py"
    )
    spec = importlib.util.spec_from_file_location("m8_search_snapshots", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    captured = {}

    def _fake_hybrid(vol_txt, query, k=3, operator="", window_budget_chars=None):
        captured["window_budget_chars"] = window_budget_chars
        captured["k"] = k
        return [f"--- snap for {query} ---"]

    with mock.patch.object(fossils, "hybrid_retrieve", _fake_hybrid), \
         mock.patch("Orchestrator.volume.read_text_safe", return_value=""):
        ctx = ToolContext(operator="alice", caller=caller)
        result = asyncio.run(mod.execute({"query": "q", "limit": limit}, ctx))
    return result, captured


def test_executor_bounds_on_device_caller_only():
    from Orchestrator.toolvault.context import ON_DEVICE_CALLER

    # on-device caller: bounded, budget split across the requested results
    result, captured = _run_search_executor(ON_DEVICE_CALLER, limit=5)
    assert result.success
    assert captured["window_budget_chars"] == 8000 // 5
    assert captured["k"] == 5

    # every other surface (None / MCP gateway) stays WHOLE per WI-10/M7
    for caller in (None, "mcp-gateway"):
        result, captured = _run_search_executor(caller)
        assert result.success
        assert captured["window_budget_chars"] is None


def test_executor_window_floor_for_large_limits():
    from Orchestrator.toolvault.context import ON_DEVICE_CALLER
    _, captured = _run_search_executor(ON_DEVICE_CALLER, limit=20)
    assert captured["window_budget_chars"] == 1000  # LOCAL_MIN_WINDOW_CHARS floor


def test_local_tools_execute_stamps_on_device_caller():
    """/local/tools/execute with NO X-BlackBox-Caller header (the Android
    bridge) must thread caller=ON_DEVICE_CALLER; the MCP gateway's declared
    header passes through unchanged."""
    from fastapi.testclient import TestClient

    import Orchestrator.routes.local_routes as lr
    from Orchestrator.toolvault.context import ON_DEVICE_CALLER, ToolResult

    client = TestClient(app)
    seen = {}

    async def _fake_execute_tool(tool, params, operator, caller=None):
        seen["caller"] = caller
        return ToolResult(True, "ok")

    with mock.patch.object(lr, "execute_tool", _fake_execute_tool):
        resp = client.post("/local/tools/execute",
                           json={"tool": "roll_dice", "operator": "alice"})
        assert resp.status_code == 200
        assert seen["caller"] == ON_DEVICE_CALLER

        resp = client.post("/local/tools/execute",
                           json={"tool": "roll_dice", "operator": "alice"},
                           headers={"X-BlackBox-Caller": "mcp-gateway"})
        assert resp.status_code == 200
        assert seen["caller"] == "mcp-gateway"


# ── 6. live lean profile (skip-guarded, mirrors test_local_lean_retrieval) ───

def _require_live_store():
    try:
        from Orchestrator.embeddings.search import get_active_store
        store = get_active_store()
    except Exception as e:  # noqa: BLE001 - provider/store unavailable in test env
        pytest.skip(f"active store/provider unavailable: {e}")
    if store.count == 0:
        pytest.skip("active store empty")
    return store


def test_live_lean_windowed_delivery_under_budget():
    """LIVE: the lean profile's semantic delivery fits the per-snapshot budget
    with ranking untouched — every text <= budget, same ids as the whole-text
    call, and each windowed block still carries its START marker."""
    store = _require_live_store()
    if store.schema != 2:
        pytest.skip("live store is not chunked (schema 1) — no ordinals to window on")
    budget = 2000
    query = "pluggable embeddings model migration reembed"
    whole = fossils.semantic_retrieve(query, operator="system", k=3)
    if not whole:
        pytest.fail("lean semantic delivery EMPTY on a live store (starvation)")
    windowed = fossils.semantic_retrieve(
        query, operator="system", k=3, window_budget_chars=budget
    )
    assert extract_snap_ids(windowed) == extract_snap_ids(whole)
    for text in windowed:
        assert len(text) <= budget
        assert "=== START SNAPSHOT" in text
