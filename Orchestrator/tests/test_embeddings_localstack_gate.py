"""The localstack embeddings provider must serialize behind an open on-box voice
session (D12) and, if the bounded gate times out, raise EmbeddingProviderError so
the mint completes vector-less rather than deadlocking."""
import asyncio

import pytest

from Orchestrator import local_stack
from Orchestrator.embeddings.providers import EmbeddingProviderError, get_provider


def _localstack_provider():
    # The on-box embedding slug; get_provider returns the localstack-backed provider.
    return get_provider("qwen3-embedding-8b-local")


def test_embed_raises_provider_error_when_gate_times_out(monkeypatch):
    async def scenario():
        monkeypatch.setattr(local_stack, "RETRIEVAL_GATE_TIMEOUT_S", 0.1)
        prov = _localstack_provider()
        # Stub the network so the ONLY route to a raise is the gate: without the
        # gate this returns valid vectors (DID NOT RAISE); the gate must convert
        # the held on-box voice session into EmbeddingProviderError. Keeps the
        # test hermetic on a box with no live :9098 backend.
        async def _fake_post(texts, purpose):
            return [[0.0] * prov.dims for _ in texts]
        monkeypatch.setattr(prov, "_post_embeddings", _fake_post, raising=False)
        async with local_stack.voice_session():          # gate can never open
            with pytest.raises(EmbeddingProviderError):
                await prov.embed(["hello"], "document")
    asyncio.run(scenario())


def test_embed_proceeds_when_no_voice_session(monkeypatch):
    # With no voice session the gate opens; stub the network so the test is
    # hermetic and assert embed() runs the dispatch to completion (the gate is
    # acquired AND released, not merely un-held) and returns the vectors.
    async def scenario():
        prov = _localstack_provider()

        async def _fake_post(texts, purpose):        # replaces the real HTTP call
            return [[0.0] * prov.dims for _ in texts]
        monkeypatch.setattr(prov, "_post_embeddings", _fake_post, raising=False)

        assert local_stack.is_voice_active() is False
        vectors = await prov.embed(["hello", "world"], "document")
        assert len(vectors) == 2
        assert all(len(vec) == prov.dims for vec in vectors)
        # Gate released after the happy path — a subsequent embed still passes.
        again = await prov.embed(["again"], "document")
        assert len(again) == 1
        assert local_stack.is_voice_active() is False
    asyncio.run(scenario())
