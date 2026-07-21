"""Pluggable embeddings — provider layer tests (Task 3).

Per docs/plans/2026-06-11-pluggable-embeddings.md Task 3: one async
`embed(texts, purpose)` interface over Gemini / OpenAI / Ollama with
truncation, retry/backoff, purpose→task_type mapping (the retrieval_query
fix), and a dims sanity guard. ALL network is mocked — zero live calls,
zero real sleeps.
"""
import json
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from Orchestrator import config, tokenization
from Orchestrator.embeddings import providers
from Orchestrator.embeddings.providers import (
    EmbeddingProviderError,
    GeminiProvider,
    LocalStackProvider,
    OllamaProvider,
    OpenAIProvider,
    get_provider,
)
from Orchestrator.embeddings.registry import EMBEDDING_MODELS

GEMINI_SLUG = "gemini-embedding-001"
OPENAI_SLUG = "openai-text-embedding-3-large"
OLLAMA_SLUG = "qwen3-embedding-0.6b"

GEMINI_DIMS = EMBEDDING_MODELS[GEMINI_SLUG]["dims"]
OPENAI_DIMS = EMBEDDING_MODELS[OPENAI_SLUG]["dims"]
OLLAMA_DIMS = EMBEDDING_MODELS[OLLAMA_SLUG]["dims"]

LOCALSTACK_SLUG = "qwen3-embedding-8b-local"
LOCALSTACK_DIMS = EMBEDDING_MODELS[LOCALSTACK_SLUG]["dims"]
LOCALSTACK_BASE = "http://127.0.0.1:9098/v1"


@pytest.fixture(autouse=True)
def _fresh_provider_cache():
    """Tests mutate provider instances (_sleep, _client_factory, _transport)
    — never let that leak through the factory cache."""
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


def _openai_mock_client(resp):
    """Stand-in for AsyncOpenAI: supports `async with` (the provider creates
    and closes a client per _embed call) and a mocked embeddings.create."""
    client = MagicMock()
    client.embeddings.create = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


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
        request_options={"timeout": providers.GEMINI_EMBED_TIMEOUT_S},
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
        request_options={"timeout": providers.GEMINI_EMBED_TIMEOUT_S},
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


@pytest.mark.asyncio
async def test_gemini_hanging_call_times_out_to_provider_error(monkeypatch):
    """Review note (Task 16): a hung Gemini embed must not pin an embed-pool
    worker forever. The asyncio.wait_for outer guard (2x the gRPC deadline,
    patched short here) turns the hang into retried TimeoutErrors and finally
    an EmbeddingProviderError — never an indefinite block."""
    provider = get_provider(GEMINI_SLUG)
    sleeps = _record_sleeps(provider)
    monkeypatch.setattr(providers, "GEMINI_EMBED_TIMEOUT_S", 0.02)

    def hang(**kwargs):
        time.sleep(0.5)  # well past 2x the patched deadline
        return {"embedding": [0.0] * GEMINI_DIMS}

    with patch.object(providers.genai, "embed_content", hang):
        with pytest.raises(EmbeddingProviderError, match="failed after 4 attempts"):
            await provider.embed(["stuck"], purpose="document")
    assert sleeps == [1.0, 2.0, 4.0]  # full retry envelope, no real sleeping


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
async def test_ollama_keep_alive_passthrough(tmp_path, monkeypatch):
    # Hermetic: point the stores dir at an empty tmp dir so a real per-box
    # keep_alive.json override (wizard toggle) can't shadow the registry
    # default this test asserts. (Override precedence itself is pinned by
    # test_embeddings_keep_alive.py::test_provider_sends_overridden_keep_alive.)
    monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(tmp_path / "embeddings"))
    provider = get_provider(OLLAMA_SLUG)
    seen = []
    _ollama_with_mock_transport(provider, seen, OLLAMA_DIMS)
    await provider.embed(["text"], purpose="document")
    assert seen[0]["json"]["keep_alive"] == EMBEDDING_MODELS[OLLAMA_SLUG]["keep_alive"]


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["qwen3-embedding-0.6b", "qwen3-embedding-8b"])
async def test_ollama_payload_sends_num_ctx_and_truncate_false(slug):
    """WI-1: Ollama must get an explicit num_ctx (its VRAM-tiered default ctx
    silently truncated at 4,095 tokens — live-probed) plus truncate:false so
    an over-budget input fails LOUD (400) instead of silently losing its tail.
    num_ctx = the registry max_input_tokens (1.0x); the clamp budget is 0.9x,
    so a 400 can only mean our own accounting failed."""
    provider = get_provider(slug)
    seen = []
    _ollama_with_mock_transport(provider, seen, EMBEDDING_MODELS[slug]["dims"])
    await provider.embed(["text"], purpose="document")
    payload = seen[0]["json"]
    assert payload["options"] == {"num_ctx": EMBEDDING_MODELS[slug]["max_input_tokens"]}
    assert payload["truncate"] is False


