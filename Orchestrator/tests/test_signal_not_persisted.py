"""Regression guard — "The Signal" telemetry NEVER leaks into the prompt/context
or a snapshot (Task 1.9).

The load-bearing invariant of this feature: the UI-only ``system_activity``
telemetry is PRESENTATION-ONLY. It lives ONLY in the separate ``telemetry`` sink
dict (filled in place) and the SSE ``system_activity`` event burst. It is NEVER
persisted into:
  * the LLM prompt / retrieval context (the fossil_context string), or
  * a snapshot / the ledger.

These tests are HERMETIC and fast: NO live embedding provider, NO network, NO
running server. They drive:
  * retrieve()'s eval seam (``store=`` + ``query_vector=``) with an in-memory
    fake store — exactly the pattern in test_retrieval_telemetry.py — so the
    telemetry-fill code runs against a deterministic input, and
  * build_fossil_context() with monkeypatched channel functions (recent /
    keyword / semantic / checkpoint / media / volume-read all faked) so the
    prompt-context assembly runs with no I/O and no embeddings.

The value here is regression protection: these assert invariants of already-
correct code, so they pass immediately. If a future change starts threading a
rendered ``system_activity`` label into the delivered context — or imports the
SSE builder into a prompt/snapshot builder — one of these turns red.
"""
import os
from pathlib import Path

import numpy as np

from Orchestrator.retrieval import retrieve
from Orchestrator.telemetry_events import build_retrieval_activity


# ── fake store: the same eval-seam stand-in as test_retrieval_telemetry.py ─────

class _FakeStore:
    """Minimal VectorStore stand-in for retrieve()'s eval seam (WI-6).

    Two candidates clear the junk floor, one falls below it, so ``candidates``
    is deterministically 2 for any sane global floor. Handles both the 3-tuple
    and 4-tuple (``with_ordinals``) call shapes so the test is independent of
    whether the reranker sidecar happens to be enabled on this box.
    """

    def __init__(self, slug="fake-embed-v1", dims=4, count=1234):
        self.slug = slug
        self.dims = dims
        self.count = count
        self._rows = [
            ("FAKE-1", 0.95, np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32), 0),
            ("FAKE-2", 0.90, np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32), 3),
            ("FAKE-3", 0.10, np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32), 0),  # < floor
        ]

    def search_with_vectors(self, qv, n, allowed, with_ordinals=False):
        if with_ordinals:
            return list(self._rows)
        return [(sid, cos, vec) for (sid, cos, vec, _o) in self._rows]


_QV = [0.1, 0.2, 0.3, 0.4]


def _drive_retrieve(telemetry):
    """One canonical retrieve() call over the fake store. ``telemetry`` may be
    None (no sink) or a dict (populate-in-place). Everything else is fixed, so
    the ONLY difference between two calls is the presence of the sink."""
    return retrieve(
        query="grocery pricing tiers",
        query_vector=list(_QV),
        store=_FakeStore(),
        include_keyword=False,   # semantic-only: no volume/keyword I/O
        telemetry=telemetry,
    )


# ── Invariant 1: the telemetry path is POPULATE-ONLY (zero behavior change) ────

def test_retrieve_output_identical_with_and_without_telemetry():
    """retrieve() WITHOUT a sink vs WITH ``telemetry={}`` must return
    byte-identical results — populating telemetry cannot alter ranking output.

    Hermetic: fake store + supplied query_vector + include_keyword=False, so no
    provider, network, or volume read participates. The two calls differ ONLY in
    whether the opt-in sink is passed."""
    without = _drive_retrieve(None)
    sink: dict = {}
    with_sink = _drive_retrieve(sink)

    assert with_sink == without, (
        "telemetry sink altered retrieval output — the Signal must be "
        f"populate-only. without={without!r} with={with_sink!r}"
    )
    # And the sink WAS populated (guards against a no-op that would make the
    # identity assertion vacuous).
    assert sink.get("embed_model") == "fake-embed-v1"
    assert sink.get("candidates") == 2
    assert sink.get("mmr_topk") == len(with_sink)


# ── Invariant 2a: telemetry strings do NOT leak into retrieve()'s output ───────

