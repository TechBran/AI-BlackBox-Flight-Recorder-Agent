"""localstack rerank must serialize behind an open on-box voice session (D12) and,
if the bounded gate times out, return None (un-reranked) rather than raise."""
import asyncio

from Orchestrator import local_stack
from Orchestrator import rerank


def test_localstack_score_returns_none_when_gate_times_out(monkeypatch):
    async def scenario():
        monkeypatch.setattr(local_stack, "RETRIEVAL_GATE_TIMEOUT_S", 0.1)
        async with local_stack.voice_session():
            # Call the localstack scorer directly. Adjust the symbol to M4's
            # landed name (_score_localstack); it must be reachable + async.
            out = await rerank._score_localstack("q", ["doc a", "doc b"])
            return out
    assert asyncio.run(scenario()) is None
