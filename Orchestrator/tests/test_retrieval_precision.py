"""Precision levers for the canonical retriever (plan 2026-07-07).

Unit-pins the NEW behavior added behind flags — keyword_mode (fused|dedup|
gated|off), the output cosine floor + min_results underflow guard, and the
rerank preserve-score + absolute floor — all hermetic (no network/store).
Byte-identical-default is already pinned by test_retrieval_rerank.py's
flag-off tests + the whole existing suite; here we assert the levers DO their
job when switched on.
"""
import contextlib

import numpy as np
import pytest

import Orchestrator.retrieval as retrieval
from Orchestrator import config as _config
from Orchestrator import rerank as rerank_mod
from Orchestrator.config import CFG

# q = [1,0,0,0]; cosines: A≈0.70, B=0.65, C=0.60 (all ≥ junk_floor 0.40),
# KW1≈0.30 (BELOW the floor -> keyword-only, never a semantic candidate).
ROWS = [
    ("A", [0.70, 0.714, 0.0, 0.0]),
    ("B", [0.65, 0.0, 0.76, 0.0]),
    ("C", [0.60, 0.0, 0.0, 0.80]),
    ("KW1", [0.30, 0.954, 0.0, 0.0]),
]
IDX = {sid: {"operator": "alice", "timestamp": "T0", "text": f"text of {sid}"}
       for sid in ("A", "B", "C", "KW1")}


class PStore:
    """FakeStore + the max_cosine_for helper the gated lever needs."""

    def __init__(self, rows, slug="test-slug", dims=4):
        self.dims, self.slug, self._rows = dims, slug, []
        for sid, v in rows:
            vec = np.asarray(v, dtype=np.float32)
            n = float(np.linalg.norm(vec))
            self._rows.append((sid, vec / n if n > 0 else vec))

    @property
    def count(self):
        return len(self._rows)

    def _q(self, query_vec):
        q = np.asarray(query_vec, dtype=np.float32)
        n = float(np.linalg.norm(q))
        return q / n if n > 0 else q

    def search_with_vectors(self, query_vec, k, allowed_ids=None, with_ordinals=False):
        q = self._q(query_vec)
        scored = [(sid, float(vec @ q), vec.copy()) for sid, vec in self._rows
                  if allowed_ids is None or sid in allowed_ids]
        scored.sort(key=lambda t: t[1], reverse=True)
        scored = scored[:k]
        return [(s, c, v, None) for s, c, v in scored] if with_ordinals else scored

    def max_cosine_for(self, query_vec, sids):
        q, want = self._q(query_vec), set(sids)
        out = {}
        for sid, vec in self._rows:
            if sid in want:
                out[sid] = max(out.get(sid, -2.0), float(vec @ q))
        return out


@contextlib.contextmanager
def pin_retrieval(**extra):
    base = dict(candidate_n="40", rrf_c="60", recency_weight="0.005",
                recency_half_life_days="90", mmr_lambda="0.85", mmr_protect_top="3",
                junk_floor="0.40", registry_floor_enabled="false", debug_log="false",
                keyword_mode="fused", rerank_relevance="rankspace", rerank_floor="0.0",
                output_cos_floor="0.0", min_results="0", rerank_enabled="false")
    base.update({k: str(v) for k, v in extra.items()})
    if not CFG.has_section("retrieval"):
        CFG.add_section("retrieval")
    saved = {o: (CFG.get("retrieval", o) if CFG.has_option("retrieval", o) else None)
             for o in base}
    try:
        for o, val in base.items():
            CFG.set("retrieval", o, val)
        yield
    finally:
        for o, prev in saved.items():
            if prev is None:
                CFG.remove_option("retrieval", o)
            else:
                CFG.set("retrieval", o, prev)


@pytest.fixture()
def world(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "EMBEDDINGS_STORES_DIR", str(tmp_path / "stores"))
    monkeypatch.setattr(retrieval._emb, "generate_embedding_sync",
                        lambda text, purpose="query": [1.0, 0.0, 0.0, 0.0])
    monkeypatch.setattr(retrieval, "load_snapshot_index", lambda: dict(IDX))
    monkeypatch.setattr(retrieval, "keyword_retrieve_ids_for_operator",
                        lambda vol, q, n, op: ["KW1", "A"])
    monkeypatch.setattr(retrieval, "_decode_snapshot_text", lambda meta: meta.get("text"))
    monkeypatch.setattr(retrieval, "_age_days", lambda ts, now: 0.0)
    monkeypatch.setattr(retrieval, "_rerank_fallthrough_logged", False)
    rerank_mod.reset_preflight()
    yield
    rerank_mod.reset_preflight()