def test_telemetry_strings_absent_from_retrieve_output():
    """The sink's string values and the rendered ``system_activity`` labels must
    not appear anywhere in retrieve()'s delivered result payload (snap ids +
    scores). Hermetic: fake store, real build_retrieval_activity renderer."""
    sink: dict = {}
    results = _drive_retrieve(sink)
    labels = build_retrieval_activity(sink)
    assert labels, "renderer produced no events for a populated retrieval sink"

    delivered = repr(results)  # snap_ids + scores — the whole delivered payload
    for key, val in sink.items():
        if isinstance(val, str):
            assert val not in delivered, (
                f"telemetry value {key}={val!r} leaked into retrieve() output"
            )
    for ev in labels:
        label = ev["data"]["label"]
        assert label not in delivered, (
            f"rendered system_activity label {label!r} leaked into output"
        )


# ── build_fossil_context: hermetic fakes for its four channels ────────────────

def _fake_snap(sid: str, body: str) -> str:
    """A minimal but real-shaped snapshot block: a START marker extract_snap_ids
    can parse, plain body text (NO telemetry-label characters), an END marker.
    Bodies deliberately avoid '·', '→', 'embed', 'brain', 'sig-' etc. so a leak
    would be unambiguous."""
    return (
        f"=== START SNAPSHOT - UTC 2026-07-13T00:00:00Z - {sid} ===\n"
        f"{body}\n"
        f"=== END SNAPSHOT - {sid} - UTC 2026-07-13T00:00:00Z ==="
    )


_RECENT = _fake_snap("SNAP-20260713-0001", "Discussion of the grocery store checkout flow.")
_KEYWORD = _fake_snap("SNAP-20260713-0002", "Notes on the weekly produce delivery schedule.")
_SEMANTIC = _fake_snap("SNAP-20260713-0003", "Pricing tier options and the loyalty program.")
_CHECKPT = _fake_snap("SNAP-20260713-0004", "Session summary covering the storefront redesign.")


def _patch_channels(monkeypatch, semantic_fills_telemetry: bool):
    """Monkeypatch every I/O / provider channel build_fossil_context touches so
    the assembly runs hermetically and deterministically. Patched in the
    context_builder namespace (where the names are bound)."""
    import Orchestrator.context_builder as cb

    monkeypatch.setattr(cb, "read_text_safe", lambda *a, **k: "")
    monkeypatch.setattr(cb, "get_recent_media_artifacts", lambda *a, **k: [])
    monkeypatch.setattr(cb, "get_recent_fossils_for_operator", lambda *a, **k: [_RECENT])
    monkeypatch.setattr(cb, "keyword_retrieve_for_operator", lambda *a, **k: [_KEYWORD])
    monkeypatch.setattr(cb, "get_recent_checkpoints_for_operator", lambda *a, **k: [_CHECKPT])

    def _fake_semantic(query, operator="", k=15, threshold=0.60, *,
                       window_budget_chars=None, telemetry=None):
        # Mirror the real shim: thread the retrieval-stage metrics into the sink
        # exactly as retrieve() would, but with fixed values (no embeddings).
        if telemetry is not None and semantic_fills_telemetry:
            telemetry["embed_model"] = "sig-embed-v1"
            telemetry["embed_dims"] = 4
            telemetry["corpus_count"] = 1234
            telemetry["candidates"] = 2
            telemetry["rerank_enabled"] = False
            telemetry["mmr_topk"] = 1
        return [_SEMANTIC]

    monkeypatch.setattr(cb, "semantic_retrieve", _fake_semantic)
    return cb


# ── Invariant 1 (context): fossil_context identical with vs without telemetry ──

def test_fossil_context_identical_with_and_without_telemetry(monkeypatch):
    """The delivered fossil-context STRING (what the LLM actually sees) must be
    byte-identical whether or not a telemetry sink is threaded through — the
    sink is filled in place and never contributes to the prompt text.

    Hermetic: all four retrieval channels + the volume read + the media lookup
    are monkeypatched to fixed fakes, so build_fossil_context is deterministic."""
    from Orchestrator.context_builder import build_fossil_context

    _patch_channels(monkeypatch, semantic_fills_telemetry=True)
    kwargs = dict(user_text="grocery pricing tiers", operator="system",
                  provider="openai")

    ctx_without, _prov0 = build_fossil_context(**kwargs, telemetry=None)
    sink: dict = {}
    ctx_with, _prov1 = build_fossil_context(**kwargs, telemetry=sink)

    assert ctx_with == ctx_without, (
        "telemetry sink changed the delivered fossil_context — the Signal must "
        "never touch prompt text"
    )
    # Sink was genuinely populated (both retrieval-stage + context-assembly keys).
    assert sink.get("embed_model") == "sig-embed-v1"
    assert "memories" in sink, "context-assembly metrics were not recorded"