def test_ollama_read_timeout_is_generous_for_local_cpu_inference():
    # Ratchet: a cold Qwen3-8B batch-of-8 on CPU measured >120s and timed out
    # the whole retry envelope. The read cap must stay generous so legitimate
    # slow local inference completes; connect stays short to catch a dead daemon.
    assert OllamaProvider.TIMEOUT.read >= 300.0
    assert OllamaProvider.TIMEOUT.connect <= 10.0


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
    client = _openai_mock_client(resp)
    provider._client_factory = lambda: client
    result = await provider.embed(["a", "b", "c"], purpose="document")
    assert result == [vec_a, vec_b, vec_c]
    client.embeddings.create.assert_awaited_once_with(
        model=EMBEDDING_MODELS[OPENAI_SLUG]["model_id"], input=["a", "b", "c"]
    )


@pytest.mark.asyncio
async def test_openai_client_created_per_call():
    # ephemeral-loop safety: a cached client's httpx pool binds to the first
    # event loop and dies with it; the provider must build a fresh client per
    # _embed call. Two embeds → two factory invocations, each entered+exited.
    provider = get_provider(OPENAI_SLUG)
    resp = SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[0.0] * OPENAI_DIMS)]
    )
    clients = []

    def factory():
        client = _openai_mock_client(resp)
        clients.append(client)
        return client

    provider._client_factory = factory
    await provider.embed(["first"], purpose="document")
    await provider.embed(["second"], purpose="document")
    assert len(clients) == 2
    assert clients[0] is not clients[1]
    for client in clients:
        client.__aenter__.assert_awaited_once()
        client.__aexit__.assert_awaited_once()  # closed, not leaked
        client.embeddings.create.assert_awaited_once()


# ── token-aware clamp (WI-1 mode 1) ──────────────────────────────────────────
# The old 10,000-char cap (≈ 3.4k tokens) both
# over-truncated (672 snapshots lost their tails) and under-protected (Ollama
# silently cut at its 4,095-token default ctx). The clamp is token-aware via
# tokenization.clamp_to_tokens with a per-model registry budget and a 10%
# margin — and it NEVER raises (queries rely on the layer self-capping).

def _clamp_budget(slug: str) -> int:
    return int(EMBEDDING_MODELS[slug]["max_input_tokens"] * 0.9)


@pytest.mark.asyncio
async def test_qwen_91k_char_document_clamps_to_token_budget():
    """A 91k-char document (the live-probe size) reaches Ollama at ≤ the
    per-model token budget — estimate-verified with the exact vendored
    tokenizer, head-preserving."""
    provider = get_provider(OLLAMA_SLUG)
    seen = []
    _ollama_with_mock_transport(provider, seen, OLLAMA_DIMS)
    doc = ("The embedding watcher healed the missing snapshots overnight. " * 1500)[:91000]
    assert len(doc) == 91000
    await provider.embed([doc], purpose="document")
    sent = seen[0]["json"]["input"][0]
    assert tokenization.estimate_tokens(sent, OLLAMA_SLUG) <= _clamp_budget(OLLAMA_SLUG)
    assert len(sent) < len(doc)
    assert sent.startswith(doc[:200])  # head-preserving


@pytest.mark.asyncio
async def test_query_purpose_clamps_and_never_raises():
    """chat_routes._last_user_msg / tasks.py tool selection rely on the
    embedding layer self-capping queries — oversized and garbage query text
    must clamp, embed, and NEVER raise."""
    provider = get_provider(OLLAMA_SLUG)
    seen = []
    _ollama_with_mock_transport(provider, seen, OLLAMA_DIMS)
    oversized = "how did we fix the upload preview css bug " * 3000  # ~126k chars
    garbage = "<|endoftext|><|im_start|>\x00\x01� 🤖\n\t" * 400
    result = await provider.embed([oversized, garbage], purpose="query")
    assert len(result) == 2
    budget = _clamp_budget(OLLAMA_SLUG)
    for sent in seen[0]["json"]["input"]:
        assert tokenization.estimate_tokens(sent, OLLAMA_SLUG) <= budget


