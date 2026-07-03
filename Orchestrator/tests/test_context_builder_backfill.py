"""WI-7b: cross-section dedupe must BACKFILL from each channel's ranked list.

Before this change, dedupe was filter-only: keyword was filtered against
recent, semantic against recent∪keyword — so whenever channels agreed (the
snapshot most worth retrieving!), the later section silently SHRANK. Now each
channel over-fetches by len(seen_so_far) and fills its section with the first
section_k UNSEEN snaps, preserving the channel's own ranking.

Coverage map for the three sectioned context builders (sibling sweep):

1. ``Orchestrator.context_builder.build_fossil_context`` — tested directly
   here (TestBuildFossilContextBackfill).
2. ``chat_routes.build_streaming_context`` — NOT separately re-implemented:
   it delegates all fossil retrieval to ``build_fossil_context`` (see the
   ``from Orchestrator.context_builder import build_fossil_context`` call in
   chat_routes.py), so it is covered by (1). No separate assembly exists to
   test.
3. ``chat_routes.build_cu_context`` — a separate inline assembly (Computer
   Use, reduced counts); tested directly here (TestBuildCuContextBackfill).
4. ``tasks.process_chat_task`` (non-stream worker) — the assembly is inline
   in a worker that is not hermetically callable (spins the full task/LLM
   pipeline). It now uses the SAME shared ``context_builder.fill_unseen``
   helper with the same over-fetch pattern as (1) and (3); the helper's
   behavior is unit-tested directly here (TestFillUnseen).

All retriever fakes HONOR the requested k (``ranked[:k]``) — exactly like the
real ranked retrievers — so these tests fail if an implementation fetches
only section_k and then filters (the old shrinking behavior).
"""
from Orchestrator import context_builder as cb


# --- fixtures ---------------------------------------------------------------

# Readable aliases; START_RX requires the real SNAP-\d{8}-\d+ shape.
A = "SNAP-20260701-0001"
B = "SNAP-20260701-0002"
C = "SNAP-20260701-0003"
D = "SNAP-20260701-0004"
E = "SNAP-20260701-0005"
F = "SNAP-20260701-0006"
G = "SNAP-20260701-0007"
H = "SNAP-20260701-0008"


def _blk(sid: str) -> str:
    """A realistic snapshot block whose START marker matches config.START_RX
    (em-dash separators, real SNAP-ID) so extract_snap_ids parses it the same
    way the production snapshot parser does."""
    return f"=== START SNAPSHOT — UTC 2026-07-01T00:00:00Z — {sid} ===\nbody for {sid}\n"


class _FakeCFG:
    """Hermetic stand-in for the [context] knobs so tests don't depend on
    config.ini values (live box: RF=5 KF=3 SF=6 CP=2)."""

    def __init__(self, **overrides):
        self._ints = {
            "recent_fossils_per_user": 5,
            "keyword_fossils_per_user": 3,
            "semantic_fossils_per_user": 6,
            "checkpoint_snapshots": 2,
            "max_fossil_chars": 10000,
        }
        self._ints.update(overrides)

    def getint(self, section, key, fallback=0):
        return self._ints.get(key, fallback)

    def getfloat(self, section, key, fallback=0.0):
        return fallback


def _patch_builder(monkeypatch, recent, keyword_ranked, semantic_ranked,
                   checkpoints=(), **cfg_overrides):
    """Patch the four retrievers (+ IO) on context_builder with rank-honoring
    fakes: each returns its ranked list truncated to the REQUESTED k, exactly
    like the real retrievers."""
    monkeypatch.setattr(cb, "CFG", _FakeCFG(**cfg_overrides))
    monkeypatch.setattr(cb, "read_text_safe", lambda p: "")
    monkeypatch.setattr(cb, "get_recent_media_artifacts", lambda op, limit=10: [])
    monkeypatch.setattr(cb, "get_recent_fossils_for_operator",
                        lambda vol, op, n, cap: [_blk(s) for s in recent][:n])
    monkeypatch.setattr(cb, "keyword_retrieve_for_operator",
                        lambda vol, q, k, op: [_blk(s) for s in keyword_ranked][:k])
    monkeypatch.setattr(cb, "semantic_retrieve",
                        lambda q, operator="", k=15, threshold=0.60,
                        window_budget_chars=None:
                        [_blk(s) for s in semantic_ranked][:k])
    monkeypatch.setattr(cb, "get_recent_checkpoints_for_operator",
                        lambda vol, op, count=1: [_blk(s) for s in checkpoints][:count])


# --- build_fossil_context ----------------------------------------------------