# ── Invariant 2b (STRONG): no telemetry string leaks into delivered context ────

def test_telemetry_labels_absent_from_delivered_context(monkeypatch):
    """None of the sink's string values, nor any rendered ``system_activity``
    label build_retrieval_activity produces, may appear anywhere in the
    delivered fossil_context. This is the real leak check: fossil_context is the
    exact text handed to the model.

    The sink is seeded with the model/provider/window keys build_streaming_context
    contributes upstream, then build_fossil_context fills the rest — so the
    rendered labels exercise the full brain/embed/search/mmr/context set."""
    from Orchestrator.context_builder import build_fossil_context

    _patch_channels(monkeypatch, semantic_fills_telemetry=True)

    # Seed the model-scope keys build_streaming_context adds (distinctive
    # sentinels so any leak is unambiguous), then let build_fossil_context fill
    # the retrieval + context-assembly keys.
    sink: dict = {"provider": "sigprovider", "model": "sigbrain-max",
                  "window_tokens": 240000}
    fossil_context, _prov = build_fossil_context(
        user_text="grocery pricing tiers", operator="system",
        provider="openai", telemetry=sink,
    )

    labels = build_retrieval_activity(sink)
    assert labels, "renderer produced no events for a fully populated sink"
    # Sanity: the full label set (incl. the brain line) actually rendered.
    stages = {ev["data"]["stage"] for ev in labels}
    assert {"resolve_model", "embed", "search", "mmr"}.issubset(stages)

    for key, val in sink.items():
        if isinstance(val, str):
            assert val not in fossil_context, (
                f"telemetry value {key}={val!r} leaked into the delivered "
                "fossil_context (prompt text)"
            )
    for ev in labels:
        label = ev["data"]["label"]
        assert label not in fossil_context, (
            f"rendered system_activity label {label!r} leaked into the "
            "delivered fossil_context (prompt text)"
        )


# ── Invariant 3: source-level guard — SSE builder stays out of the builders ────

def _read_source(rel_parts):
    """Read an Orchestrator source file by path parts relative to the package
    root. Returns None (test skips that file) if the path does not exist, so the
    guard is robust to future layout changes."""
    pkg_root = Path(__file__).resolve().parent.parent  # Orchestrator/
    path = pkg_root.joinpath(*rel_parts)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def test_signal_builder_absent_from_prompt_and_snapshot_builders():
    """``build_retrieval_activity`` / ``system_activity`` must appear ONLY on the
    SSE emit path (chat_routes.py), NEVER in the prompt-context builder
    (context_builder.py) or the snapshot/ledger builders (checkpoint.py,
    fossils.py). If a future edit routes a rendered activity label into one of
    those, this turns red.

    Pure source-substring guard — no imports, no execution, fully hermetic."""
    builders = [
        ("context_builder.py",),
        ("checkpoint.py",),
        ("fossils.py",),
    ]
    checked = 0
    for parts in builders:
        src = _read_source(parts)
        if src is None:
            continue  # layout changed — skip gracefully rather than false-fail
        checked += 1
        assert "build_retrieval_activity" not in src, (
            f"{os.path.join(*parts)} references build_retrieval_activity — the "
            "UI-only SSE builder must not touch a prompt/snapshot builder"
        )
        assert "system_activity" not in src, (
            f"{os.path.join(*parts)} references system_activity — the UI-only "
            "telemetry event must not touch a prompt/snapshot builder"
        )
    assert checked >= 1, "no builder source files were found to guard"


def test_signal_builder_present_on_the_sse_emit_path():
    """Positive control: the SSE emit path DOES reference the builder, so the
    guard above is meaningful (the symbol exists and lives where it should)."""
    src = _read_source(("routes", "chat_routes.py"))
    assert src is not None, "chat_routes.py not found"
    assert "build_retrieval_activity" in src, (
        "chat_routes.py no longer references build_retrieval_activity — the SSE "
        "emit path is where the Signal is supposed to be rendered"
    )
