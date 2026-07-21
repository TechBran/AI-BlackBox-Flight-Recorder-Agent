"""localstack rerank must serialize behind an open on-box voice session (D12).

Two layers are covered:
  * the PRODUCTION path — the sync ``rerank.score()`` dispatcher (runs in the
    FastAPI threadpool) routes ``localstack`` through the blocking gate; a voice
    session held open blocks it to a timeout -> ``None`` (un-reranked), and once
    the session releases the real dispatch runs;
  * the async ``_score_localstack`` wrapper — the future async entry — which must
    likewise time out to ``None`` while held and run the dispatch once open, and
    never raise (a dead reranker costs latency, never recall — §6).
"""
import asyncio

from Orchestrator import local_stack
from Orchestrator import rerank


# ── production sync path: score() dispatch is gated ───────────────────────────

def test_score_localstack_returns_none_while_voice_held(monkeypatch):
    """score()->localstack blocks on the sync gate and degrades to None (never
    firing the :9098 dispatch) while an on-box voice stream is open."""
    monkeypatch.setattr(local_stack, "RETRIEVAL_GATE_TIMEOUT_S", 0.1)
    called = []
    monkeypatch.setattr(rerank, "_do_localstack_rerank",
                        lambda q, p, s: called.append(1) or [0.9, 0.1])
    monkeypatch.setattr(rerank, "get_settings", lambda: {"provider": "localstack"})
    # An on-box voice stream is open -> the retrieval group is gated.
    monkeypatch.setattr(local_stack, "_voice_depth", 1)

    assert rerank.score("q", ["doc a", "doc b"]) is None  # gate timed out
    assert called == []                                   # dispatch never fired


def test_score_localstack_runs_dispatch_when_gate_open(monkeypatch):
    """With no voice stream open the gate passes through immediately and score()
    returns the real dispatch's scores — proves the gate actually RELEASES."""
    monkeypatch.setattr(local_stack, "RETRIEVAL_GATE_TIMEOUT_S", 0.1)
    monkeypatch.setattr(rerank, "_do_localstack_rerank", lambda q, p, s: [0.9, 0.1])
    monkeypatch.setattr(rerank, "get_settings", lambda: {"provider": "localstack"})
    monkeypatch.setattr(local_stack, "_voice_depth", 0)

    assert local_stack.is_voice_active() is False
    assert rerank.score("q", ["doc a", "doc b"]) == [0.9, 0.1]


# ── async wrapper: block, release, and never-raise ────────────────────────────

def test_localstack_score_returns_none_when_gate_times_out(monkeypatch):
    def run():
        async def scenario():
            monkeypatch.setattr(local_stack, "RETRIEVAL_GATE_TIMEOUT_S", 0.1)
            async with local_stack.voice_session():
                return await rerank._score_localstack("q", ["doc a", "doc b"])
        return asyncio.run(scenario())
    assert run() is None


def test_localstack_score_runs_dispatch_when_session_releases(monkeypatch):
    def run():
        async def scenario():
            monkeypatch.setattr(rerank, "_do_localstack_rerank",
                                lambda q, p, s: [0.7, 0.2])
            monkeypatch.setattr(rerank, "get_settings",
                                lambda: {"provider": "localstack"})
            # No voice session open -> gate opens immediately, dispatch runs.
            return await rerank._score_localstack("q", ["doc a", "doc b"])
        return asyncio.run(scenario())
    assert run() == [0.7, 0.2]


def test_localstack_score_never_raises_on_transport_error(monkeypatch):
    """A transport failure from the dispatch (unreachable :9098) degrades to None,
    mirroring score()'s never-raise backstop (A9) — not a propagating exception."""
    def run():
        async def scenario():
            def boom(q, p, s):
                raise ConnectionError("connection refused to :9098")
            monkeypatch.setattr(rerank, "_do_localstack_rerank", boom)
            monkeypatch.setattr(rerank, "get_settings",
                                lambda: {"provider": "localstack"})
            return await rerank._score_localstack("q", ["doc a", "doc b"])
        return asyncio.run(scenario())
    assert run() is None