def ids_of(results):
    return [sid for sid, _score in results]


def store():
    return PStore(ROWS)


# ── keyword_mode ──────────────────────────────────────────────────────────────

def test_keyword_off_excludes_keyword_channel(world):
    with pin_retrieval(keyword_mode="off"):
        got = ids_of(retrieve_hermetic())
    assert "KW1" not in got and set(got) == {"A", "B", "C"}


def test_keyword_dedup_drops_keyword_only(world):
    # KW1 is keyword-only (below junk floor) -> dedup never lets it inject.
    with pin_retrieval(keyword_mode="dedup"):
        got = ids_of(retrieve_hermetic())
    assert "KW1" not in got and set(got) == {"A", "B", "C"}


def test_keyword_fused_DOES_inject_keyword_only(world):
    # Baseline behavior (the bug): fused fuses KW1 into the ranking.
    with pin_retrieval(keyword_mode="fused"):
        got = ids_of(retrieve_hermetic())
    assert "KW1" in got


def test_keyword_gated_drops_low_cosine_keyword_only(world):
    # gated: KW1's true cosine (~0.30) is below the 0.50 gate -> dropped.
    with pin_retrieval(keyword_mode="gated", output_cos_floor="0.50"):
        got = ids_of(retrieve_hermetic())
    assert "KW1" not in got


# ── output cosine floor + variable-k + min_results ────────────────────────────

def test_output_floor_returns_fewer_than_k(world):
    # floor 0.68 keeps only A (0.70); B/C (0.65/0.60) drop -> 1 result, not k.
    with pin_retrieval(keyword_mode="off", output_cos_floor="0.68"):
        got = ids_of(retrieve_hermetic())
    assert got == ["A"]


def test_min_results_guards_against_over_pruning(world):
    with pin_retrieval(keyword_mode="off", output_cos_floor="0.68", min_results="2"):
        got = ids_of(retrieve_hermetic())
    assert set(got) == {"A", "B"} and len(got) == 2  # B restored by the guard


# ── rerank preserve + absolute floor ──────────────────────────────────────────

def test_rerank_preserve_floor_drops_low_score(world, monkeypatch):
    monkeypatch.setattr(rerank_mod, "available", lambda: True)
    # pool order is [A, B, C] (post-recency); score them 0.9 / 0.5 / 0.01.
    monkeypatch.setattr(rerank_mod, "score",
                        lambda q, passages: [0.9, 0.5, 0.01][:len(passages)])
    with pin_retrieval(keyword_mode="off", rerank_enabled="true",
                       rerank_relevance="preserve", rerank_floor="0.1"):
        got = ids_of(retrieve_hermetic())
    assert got == ["A", "B"]          # C (0.01) dropped by the 0.1 floor
    assert "C" not in got


# ── M4: best-window rerank passage ────────────────────────────────────────────

def test_best_window_returns_body_when_short():
    assert retrieval._best_passage_window("short body", "anything", 100) == "short body"


def test_best_window_head_when_no_query_terms():
    b = "a" * 500
    assert retrieval._best_passage_window(b, "  ??  ", 100) == b[:100]


def test_best_window_finds_deep_relevant_span_not_head():
    body = ("generic intro text " * 30) + " THE_ANSWER quorptext marker " + ("filler " * 30)
    win = retrieval._best_passage_window(body, "quorptext answer", width=60)
    assert "quorptext" in win.lower()          # picked the deep span…
    assert not win.startswith("generic intro")  # …not the head-cut


def test_best_window_stays_on_head_when_no_term_hits():
    body = "aaaa " * 200  # body has none of the query terms anywhere
    assert retrieval._best_passage_window(body, "zzzz yyyy", 80) == body[:80]


def retrieve_hermetic():
    return retrieval.retrieve("q", operator="alice", k=10, store=store(),
                              query_vector=[1.0, 0.0, 0.0, 0.0])
