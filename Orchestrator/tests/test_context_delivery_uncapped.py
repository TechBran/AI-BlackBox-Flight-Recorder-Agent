"""WI-10 (M7): delivery-cap removal — every delivered snapshot arrives WHOLE.

Brandon's directive (audit §5 decision 6): caps exist ONLY at the embedding
layer; model-bound context is governed by the config.ini COUNT knobs; every
delivered snapshot arrives WHOLE. What remains is a WINDOW SAFETY GUARD:
per-provider token budgets (M3 audit table) that — if ever exceeded — drop
whole LOWEST-RANKED snapshots (never mid-snapshot truncation) and log it.

All tests hermetic (monkeypatched retrievers, synthetic budgets).
"""
import asyncio
import importlib.util
import types
from pathlib import Path

from Orchestrator import context_builder as cb


# --- fixtures (same shape as test_context_builder_backfill) -------------------

A = "SNAP-20260703-0001"
B = "SNAP-20260703-0002"
C = "SNAP-20260703-0003"
D = "SNAP-20260703-0004"


def _blk(sid: str, body: str = "body") -> str:
    return f"=== START SNAPSHOT — UTC 2026-07-03T00:00:00Z — {sid} ===\n{body}\n"


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


def _patch_builder(monkeypatch, recent_blocks, keyword_blocks, semantic_blocks,
                   checkpoint_blocks=(), **cfg_overrides):
    """Patch the four retrievers with pre-built BLOCKS (rank-honoring [:k])."""
    monkeypatch.setattr(cb, "CFG", _FakeCFG(**cfg_overrides))
    monkeypatch.setattr(cb, "read_text_safe", lambda p: "")
    monkeypatch.setattr(cb, "get_recent_media_artifacts", lambda op, limit=10: [])
    recorded = {}

    def _recent(vol, op, n, cap):
        recorded["recent_cap"] = cap
        return list(recent_blocks)[:n]

    monkeypatch.setattr(cb, "get_recent_fossils_for_operator", _recent)
    monkeypatch.setattr(cb, "keyword_retrieve_for_operator",
                        lambda vol, q, k, op: list(keyword_blocks)[:k])
    monkeypatch.setattr(cb, "semantic_retrieve",
                        lambda q, operator="", k=15, threshold=0.60,
                        window_budget_chars=None:
                        list(semantic_blocks)[:k])
    monkeypatch.setattr(cb, "get_recent_checkpoints_for_operator",
                        lambda vol, op, count=1: list(checkpoint_blocks)[:count])
    return recorded


# --- whole-snapshot delivery (cloud) ------------------------------------------

class TestCloudDeliveryUncapped:

    def test_30k_fossil_reaches_assembled_context_intact(self, monkeypatch, capsys):
        """A 30k-char snapshot — over EVERY historical cap (8k max_fossil_chars,
        10k search cap, 15k history cut) — arrives WHOLE for a cloud provider."""
        big_body = "FACT-" + "x" * 30000 + "-ENDFACT"
        _patch_builder(monkeypatch,
                       recent_blocks=[_blk(A, big_body)],
                       keyword_blocks=[], semantic_blocks=[])
        text, prov = cb.build_fossil_context("q", "TestOp", provider="anthropic")
        assert big_body in text                       # intact, byte-for-byte
        assert "[Context truncated" not in text
        assert prov["recent"] == [A]
        out = capsys.readouterr().out
        assert "window guard dropped" not in out
        assert "delivered WHOLE" in out               # the D4 evidence log line

    def test_cloud_passes_uncapped_recent_fetch(self, monkeypatch):
        """CLOUD delivery no longer consumes [context] max_fossil_chars — the
        recent channel is fetched with cap=None (whole snapshots)."""
        recorded = _patch_builder(monkeypatch,
                                  recent_blocks=[_blk(A)],
                                  keyword_blocks=[], semantic_blocks=[])
        cb.build_fossil_context("q", "TestOp", provider="gemini")
        assert recorded["recent_cap"] is None

    def test_every_cloud_chat_provider_has_a_window_budget(self):
        for p in ("anthropic", "openai", "gemini", "google", "xai", "grok",
                  "computer-use"):
            assert cb.window_guard_budget_tokens(p) == cb.PROVIDER_WINDOW_GUARD_TOKENS[p]
        # unknown/absent provider → conservative default
        assert cb.window_guard_budget_tokens("mystery") == cb.DEFAULT_WINDOW_GUARD_TOKENS
        assert cb.window_guard_budget_tokens(None) == cb.DEFAULT_WINDOW_GUARD_TOKENS
        # case-insensitive
        assert cb.window_guard_budget_tokens("Anthropic") == cb.PROVIDER_WINDOW_GUARD_TOKENS["anthropic"]

    def test_computer_use_budget_reflects_the_binding_gemini_cu_window(self):
        """131,072-token gemini-CU window minus output+loop headroom — the ONE
        cloud window where the guard can genuinely bind (M3 audit §6)."""
        assert cb.PROVIDER_WINDOW_GUARD_TOKENS["computer-use"] == 131_072 - 65_536 - 16_384


