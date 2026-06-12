"""Pluggable embeddings — provider layer tests (Task 3).

Per docs/plans/2026-06-11-pluggable-embeddings.md Task 3: one async
`embed(texts, purpose)` interface over Gemini / OpenAI / Ollama with
truncation, retry/backoff, purpose→task_type mapping (the retrieval_query
fix), and a dims sanity guard. ALL network is mocked — zero live calls,
zero real sleeps.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from Orchestrator import config
from Orchestrator.embeddings import providers
from Orchestrator.embeddings.providers import (
    EmbeddingProviderError,
    GeminiProvider,
    OllamaProvider,
    OpenAIProvider,
    get_provider,
)
from Orchestrator.embeddings.registry import EMBEDDING_MAX_CHARS, EMBEDDING_MODELS

GEMINI_SLUG = "gemini-embedding-001"
OPENAI_SLUG = "openai-text-embedding-3-large"
OLLAMA_SLUG = "qwen3-embedding-0.6b"

GEMINI_DIMS = EMBEDDING_MODELS[GEMINI_SLUG]["dims"]
OPENAI_DIMS = EMBEDDING_MODELS[OPENAI_SLUG]["dims"]
OLLAMA_DIMS = EMBEDDING_MODELS[OLLAMA_SLUG]["dims"]


@pytest.fixture(autouse=True)
def _fresh_provider_cache():
    """Tests mutate provider instances (_sleep, _client, _transport) —
    never let that leak through the factory cache."""
    providers._instances.clear()
    yield
    providers._instances.clear()


def _record_sleeps(provider):
    sleeps = []

    async def _sleep(seconds):
        sleeps.append(seconds)

    provider._sleep = _sleep
    return sleeps


def _ollama_with_mock_transport(provider, requests_seen, dims, status=200):
    """Route the provider's httpx client through a MockTransport that
    records every request payload and answers with zero vectors."""

    def handler(request):
        body = json.loads(request.content.decode())
        requests_seen.append({"url": str(request.url), "json": body})
        if status != 200:
            return httpx.Response(status, json={"error": "boom"})
        return httpx.Response(
            200, json={"embeddings": [[0.0] * dims for _ in body["input"]]}
        )

    provider._transport = httpx.MockTransport(handler)
    return provider


# ── Gemini: purpose → task_type mapping ──────────────────────────────────────

@pytest.mark.asyncio
async def test_gemini_document_maps_to_retrieval_document():
    provider = get_provider(GEMINI_SLUG)
    fake = MagicMock(return_value={"embedding": [0.0] * GEMINI_DIMS})
    with patch.object(providers.genai, "embed_content", fake):
        result = await provider.embed(["hello world"], purpose="document")
    assert result == [[0.0] * GEMINI_DIMS]
    fake.assert_called_once_with(
        model=EMBEDDING_MODELS[GEMINI_SLUG]["model_id"],
        content="hello world",
        task_type="retrieval_document",
    )


@pytest.mark.asyncio
async def test_gemini_query_maps_to_retrieval_query():
    # THE bug fix: legacy generate_embedding used retrieval_document for queries
    provider = get_provider(GEMINI_SLUG)
    fake = MagicMock(return_value={"embedding": [0.0] * GEMINI_DIMS})
    with patch.object(providers.genai, "embed_content", fake):
        await provider.embed(["find the css fix"], purpose="query")
    fake.assert_called_once_with(
        model=EMBEDDING_MODELS[GEMINI_SLUG]["model_id"],
        content="find the css fix",
        task_type="retrieval_query",
    )


@pytest.mark.asyncio
async def test_gemini_one_call_per_text():
    provider = get_provider(GEMINI_SLUG)
    fake = MagicMock(return_value={"embedding": [0.0] * GEMINI_DIMS})
    with patch.object(providers.genai, "embed_content", fake):
        result = await provider.embed(["a", "b", "c"], purpose="document")
    assert len(result) == 3
    assert fake.call_count == 3
    assert [c.kwargs["content"] for c in fake.call_args_list] == ["a", "b", "c"]


# ── purpose validation ───────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_purpose", ["retrieval_document", "doc", "", None])
async def test_invalid_purpose_raises_valueerror(bad_purpose):
    provider = get_provider(GEMINI_SLUG)
    fake = MagicMock()
    with patch.object(providers.genai, "embed_content", fake):
        with pytest.raises(ValueError):
            await provider.embed(["hello"], purpose=bad_purpose)
    fake.assert_not_called()


# ── Ollama: query-instruction prefixing ──────────────────────────────────────

@pytest.mark.asyncio
async def test_ollama_query_prefixes_instruction_on_each_text():
    provider = get_provider(OLLAMA_SLUG)
    seen = []
    _ollama_with_mock_transport(provider, seen, OLLAMA_DIMS)
    await provider.embed(["first", "second"], purpose="query")
    instruction = EMBEDDING_MODELS[OLLAMA_SLUG]["query_instruction"]
    assert seen[0]["json"]["input"] == [instruction + "first", instruction + "second"]


@pytest.mark.asyncio
async def test_ollama_document_not_prefixed():
    provider = get_provider(OLLAMA_SLUG)
    seen = []
    _ollama_with_mock_transport(provider, seen, OLLAMA_DIMS)
    await provider.embed(["first", "second"], purpose="document")
    assert seen[0]["json"]["input"] == ["first", "second"]
    assert seen[0]["json"]["model"] == EMBEDDING_MODELS[OLLAMA_SLUG]["model_id"]
    assert seen[0]["url"] == f"{config.OLLAMA_BASE_URL}/api/embed"


# ── Ollama: keep_alive passthrough / omission ────────────────────────────────

@pytest.mark.asyncio
async def test_ollama_keep_alive_passthrough():
    provider = get_provider(OLLAMA_SLUG)
    seen = []
    _ollama_with_mock_transport(provider, seen, OLLAMA_DIMS)
    await provider.embed(["text"], purpose="document")
    assert seen[0]["json"]["keep_alive"] == EMBEDDING_MODELS[OLLAMA_SLUG]["keep_alive"]


@pytest.mark.asyncio
async def test_ollama_keep_alive_omitted_when_none():
    entry = {
        "provider": "ollama", "model_id": "fake-embed:tiny", "dims": 4,
        "query_instruction": None, "keep_alive": None,
    }
    provider = OllamaProvider("synthetic-ollama", entry)
    seen = []
    _ollama_with_mock_transport(provider, seen, 4)
    await provider.embed(["text"], purpose="query")  # None instruction: no prefix either
    assert "keep_alive" not in seen[0]["json"]
    assert seen[0]["json"]["input"] == ["text"]


# ── OpenAI: single batched call, order preserved ─────────────────────────────

@pytest.mark.asyncio
async def test_openai_batch_order_preserved():
    provider = get_provider(OPENAI_SLUG)
    vec_a = [1.0] + [0.0] * (OPENAI_DIMS - 1)
    vec_b = [2.0] + [0.0] * (OPENAI_DIMS - 1)
    vec_c = [3.0] + [0.0] * (OPENAI_DIMS - 1)
    # response data deliberately scrambled — output must follow input order
    resp = SimpleNamespace(data=[
        SimpleNamespace(index=2, embedding=vec_c),
        SimpleNamespace(index=0, embedding=vec_a),
        SimpleNamespace(index=1, embedding=vec_b),
    ])
    client = MagicMock()
    client.embeddings.create = AsyncMock(return_value=resp)
    provider._client = client
    result = await provider.embed(["a", "b", "c"], purpose="document")
    assert result == [vec_a, vec_b, vec_c]
    client.embeddings.create.assert_awaited_once_with(
        model=EMBEDDING_MODELS[OPENAI_SLUG]["model_id"], input=["a", "b", "c"]
    )


# ── truncation ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_truncation_reaches_gemini_transport():
    provider = get_provider(GEMINI_SLUG)
    long_text = "x" * (EMBEDDING_MAX_CHARS + 500)
    fake = MagicMock(return_value={"embedding": [0.0] * GEMINI_DIMS})
    with patch.object(providers.genai, "embed_content", fake):
        await provider.embed([long_text], purpose="document")
    sent = fake.call_args.kwargs["content"]
    assert sent == "x" * EMBEDDING_MAX_CHARS + "..."  # monitoring.py semantics


@pytest.mark.asyncio
async def test_truncation_reaches_ollama_transport_short_text_untouched():
    provider = get_provider(OLLAMA_SLUG)
    seen = []
    _ollama_with_mock_transport(provider, seen, OLLAMA_DIMS)
    long_text = "y" * (EMBEDDING_MAX_CHARS + 1)
    await provider.embed([long_text, "short"], purpose="document")
    assert seen[0]["json"]["input"] == ["y" * EMBEDDING_MAX_CHARS + "...", "short"]


# ── retry / backoff ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_then_success_records_backoff_sleeps():
    provider = get_provider(GEMINI_SLUG)
    sleeps = _record_sleeps(provider)
    good = {"embedding": [0.0] * GEMINI_DIMS}
    fake = MagicMock(side_effect=[RuntimeError("boom1"), RuntimeError("boom2"), good])
    with patch.object(providers.genai, "embed_content", fake):
        result = await provider.embed(["hello"], purpose="document")
    assert result == [[0.0] * GEMINI_DIMS]
    assert sleeps == [1.0, 2.0]
    assert fake.call_count == 3


@pytest.mark.asyncio
async def test_retry_exhaustion_raises_provider_error():
    provider = get_provider(GEMINI_SLUG)
    sleeps = _record_sleeps(provider)
    fake = MagicMock(side_effect=RuntimeError("api down"))
    with patch.object(providers.genai, "embed_content", fake):
        with pytest.raises(EmbeddingProviderError):
            await provider.embed(["hello"], purpose="document")
    assert sleeps == [1.0, 2.0, 4.0]
    assert fake.call_count == 4  # initial attempt + 3 retries


@pytest.mark.asyncio
async def test_ollama_http_error_retries_then_raises():
    provider = get_provider(OLLAMA_SLUG)
    sleeps = _record_sleeps(provider)
    seen = []
    _ollama_with_mock_transport(provider, seen, OLLAMA_DIMS, status=500)
    with pytest.raises(EmbeddingProviderError):
        await provider.embed(["text"], purpose="document")
    assert sleeps == [1.0, 2.0, 4.0]
    assert len(seen) == 4


# ── dims sanity guard ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dims_mismatch_raises_provider_error_without_retry():
    # catches vendor-side dim changes / wrong-model responses
    provider = get_provider(GEMINI_SLUG)
    sleeps = _record_sleeps(provider)
    fake = MagicMock(return_value={"embedding": [0.0] * (GEMINI_DIMS - 1)})
    with patch.object(providers.genai, "embed_content", fake):
        with pytest.raises(EmbeddingProviderError):
            await provider.embed(["hello"], purpose="document")
    assert fake.call_count == 1  # guard fires after a "successful" call — no retries
    assert sleeps == []


# ── empty input ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_texts_returns_empty_without_calling_transport():
    provider = get_provider(GEMINI_SLUG)
    fake = MagicMock()
    with patch.object(providers.genai, "embed_content", fake):
        assert await provider.embed([], purpose="document") == []
    fake.assert_not_called()


# ── factory ──────────────────────────────────────────────────────────────────

def test_get_provider_unknown_slug_raises():
    with pytest.raises(ValueError):
        get_provider("not-a-registered-model")


def test_get_provider_returns_cached_instance():
    assert get_provider(GEMINI_SLUG) is get_provider(GEMINI_SLUG)
    assert get_provider(OLLAMA_SLUG) is get_provider(OLLAMA_SLUG)
    assert get_provider(GEMINI_SLUG) is not get_provider(OLLAMA_SLUG)


def test_get_provider_class_per_registry_provider():
    assert isinstance(get_provider(GEMINI_SLUG), GeminiProvider)
    assert isinstance(get_provider(OPENAI_SLUG), OpenAIProvider)
    assert isinstance(get_provider(OLLAMA_SLUG), OllamaProvider)
    assert isinstance(get_provider("qwen3-embedding-8b"), OllamaProvider)
