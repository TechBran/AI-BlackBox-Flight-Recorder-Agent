"""M15.2: build_fossil_context (and the CU context) deliver BODY-ONLY snapshots.

The bookkeeping envelope (=== START SNAPSHOT === / CROSS-FILE BEACON / VOLUME
TRACKER / GAUGES / Kernel Index) is ~1,000 chars/snapshot the MODEL can't use —
measured ~5,000 tokens (~8%) of a ~19-snapshot turn. Since M15.2 the context
assembler formats each rendered block via fossils.format_snapshot_for_delivery:
a compact [SNAP-id · date · operator] attribution + Context Provenance + the Raw
Session Log, dropping the envelope. The `snaps` lists stay whole so the window
guard / provenance / attribution keep working; the guard now caps CLEANER text.

Hermetic (monkeypatched retrievers, synthetic budgets).
"""
from Orchestrator import context_builder as cb


A = "SNAP-20260703-0001"
B = "SNAP-20260703-0002"
C = "SNAP-20260703-0003"


def _full_blk(sid: str, op: str = "Anna", user: str = "how do I add a reranker?",
              assistant: str = "add a provider abstraction in rerank.py.") -> str:
    """A realistic snapshot: full bookkeeping envelope + provenance + session log."""
    return (
        f"=== START SNAPSHOT — UTC 2026-07-03T00:00:00Z — {sid} (7.1.0) ===\n"
        "CROSS-FILE BEACON\n"
        "==============================================================\n"
        "Tail-first sweep resolved tip = SNAP-20260703-0000\n"
        f"COUNT=1 | TARGET_ID={sid}\n"
        "UFL: OUTSIDE-JUNK IGNORED | BYTES_AFTER_END=0 | BYTES_BEFORE_START=0\n"
        "Result: Tail lock confirmed\n"
        "==============================================================\n\n"
        "VOLUME TRACKER\n"
        "Tail: SNAP-20260703-0000\n"
        "Mode: NORMAL\n\n"
        "GAUGES\n"
        "CONTINUITY: TURNS\n"
        "TOKENS (since last mint): prompt=54170, completion=2155, total=56325\n"
        "MODEL: gemini-3.5-flash\n"
        f"OPERATOR: {op}\n"
        "MODE: Normal\n\n"
        "SNAPSHOT BODY\n\n"
        "Kernel Index\n"
        f"- Current: {sid}\n"
        "- Volume: Appliance/Overseer\n\n"
        "Context Provenance\n"
        "- GM_EXCERPT: yes\n"
        "- Recent fossils: SNAP-20260703-0000\n\n"
        "Raw Session Log\n"
        f"- [1] 2026-07-03T00:00:00Z operator={op} user: {user}\n"
        f"- [2] 2026-07-03T00:00:00Z operator={op} assistant: {assistant}\n"
        f"=== END SNAPSHOT — {sid} ===\n"
    )


class _FakeCFG:
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


def _patch_builder(monkeypatch, recent_blocks=(), keyword_blocks=(),
                   semantic_blocks=(), checkpoint_blocks=(), **cfg_overrides):
    monkeypatch.setattr(cb, "CFG", _FakeCFG(**cfg_overrides))
    monkeypatch.setattr(cb, "read_text_safe", lambda p: "")
    monkeypatch.setattr(cb, "get_recent_media_artifacts", lambda op, limit=10: [])
    monkeypatch.setattr(cb, "get_recent_fossils_for_operator",
                        lambda vol, op, n, cap: list(recent_blocks)[:n])
    monkeypatch.setattr(cb, "keyword_retrieve_for_operator",
                        lambda vol, q, k, op: list(keyword_blocks)[:k])
    monkeypatch.setattr(cb, "semantic_retrieve",
                        lambda q, operator="", k=15, threshold=0.60,
                        window_budget_chars=None: list(semantic_blocks)[:k])
    monkeypatch.setattr(cb, "get_recent_checkpoints_for_operator",
                        lambda vol, op, count=1: list(checkpoint_blocks)[:count])