@pytest.mark.asyncio
async def test_ollama_query_budget_accounts_for_instruction_prefix():
    """The registry query_instruction is prefixed AFTER clamping — the clamp
    budget must leave room so prefix+text still fits the full budget."""
    provider = get_provider(OLLAMA_SLUG)
    seen = []
    _ollama_with_mock_transport(provider, seen, OLLAMA_DIMS)
    query = "find the toolvault reload embedding cache design decision " * 2000
    await provider.embed([query], purpose="query")
    sent = seen[0]["json"]["input"][0]
    instruction = EMBEDDING_MODELS[OLLAMA_SLUG]["query_instruction"]
    assert sent.startswith(instruction)  # prefix survived, applied post-clamp
    assert tokenization.estimate_tokens(sent, OLLAMA_SLUG) <= _clamp_budget(OLLAMA_SLUG)


@pytest.mark.asyncio
async def test_per_model_budgets_differentiated_gemini_001_vs_2():
    """Budgets come from the registry PER MODEL: gemini-001 clamps at its
    2048-token budget, gemini-2 at its 8192 — same text, different cut."""
    text = "z" * 40000  # floor path (remote tokenizer): est 20000 tokens, over both
    sent_lens = {}
    for slug in (GEMINI_SLUG, "gemini-embedding-2"):
        provider = get_provider(slug)
        dims = EMBEDDING_MODELS[slug]["dims"]
        fake = MagicMock(return_value={"embedding": [0.0] * dims})
        with patch.object(providers.genai, "embed_content", fake):
            await provider.embed([text], purpose="document")
        sent = fake.call_args.kwargs["content"]
        # remote:* tokenizer → calibrated floor path: budget*2 chars exactly
        assert len(sent) == _clamp_budget(slug) * tokenization.CHARS_PER_TOKEN_FLOOR
        sent_lens[slug] = len(sent)
    assert sent_lens[GEMINI_SLUG] < sent_lens["gemini-embedding-2"]


@pytest.mark.asyncio
async def test_clamp_telemetry_emitted_on_clamp(capsys):
    provider = get_provider(OLLAMA_SLUG)
    seen = []
    _ollama_with_mock_transport(provider, seen, OLLAMA_DIMS)
    await provider.embed(["telemetry check text " * 5000], purpose="document")
    out = capsys.readouterr().out
    assert re.search(
        r"\[EMBEDDING\] clamped qwen3-embedding-0\.6b document \d+->\d+ tokens", out
    ), f"missing clamp telemetry line, got: {out!r}"


@pytest.mark.asyncio
async def test_no_clamp_telemetry_when_under_budget(capsys):
    provider = get_provider(OLLAMA_SLUG)
    seen = []
    _ollama_with_mock_transport(provider, seen, OLLAMA_DIMS)
    await provider.embed(["a perfectly ordinary short text"], purpose="document")
    assert "clamped" not in capsys.readouterr().out
    assert seen[0]["json"]["input"] == ["a perfectly ordinary short text"]  # untouched


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


@pytest.mark.asyncio
async def test_count_mismatch_raises_provider_error_without_retry():
    # provider returns fewer vectors than texts — malformed response, not a
    # transient fault: must raise EmbeddingProviderError with no retries
    provider = get_provider(OPENAI_SLUG)
    sleeps = _record_sleeps(provider)
    resp = SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[0.0] * OPENAI_DIMS)]
    )  # 1 vector for 2 texts
    client = _openai_mock_client(resp)
    provider._client_factory = lambda: client
    with pytest.raises(EmbeddingProviderError, match="got 1 vectors for 2 texts"):
        await provider.embed(["a", "b"], purpose="document")
    assert client.embeddings.create.await_count == 1
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
    assert isinstance(get_provider("qwen3-embedding-8b-local"), LocalStackProvider)
    assert isinstance(get_provider("qwen3-embedding-0.6b-local"), LocalStackProvider)


# ── LocalStack (on-box llama-swap :9098) ─────────────────────────────────────