# --- window safety guard -------------------------------------------------------

class TestWindowGuard:

    def test_guard_drops_lowest_ranked_whole_snapshot_and_logs(self, monkeypatch, capsys):
        """At a synthetic tiny budget the guard drops the LOWEST-ranked keyword
        snapshot WHOLE; survivors stay byte-intact; provenance reflects
        delivery; the drop is logged."""
        huge = _blk(C, "z" * 2000)          # ~1000 floor-tokens — the budget buster
        small_b = _blk(B, "keyword-b")
        small_d = _blk(D, "semantic-d")
        _patch_builder(monkeypatch,
                       recent_blocks=[_blk(A, "recent-a")],
                       keyword_blocks=[small_b, huge],   # C is keyword rank 2 (lowest)
                       semantic_blocks=[small_d])
        monkeypatch.setitem(cb.PROVIDER_WINDOW_GUARD_TOKENS, "anthropic", 300)
        text, prov = cb.build_fossil_context("q", "TestOp", provider="anthropic")
        # C dropped whole — not truncated into the context
        assert C not in text and "z" * 50 not in text
        # survivors delivered WHOLE
        assert "recent-a" in text and "keyword-b" in text and "semantic-d" in text
        assert prov["keyword"] == [B]
        assert prov["recent"] == [A] and prov["semantic"] == [D]
        out = capsys.readouterr().out
        assert f"window guard dropped {C} (keyword" in out
        assert "window guard summary: dropped 1 whole snapshot(s)" in out

    def test_guard_never_truncates_mid_snapshot(self, monkeypatch):
        """Even when the guard must drop several items, everything that remains
        is byte-intact — there is no partial snapshot anywhere."""
        blocks = [_blk(sid, f"payload-{sid}-" + "y" * 400) for sid in (A, B, C, D)]
        _patch_builder(monkeypatch,
                       recent_blocks=[blocks[0]],
                       keyword_blocks=[blocks[1], blocks[2]],
                       semantic_blocks=[blocks[3]])
        monkeypatch.setitem(cb.PROVIDER_WINDOW_GUARD_TOKENS, "anthropic", 350)
        text, prov = cb.build_fossil_context("q", "TestOp", provider="anthropic")
        delivered = prov["recent"] + prov["keyword"] + prov["semantic"]
        for i, sid in enumerate((A, B, C, D)):
            if sid in delivered:
                assert blocks[i] .strip() in text     # whole block present
            else:
                assert f"payload-{sid}" not in text   # dropped block fully absent
        assert "[Context truncated" not in text       # char-truncation is gone

    def test_guard_drop_order_keyword_then_semantic_then_recent(self, monkeypatch, capsys):
        _patch_builder(monkeypatch,
                       recent_blocks=[_blk(A, "r" * 300)],
                       keyword_blocks=[_blk(B, "k" * 300)],
                       semantic_blocks=[_blk(C, "s" * 300)])
        monkeypatch.setitem(cb.PROVIDER_WINDOW_GUARD_TOKENS, "anthropic", 1)
        text, prov = cb.build_fossil_context("q", "TestOp", provider="anthropic")
        out = capsys.readouterr().out
        i_b = out.index(f"window guard dropped {B} (keyword")
        i_c = out.index(f"window guard dropped {C} (semantic")
        i_a = out.index(f"window guard dropped {A} (recent")
        assert i_b < i_c < i_a
        assert prov["recent"] == prov["keyword"] == prov["semantic"] == []


# --- local profile: UNCHANGED (M8's domain) ------------------------------------

class TestLocalProfileUnchanged:

    def test_local_still_char_capped_with_truncation_suffix(self, monkeypatch):
        big = [_blk(f"SNAP-20260703-010{i}", "x" * 4000) for i in range(5)]
        _patch_builder(monkeypatch,
                       recent_blocks=[], keyword_blocks=[], semantic_blocks=big)
        text, _ = cb.build_fossil_context(
            "q", "TestOp", provider="local",
            semantic_k=5, checkpoint_count=0,
            include_recent=False, include_keyword=False, include_media=False,
        )
        suffix = "\n\n[Context truncated for token budget]"
        assert text.endswith(suffix)
        assert len(text) == cb.PROVIDER_CAPS["local"] + len(suffix)

    def test_local_cap_value_unchanged(self):
        assert cb.PROVIDER_CAPS["local"] == 16000


# --- anthropic history path: window guard replaces the 15k blanket cut ---------