def test_assembled_context_is_body_only_with_attribution(monkeypatch):
    _patch_builder(monkeypatch, semantic_blocks=[_full_blk(A)])
    text, prov = cb.build_fossil_context("how do I add a reranker?", "Anna",
                                         provider="anthropic")
    # Attribution header present — the model still knows the source snapshot.
    assert f"[{A}" in text
    assert "operator: Anna" in text
    # Content delivered.
    assert "how do I add a reranker?" in text
    assert "add a provider abstraction in rerank.py." in text
    assert "Context Provenance" in text
    # Envelope bookkeeping stripped.
    assert "CROSS-FILE BEACON" not in text
    assert "VOLUME TRACKER" not in text
    assert "GAUGES" not in text
    assert "Kernel Index" not in text
    assert "BYTES_AFTER_END" not in text
    # Provenance dict shape unchanged (id still resolved from the whole block).
    assert prov["semantic"] == [A]
    assert set(prov) == {"recent", "keyword", "semantic", "checkpoint"}


def test_delivery_char_count_drops_vs_whole_envelope(monkeypatch):
    blocks = [_full_blk(A), _full_blk(B), _full_blk(C)]
    _patch_builder(monkeypatch, semantic_blocks=blocks)
    text, _ = cb.build_fossil_context("q", "Anna", provider="anthropic")
    # The three raw envelopes summed dwarf the delivered body-only context.
    raw_total = sum(len(b) for b in blocks)
    assert len(text) < raw_total
    # Every snapshot's session log survives (nothing dropped at this budget).
    assert text.count("Raw Session Log") == 3
    assert "CROSS-FILE BEACON" not in text


def test_all_four_sections_formatted(monkeypatch):
    _patch_builder(monkeypatch,
                   recent_blocks=[_full_blk(A)],
                   keyword_blocks=[_full_blk(B)],
                   semantic_blocks=[_full_blk(C)],
                   checkpoint_blocks=[_full_blk("SNAP-20260703-0009")])
    text, _ = cb.build_fossil_context("q", "Anna", provider="anthropic")
    # No section leaks the envelope.
    assert "CROSS-FILE BEACON" not in text
    assert "GAUGES" not in text
    # All four ids attributed.
    for sid in (A, B, C, "SNAP-20260703-0009"):
        assert f"[{sid}" in text


def test_whole_delivery_of_markerless_blocks_unaffected(monkeypatch):
    # A block WITHOUT the content markers (e.g. legacy / synthetic) passes
    # through format unchanged — never-worse contract; the payload is intact.
    plain = "=== START SNAPSHOT — UTC 2026-07-03T00:00:00Z — " + A + " ===\nPAYLOAD-INTACT\n"
    _patch_builder(monkeypatch, recent_blocks=[plain])
    text, prov = cb.build_fossil_context("q", "Anna", provider="anthropic")
    assert "PAYLOAD-INTACT" in text
    assert prov["recent"] == [A]


class TestCuContextBodyOnly:
    def _patch_cu(self, monkeypatch, recent=(), keyword=(), semantic=(), checkpoint=()):
        from Orchestrator.routes import chat_routes as cr
        monkeypatch.setattr(cr, "read_text_safe", lambda p: "")
        monkeypatch.setattr(cr, "get_recent_media_artifacts", lambda op, limit=10: [])
        monkeypatch.setattr(cr, "get_recent_fossils_for_operator",
                            lambda vol, op, n, cap: list(recent)[:n])
        monkeypatch.setattr(cr, "keyword_retrieve_for_operator",
                            lambda vol, q, k, op: list(keyword)[:k])
        monkeypatch.setattr(cr, "semantic_retrieve",
                            lambda q, operator="", k=15, threshold=0.60: list(semantic)[:k])
        monkeypatch.setattr(cr, "get_recent_checkpoints_for_operator",
                            lambda vol, op, count=1: list(checkpoint)[:count])
        return cr

    def test_cu_context_body_only(self, monkeypatch):
        cr = self._patch_cu(monkeypatch,
                            semantic=[_full_blk(A)],
                            checkpoint=[_full_blk(B)])
        text, prov = cr.build_cu_context("how do I add a reranker?", "Anna")
        assert "add a provider abstraction in rerank.py." in text
        assert f"[{A}" in text
        assert "CROSS-FILE BEACON" not in text
        assert "VOLUME TRACKER" not in text
        assert "GAUGES" not in text
        assert prov["semantic"] == [A]
        assert prov["checkpoint"] == [B]