def _localstack_with_mock_transport(provider, requests_seen, dims, status=200):
    """Route the provider's httpx client through a MockTransport that records
    the request (payload + headers) and answers with the OpenAI /embeddings
    response shape ({data:[{index, embedding}]})."""

    def handler(request):
        body = json.loads(request.content.decode())
        requests_seen.append({
            "url": str(request.url),
            "json": body,
            "headers": {k.lower(): v for k, v in request.headers.items()},
        })
        if status != 200:
            return httpx.Response(status, json={"error": "boom"})
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": [0.0] * dims}
            for i, _ in enumerate(body["input"])
        ]})

    provider._transport = httpx.MockTransport(handler)
    return provider


@pytest.fixture
def _localstack_base(monkeypatch):
    from Orchestrator import local_stack
    monkeypatch.setattr(local_stack, "base_url", lambda: LOCALSTACK_BASE)
    return LOCALSTACK_BASE


@pytest.mark.asyncio
async def test_localstack_document_posts_openai_shape_to_front_door(_localstack_base):
    provider = get_provider(LOCALSTACK_SLUG)
    seen = []
    _localstack_with_mock_transport(provider, seen, LOCALSTACK_DIMS)
    result = await provider.embed(["first", "second"], purpose="document")
    assert len(result) == 2 and all(len(v) == LOCALSTACK_DIMS for v in result)
    assert seen[0]["url"] == f"{LOCALSTACK_BASE}/embeddings"
    assert seen[0]["json"]["model"] == EMBEDDING_MODELS[LOCALSTACK_SLUG]["model_id"]
    assert seen[0]["json"]["input"] == ["first", "second"]
    # loopback → NEVER an Authorization header (keyless front door)
    assert "authorization" not in seen[0]["headers"]


@pytest.mark.asyncio
async def test_localstack_query_prefixes_instruction(_localstack_base):
    provider = get_provider(LOCALSTACK_SLUG)
    seen = []
    _localstack_with_mock_transport(provider, seen, LOCALSTACK_DIMS)
    await provider.embed(["find the css fix"], purpose="query")
    instruction = EMBEDDING_MODELS[LOCALSTACK_SLUG]["query_instruction"]
    assert seen[0]["json"]["input"] == [instruction + "find the css fix"]


@pytest.mark.asyncio
async def test_localstack_output_follows_input_order(_localstack_base):
    # response indices deliberately scrambled — output must follow input order
    provider = get_provider(LOCALSTACK_SLUG)

    def handler(request):
        body = json.loads(request.content.decode())
        n = len(body["input"])
        data = [{"index": i, "embedding": [float(i)] + [0.0] * (LOCALSTACK_DIMS - 1)}
                for i in range(n)]
        return httpx.Response(200, json={"data": list(reversed(data))})

    provider._transport = httpx.MockTransport(handler)
    result = await provider.embed(["a", "b", "c"], purpose="document")
    assert [v[0] for v in result] == [0.0, 1.0, 2.0]


def test_localstack_read_timeout_is_generous_for_cold_group_swaps():
    # A cross-group swap + 8B GGUF cold-load holds the request open through
    # llama-swap's queue; the read cap must outlast it, connect stays short.
    assert LocalStackProvider.TIMEOUT.read >= 300.0
    assert LocalStackProvider.TIMEOUT.connect <= 10.0


@pytest.mark.asyncio
async def test_localstack_http_error_retries_then_raises(_localstack_base):
    provider = get_provider(LOCALSTACK_SLUG)
    sleeps = _record_sleeps(provider)
    seen = []
    _localstack_with_mock_transport(provider, seen, LOCALSTACK_DIMS, status=503)
    with pytest.raises(EmbeddingProviderError):
        await provider.embed(["text"], purpose="document")
    assert sleeps == [1.0, 2.0, 4.0]      # full retry envelope, no real sleeps
    assert len(seen) == 4                  # initial + 3 retries


@pytest.mark.asyncio
async def test_localstack_dims_mismatch_raises_without_retry(_localstack_base):
    provider = get_provider(LOCALSTACK_SLUG)
    sleeps = _record_sleeps(provider)

    def handler(request):
        body = json.loads(request.content.decode())
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": [0.0] * (LOCALSTACK_DIMS - 1)}
            for i, _ in enumerate(body["input"])
        ]})

    provider._transport = httpx.MockTransport(handler)
    with pytest.raises(EmbeddingProviderError):
        await provider.embed(["hello"], purpose="document")
    assert sleeps == []                    # guard fires after a "successful" call