class TestBuildFossilContextBackfill:

    def test_keyword_section_backfills_after_recent_dedupe(self, monkeypatch):
        # recent returns A,B; keyword ranked list is A,C,D,E (A dupes recent).
        # KF=3 -> the keyword section must deliver C,D,E (3 items), NOT just C,D.
        _patch_builder(monkeypatch,
                       recent=[A, B],
                       keyword_ranked=[A, C, D, E],
                       semantic_ranked=[],
                       keyword_fossils_per_user=3)
        _, prov = cb.build_fossil_context("some query", "TestOp")
        assert prov["recent"] == [A, B]
        assert prov["keyword"] == [C, D, E]

    def test_semantic_backfills_against_recent_and_keyword(self, monkeypatch):
        # keyword section will be C,D (KF=2). Semantic ranking leads with dupes
        # of recent (A) and keyword (C) plus a deep dupe (B) — SF=3 must still
        # deliver 3 distinct: E,F,G.
        _patch_builder(monkeypatch,
                       recent=[A, B],
                       keyword_ranked=[C, D],
                       semantic_ranked=[A, C, E, F, B, G, H],
                       keyword_fossils_per_user=2,
                       semantic_fossils_per_user=3)
        _, prov = cb.build_fossil_context("some query", "TestOp")
        assert prov["keyword"] == [C, D]
        assert prov["semantic"] == [E, F, G]

    def test_no_duplicate_snap_id_across_sections(self, monkeypatch):
        # Heavy overlap across all three discovery channels.
        _patch_builder(monkeypatch,
                       recent=[A, B],
                       keyword_ranked=[B, C, A, D],
                       semantic_ranked=[A, C, E, B, F, G, H],
                       keyword_fossils_per_user=2,
                       semantic_fossils_per_user=3)
        _, prov = cb.build_fossil_context("some query", "TestOp")
        all_ids = prov["recent"] + prov["keyword"] + prov["semantic"]
        assert len(all_ids) == len(set(all_ids)), f"duplicate snap_id across sections: {all_ids}"
        # And sections stay FULL despite the overlap:
        assert prov["keyword"] == [C, D]
        assert prov["semantic"] == [E, F, G]

    def test_channels_exhausted_returns_short_section(self, monkeypatch):
        # keyword channel only knows A,B,C total; A,B dupe recent. KF=3 ->
        # the section comes back short ([C]) — backfill never invents items.
        _patch_builder(monkeypatch,
                       recent=[A, B],
                       keyword_ranked=[A, B, C],
                       semantic_ranked=[],
                       keyword_fossils_per_user=3)
        _, prov = cb.build_fossil_context("some query", "TestOp")
        assert prov["keyword"] == [C]
        assert prov["semantic"] == []

    def test_checkpoints_stay_pinned_and_undeduped(self, monkeypatch):
        # Checkpoints are a pinned section, not a discovery channel: a
        # checkpoint that also appears in recent is NOT deduped away.
        _patch_builder(monkeypatch,
                       recent=[A, B],
                       keyword_ranked=[],
                       semantic_ranked=[],
                       checkpoints=[A, F],
                       checkpoint_snapshots=2)
        _, prov = cb.build_fossil_context("some query", "TestOp")
        assert prov["checkpoint"] == [A, F]
        assert prov["recent"] == [A, B]


# --- fill_unseen (shared helper; also covers the tasks.py worker site) -------

class TestFillUnseen:

    def test_first_k_unseen_preserves_channel_rank(self):
        ranked = [_blk(s) for s in [A, C, D, E]]
        assert cb.fill_unseen(ranked, 3, {A, B}) == [_blk(C), _blk(D), _blk(E)]

    def test_short_when_ranked_list_exhausted(self):
        ranked = [_blk(s) for s in [A, B, C]]
        assert cb.fill_unseen(ranked, 3, {A, B}) == [_blk(C)]

    def test_zero_k_returns_empty(self):
        assert cb.fill_unseen([_blk(A)], 0, set()) == []

    def test_intra_channel_duplicate_collapsed(self):
        ranked = [_blk(s) for s in [C, C, D]]
        assert cb.fill_unseen(ranked, 3, set()) == [_blk(C), _blk(D)]

    def test_does_not_mutate_seen_ids(self):
        seen = {A}
        cb.fill_unseen([_blk(s) for s in [A, B]], 2, seen)
        assert seen == {A}

    def test_idless_snap_treated_as_unseen(self):
        # Matches the historical build_fossil_context filter semantics: a block
        # with no extractable id passes through (kept).
        assert cb.fill_unseen(["no marker here"], 1, {A}) == ["no marker here"]


# --- build_cu_context (Computer Use sibling assembly) -------------------------

class TestBuildCuContextBackfill:

    def _patch_cu(self, monkeypatch, recent, keyword_ranked, semantic_ranked,
                  checkpoints=()):
        from Orchestrator.routes import chat_routes as cr
        monkeypatch.setattr(cr, "read_text_safe", lambda p: "")
        monkeypatch.setattr(cr, "get_recent_media_artifacts", lambda op, limit=10: [])
        monkeypatch.setattr(cr, "get_recent_fossils_for_operator",
                            lambda vol, op, n, cap: [_blk(s) for s in recent][:n])
        monkeypatch.setattr(cr, "keyword_retrieve_for_operator",
                            lambda vol, q, k, op: [_blk(s) for s in keyword_ranked][:k])
        monkeypatch.setattr(cr, "semantic_retrieve",
                            lambda q, operator="", k=15, threshold=0.60:
                            [_blk(s) for s in semantic_ranked][:k])
        monkeypatch.setattr(cr, "get_recent_checkpoints_for_operator",
                            lambda vol, op, count=1: [_blk(s) for s in checkpoints][:count])
        return cr

    def test_cu_sections_backfill_and_never_duplicate(self, monkeypatch):
        # CU knobs are hardcoded: CU_RF=4, CU_KF=3, CU_SF=5.
        # keyword ranked leads with dupes of recent; semantic overlaps both.
        cr = self._patch_cu(monkeypatch,
                            recent=[A, B],
                            keyword_ranked=[A, C, D, B, E],
                            semantic_ranked=[A, C, E, F, G, H, B,
                                             "SNAP-20260701-0009",
                                             "SNAP-20260701-0010",
                                             "SNAP-20260701-0011"])
        # Operator name deliberately not in OPERATOR_PREFERENCES.
        _, prov = cr.build_cu_context("some query", "TestOpNotConfigured")
        assert prov["keyword"] == [C, D, E]                # CU_KF=3, backfilled past A,B
        assert prov["semantic"] == [F, G, H,               # CU_SF=5, backfilled past A,C,E,B
                                    "SNAP-20260701-0009", "SNAP-20260701-0010"]
        all_ids = sorted(prov["recent"]) + prov["keyword"] + prov["semantic"]
        assert len(all_ids) == len(set(all_ids))