class TestAnthropicHistoryGuard:

    def _cr(self):
        from Orchestrator.routes import chat_routes as cr
        return cr

    def test_20k_char_history_message_survives_whole(self):
        """The old truncate_large_content cut every non-system message at 15k.
        Now a 20k-char message passes through UNTOUCHED under the real budget."""
        cr = self._cr()
        long_text = "PASTE-" + "q" * 20000 + "-END"
        amsgs = [
            {"role": "user", "content": long_text},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "and now?"},
        ]
        guarded, dropped = cr._guard_anthropic_history(amsgs, "system prompt")
        assert dropped == 0
        assert guarded[0]["content"] == long_text     # whole, byte-for-byte

    def test_tiny_budget_drops_oldest_whole_messages(self, monkeypatch, capsys):
        cr = self._cr()
        monkeypatch.setitem(cb.PROVIDER_WINDOW_GUARD_TOKENS, "anthropic", 50)
        amsgs = [
            {"role": "user", "content": "old " * 100},        # ~200 tokens
            {"role": "assistant", "content": "older reply " * 50},
            {"role": "user", "content": "current turn"},
        ]
        guarded, dropped = cr._guard_anthropic_history(amsgs, "sys")
        assert dropped == 2
        assert guarded == [{"role": "user", "content": "current turn"}]
        assert "window guard dropped 2 oldest whole history message(s)" in capsys.readouterr().out
        # input list not mutated
        assert len(amsgs) == 3

    def test_final_message_never_dropped_and_first_is_user(self, monkeypatch):
        cr = self._cr()
        monkeypatch.setitem(cb.PROVIDER_WINDOW_GUARD_TOKENS, "anthropic", 1)
        amsgs = [
            {"role": "user", "content": "a" * 500},
            {"role": "assistant", "content": "b" * 500},
            {"role": "user", "content": "the current turn " * 50},  # itself over budget
        ]
        guarded, _ = cr._guard_anthropic_history(amsgs, "sys")
        assert len(guarded) == 1
        assert guarded[0]["role"] == "user"           # anthropic requires user-first
        assert guarded[0]["content"] == amsgs[-1]["content"]


# --- search_snapshots executor: 10k cap removed ---------------------------------

class TestSearchSnapshotsExecutorUncapped:

    def test_over_10k_result_delivered_whole(self, monkeypatch):
        repo = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location(
            "search_snapshots_executor",
            repo / "ToolVault" / "tools" / "search_snapshots" / "executor.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        big = _blk(A, "M" * 25000)
        import Orchestrator.fossils as fossils
        monkeypatch.setattr(fossils, "hybrid_retrieve",
                            lambda vol, q, k=5, operator="",
                            window_budget_chars=None: [big])
        # caller-less ctx = a cloud/box surface -> WHOLE delivery (the M8
        # on-device bound keys on ctx.caller == "local" only).
        ctx = types.SimpleNamespace(operator="TestOp")
        res = asyncio.run(mod.execute({"query": "anything"}, ctx))
        assert res.success
        assert big in res.result                      # whole snapshot in the tool result
        assert "[truncated]" not in res.result


# --- CU context: cap-free + guarded ---------------------------------------------

class TestCuContextUncapped:

    def _patch_cu(self, monkeypatch, recent_blocks, keyword_blocks, semantic_blocks):
        from Orchestrator.routes import chat_routes as cr
        recorded = {}

        def _recent(vol, op, n, cap):
            recorded["recent_cap"] = cap
            return list(recent_blocks)[:n]

        monkeypatch.setattr(cr, "read_text_safe", lambda p: "")
        monkeypatch.setattr(cr, "get_recent_media_artifacts", lambda op, limit=10: [])
        monkeypatch.setattr(cr, "get_recent_fossils_for_operator", _recent)
        monkeypatch.setattr(cr, "keyword_retrieve_for_operator",
                            lambda vol, q, k, op: list(keyword_blocks)[:k])
        monkeypatch.setattr(cr, "semantic_retrieve",
                            lambda q, operator="", k=15, threshold=0.60:
                            list(semantic_blocks)[:k])
        monkeypatch.setattr(cr, "get_recent_checkpoints_for_operator",
                            lambda vol, op, count=1: [])
        return cr, recorded

    def test_cu_recent_fetch_uncapped_and_big_snapshot_whole(self, monkeypatch):
        big_body = "CUFACT-" + "w" * 20000
        cr, recorded = self._patch_cu(monkeypatch,
                                      recent_blocks=[_blk(A, big_body)],
                                      keyword_blocks=[], semantic_blocks=[])
        text, prov = cr.build_cu_context("q", "TestOpNotConfigured")
        assert recorded["recent_cap"] is None         # old CU_CAP=10000 is gone
        assert big_body in text
        assert prov["recent"] == [A]

    def test_cu_window_guard_drops_whole_at_tiny_budget(self, monkeypatch, capsys):
        cr, _ = self._patch_cu(monkeypatch,
                               recent_blocks=[_blk(A, "cu-recent")],
                               keyword_blocks=[_blk(B, "v" * 2000)],
                               semantic_blocks=[_blk(C, "cu-sem")])
        monkeypatch.setitem(cb.PROVIDER_WINDOW_GUARD_TOKENS, "computer-use", 200)
        text, prov = cr.build_cu_context("q", "TestOpNotConfigured")
        assert B not in text and "v" * 50 not in text  # dropped WHOLE
        assert "cu-recent" in text and "cu-sem" in text
        assert prov["keyword"] == []
        assert f"window guard dropped {B} (keyword" in capsys.readouterr().out
