"""Pluggable embeddings — provider layer tests (Task 3).

Per docs/plans/2026-06-11-pluggable-embeddings.md Task 3: one async
`embed(texts, purpose)` interface over Gemini / OpenAI / Ollama with
truncation, retry/backoff, purpose→task_type mapping (the retrieval_query
fix), and a dims sanity guard. ALL network is mocked — zero live calls,
zero real sleeps.
"""
import asyncio
import json
import re
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
    and route the shared Gemini client through fakes — never let either leak
    through the factory cache or the shared-client slot."""
    providers._instances.clear()
    providers._gemini_client = None
    yield
    providers._instances.clear()
    providers._gemini_client = None


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


# ── Gemini: one shared google-genai sync client, batched request ─────────────

def _gemini_response(vectors):
    """An EmbedContentResponse-shaped object: .embeddings[i].values."""
    return SimpleNamespace(embeddings=[SimpleNamespace(values=v) for v in vectors])


def _gemini_mock_client(dims=GEMINI_DIMS, side_effect=None):
    """Stand-in for the shared google.genai.Client. The provider calls the SYNC
    models.embed_content(model=, contents=[...], config=) on a worker thread
    and never closes the client (other embeds share it); by default it answers
    one zero vector per content."""
    def answer(*, model, contents, config):
        return _gemini_response([[0.0] * dims for _ in contents])

    client = MagicMock()
    client.models.embed_content = MagicMock(side_effect=side_effect or answer)
    return client


def _with_gemini_client(provider, client):
    """Route the provider through `client`; returns the embed_content mock."""
    provider._client_factory = lambda: client
    return client.models.embed_content


def _sent_texts(call):
    """The texts of one embed_content call: one single-part Content each."""
    contents = call.kwargs["contents"]
    assert all(len(c.parts) == 1 for c in contents)
    return [c.parts[0].text for c in contents]


def _assert_gemini_request(call, slug, texts, task_type):
    """The request the legacy SDK sent, per text: the registry model id, the
    text, the task type, and nothing else (no output_dimensionality: the
    model's full dims, which the stores hold; no title)."""
    assert call.kwargs["model"] == EMBEDDING_MODELS[slug]["model_id"]
    assert _sent_texts(call) == texts
    request_config = call.kwargs["config"]
    assert request_config.task_type == task_type
    assert request_config.output_dimensionality is None
    assert request_config.title is None
    assert request_config.http_options.timeout == int(providers.GEMINI_EMBED_TIMEOUT_S * 1000)


def _gemini_dims_of(url):
    """The registry dims of the Gemini model a batchEmbedContents URL names."""
    for entry in EMBEDDING_MODELS.values():
        if entry["provider"] == "gemini" and \
                str(url).endswith(f"/{entry['model_id']}:batchEmbedContents"):
            return entry["dims"]
    raise AssertionError(f"not a registry Gemini embed URL: {url}")


def _capture(seen, answer=None):
    """An httpx handler for the REAL SDK: records method, url, headers, the
    body bytes, the timeout extension and the calling thread, then answers
    batchEmbedContents with one vector per request (`answer(texts)` picks
    them; by default vector i is [i] * the URL's model dims)."""
    def handler(request):
        body = json.loads(request.content.decode())
        texts = [r["content"]["parts"][0]["text"] for r in body["requests"]]
        seen.append({"method": request.method, "url": str(request.url),
                     "headers": dict(request.headers), "content": request.content,
                     "json": body, "timeout": request.extensions.get("timeout"),
                     "thread": threading.get_ident()})
        dims = _gemini_dims_of(request.url)
        vectors = answer(texts) if answer else [[float(i)] * dims for i in range(len(texts))]
        return httpx.Response(200, json={"embeddings": [{"values": v} for v in vectors]})
    return handler


def _route_default_client_through(monkeypatch, handler, build_delay=0.0):
    """Leave the provider's DEFAULT factory (the shared client) in place, but
    have every genai.Client it builds send its SYNC requests through
    MockTransport(handler); the async client's transport fails the request
    outright, since the provider must not use it. Returns the list of
    (kwargs, client) the factory built."""
    from google import genai
    from google.genai import types as genai_types

    real_client = genai.Client
    built = []

    def async_used(request):
        raise AssertionError("the provider sent its embed through the async client")

    def build(**kwargs):
        time.sleep(build_delay)  # widens the cold-cache race for the thread test
        client = real_client(**kwargs, http_options=genai_types.HttpOptions(
            client_args={"transport": httpx.MockTransport(handler)},
            async_client_args={"transport": httpx.MockTransport(async_used)}))
        built.append((kwargs, client))
        return client

    monkeypatch.setattr(providers.genai, "Client", build)
    return built


@pytest.mark.asyncio
async def test_gemini_document_maps_to_retrieval_document():
    provider = get_provider(GEMINI_SLUG)
    embed = _with_gemini_client(provider, _gemini_mock_client())
    result = await provider.embed(["hello world"], purpose="document")
    assert result == [[0.0] * GEMINI_DIMS]
    embed.assert_called_once()
    _assert_gemini_request(embed.call_args, GEMINI_SLUG, ["hello world"],
                           "RETRIEVAL_DOCUMENT")


@pytest.mark.asyncio
async def test_gemini_query_maps_to_retrieval_query():
    # THE bug fix: legacy generate_embedding used retrieval_document for queries
    provider = get_provider(GEMINI_SLUG)
    embed = _with_gemini_client(provider, _gemini_mock_client())
    await provider.embed(["find the css fix"], purpose="query")
    embed.assert_called_once()
    _assert_gemini_request(embed.call_args, GEMINI_SLUG, ["find the css fix"],
                           "RETRIEVAL_QUERY")


@pytest.mark.asyncio
async def test_gemini_batches_texts_in_one_call():
    """The legacy SDK made one embed_content call per text; batchEmbedContents
    takes them all in one request, in order."""
    provider = get_provider(GEMINI_SLUG)
    embed = _with_gemini_client(provider, _gemini_mock_client())
    result = await provider.embed(["a", "b", "c"], purpose="document")
    assert len(result) == 3
    embed.assert_called_once()
    _assert_gemini_request(embed.call_args, GEMINI_SLUG, ["a", "b", "c"],
                           "RETRIEVAL_DOCUMENT")


@pytest.mark.asyncio
async def test_gemini_splits_at_the_batch_limit_and_keeps_order():
    """batchEmbedContents refuses more than 100 requests, and one snapshot can
    chunk to more than that (pack_chunk_batches keeps a chunk group in ONE
    provider call). The provider splits, and vector i still belongs to text i."""
    limit = providers.GEMINI_EMBED_BATCH_MAX
    assert limit == 100
    texts = [str(i) for i in range(2 * limit + 50)]

    def answer(*, model, contents, config):
        return _gemini_response([[float(c.parts[0].text)] + [0.0] * (GEMINI_DIMS - 1)
                                 for c in contents])

    provider = get_provider(GEMINI_SLUG)
    embed = _with_gemini_client(provider, _gemini_mock_client(side_effect=answer))
    result = await provider.embed(texts, purpose="document")
    assert [_sent_texts(c) for c in embed.call_args_list] == \
        [texts[:limit], texts[limit:2 * limit], texts[2 * limit:]]
    assert [vec[0] for vec in result] == [float(i) for i in range(len(texts))]


@pytest.mark.asyncio
async def test_gemini_short_batch_raises_provider_error_without_retry():
    """A batch answered with fewer vectors than texts is a malformed response,
    not a transient fault: no retry, and no misaligned vectors downstream."""
    provider = get_provider(GEMINI_SLUG)
    sleeps = _record_sleeps(provider)

    def short(*, model, contents, config):
        return _gemini_response([[0.0] * GEMINI_DIMS])

    embed = _with_gemini_client(provider, _gemini_mock_client(side_effect=short))
    with pytest.raises(EmbeddingProviderError, match="got 1 vectors for 2 texts"):
        await provider.embed(["a", "b"], purpose="document")
    assert embed.call_count == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_gemini_embedding_without_values_hits_the_dims_guard():
    provider = get_provider(GEMINI_SLUG)
    sleeps = _record_sleeps(provider)

    def empty(*, model, contents, config):
        return _gemini_response([None for _ in contents])

    _with_gemini_client(provider, _gemini_mock_client(side_effect=empty))
    with pytest.raises(EmbeddingProviderError, match="0-dim vector"):
        await provider.embed(["a"], purpose="document")
    assert sleeps == []


@pytest.mark.asyncio
async def test_gemini_hanging_call_times_out_to_provider_error(monkeypatch):
    """Review note (Task 16): a hung Gemini embed must not pin its caller
    forever. The asyncio.wait_for outer guard (2x the SDK deadline, patched
    short here) turns the hang into retried TimeoutErrors and finally an
    EmbeddingProviderError — never an indefinite block. (It cannot stop the
    worker thread; the SDK's own HTTP deadline does that, pinned below in the
    wire test.)"""
    provider = get_provider(GEMINI_SLUG)
    sleeps = _record_sleeps(provider)
    monkeypatch.setattr(providers, "GEMINI_EMBED_TIMEOUT_S", 0.02)

    def hang(**kwargs):
        time.sleep(0.3)  # well past 2x the patched deadline
        return _gemini_response([[0.0] * GEMINI_DIMS])

    _with_gemini_client(provider, _gemini_mock_client(side_effect=hang))
    with pytest.raises(EmbeddingProviderError, match="failed after 4 attempts"):
        await provider.embed(["stuck"], purpose="document")
    assert sleeps == [1.0, 2.0, 4.0]  # full retry envelope, no real sleeping


@pytest.mark.asyncio
async def test_gemini_shared_client_is_never_closed_by_an_embed():
    """The client is shared by every embed in the process, so neither a
    success nor a failure closes it, and a retry goes out on the same one."""
    provider = get_provider(GEMINI_SLUG)
    _record_sleeps(provider)
    good = _gemini_response([[0.0] * GEMINI_DIMS])
    client = _gemini_mock_client(side_effect=[RuntimeError("boom"), good])
    served = []

    def factory():
        served.append(client)
        return client

    provider._client_factory = factory
    await provider.embed(["hello"], purpose="document")
    assert client.models.embed_content.call_count == 2
    assert all(c is client for c in served)
    client.close.assert_not_called()
    client.__exit__.assert_not_called()
    client.aio.aclose.assert_not_called()


@pytest.mark.asyncio
async def test_gemini_client_is_built_off_the_event_loop():
    """Building a Client sets up its HTTP sessions and TLS contexts (~30 ms);
    the watcher and migrations embed on the server's one event loop."""
    provider = get_provider(GEMINI_SLUG)
    built_on_loop = []

    def factory():
        try:
            asyncio.get_running_loop()
            built_on_loop.append(True)
        except RuntimeError:
            built_on_loop.append(False)
        return _gemini_mock_client()

    provider._client_factory = factory
    await provider.embed(["hello"], purpose="document")
    assert built_on_loop == [False]


def test_gemini_default_client_carries_the_box_key(monkeypatch):
    """The legacy SDK got its key from config.py's import-time genai.configure;
    the shared client is built with the key config holds NOW (read per call,
    so a key pasted in the wizard applies with no restart), once per key, and
    an unset key leaves the SDK its own GOOGLE_API_KEY/GEMINI_API_KEY lookup."""
    built = []

    def fake_client(**kw):
        built.append(kw)
        return object()

    monkeypatch.setattr(providers.genai, "Client", fake_client)
    provider = GeminiProvider(GEMINI_SLUG, EMBEDDING_MODELS[GEMINI_SLUG])
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "test-key-not-real")
    first = provider._client_factory()
    assert provider._client_factory() is first  # same key: same client
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    provider._client_factory()
    provider._client_factory()
    assert built == [{"api_key": "test-key-not-real"}, {"api_key": None}]


def test_gemini_client_is_reused_across_fresh_event_loops(monkeypatch):
    """THE hot path: search._run_async runs every query embed under its own
    asyncio.run. One client serves embeds from two different loops, and both
    Gemini models (the client is per key, not per model)."""
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "test-key-not-real")
    seen = []
    built = _route_default_client_through(monkeypatch, _capture(seen))
    loops = []

    async def embed_on_a_fresh_loop(slug, text):
        loops.append(asyncio.get_running_loop())
        return await get_provider(slug).embed([text], "query")

    asyncio.run(embed_on_a_fresh_loop(GEMINI_SLUG, "first loop"))
    asyncio.run(embed_on_a_fresh_loop(GEMINI_SLUG, "second loop"))
    asyncio.run(embed_on_a_fresh_loop("gemini-embedding-2", "third loop, other model"))

    assert len({id(loop) for loop in loops}) == 3
    assert len(built) == 1
    assert [s["json"]["requests"][0]["content"]["parts"][0]["text"] for s in seen] == \
        ["first loop", "second loop", "third loop, other model"]
    assert seen[2]["url"].endswith(
        f"/{EMBEDDING_MODELS['gemini-embedding-2']['model_id']}:batchEmbedContents")
    assert not built[0][1]._api_client._httpx_client.is_closed


def test_gemini_changed_key_gets_a_new_client(monkeypatch):
    """A key changed in the wizard takes effect on the next embed: a new
    client, built with the new key and sending it. The old client is dropped,
    NOT closed (another thread may be mid-request on it)."""
    seen = []
    built = _route_default_client_through(monkeypatch, _capture(seen))
    provider = get_provider(GEMINI_SLUG)

    monkeypatch.setattr(config, "GOOGLE_API_KEY", "key-one-not-real")
    asyncio.run(provider.embed(["a"], "query"))
    asyncio.run(provider.embed(["b"], "query"))
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "key-two-not-real")
    asyncio.run(provider.embed(["c"], "query"))

    assert [kw for kw, _ in built] == [{"api_key": "key-one-not-real"},
                                      {"api_key": "key-two-not-real"}]
    assert [s["headers"]["x-goog-api-key"] for s in seen] == \
        ["key-one-not-real", "key-one-not-real", "key-two-not-real"]
    assert not built[0][1]._api_client._httpx_client.is_closed


def test_gemini_concurrent_embeds_from_threads_share_one_client(monkeypatch):
    """Four emb-pool threads (search._EXECUTOR) plus the server loop embed at
    once. Six threads, each on its own asyncio.run, race a cold cache: one
    client is built, all six requests are in flight on it at the same time
    (the handler's barrier only opens when all six have arrived), and every
    thread gets its own vectors, in its own order."""
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "test-key-not-real")
    n_threads = 6
    in_flight = threading.Barrier(n_threads, timeout=10)

    def answer(texts):
        in_flight.wait()
        return [[float(t.split("-")[0]), float(t.split("-")[1])] + [0.0] * (GEMINI_DIMS - 2)
                for t in texts]

    seen = []
    built = _route_default_client_through(
        monkeypatch, _capture(seen, answer), build_delay=0.05)
    provider = get_provider(GEMINI_SLUG)
    sleeps = _record_sleeps(provider)
    start = threading.Barrier(n_threads, timeout=10)
    results, errors = {}, []

    def worker(i):
        texts = [f"{i}-{j}" for j in range(i + 1)]
        try:
            start.wait()
            results[i] = asyncio.run(provider.embed(texts, "document"))
        except Exception as e:  # surfaced below, not swallowed
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [] and sleeps == []
    assert len(built) == 1
    assert len(seen) == n_threads
    assert len({s["thread"] for s in seen}) == n_threads
    for i in range(n_threads):
        assert [(v[0], v[1]) for v in results[i]] == [(float(i), float(j)) for j in range(i + 1)]


@pytest.mark.asyncio
async def test_gemini_embed_does_not_block_the_event_loop(monkeypatch):
    """The watcher and migrations embed on the server's one event loop: the
    sync HTTP call runs on a worker thread, so the loop keeps ticking while a
    slow response is outstanding."""
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "test-key-not-real")
    seen = []
    capture = _capture(seen)

    def slow(request):
        time.sleep(0.3)
        return capture(request)

    _route_default_client_through(monkeypatch, slow)
    provider = get_provider(GEMINI_SLUG)
    ticks = 0
    done = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    tick_task = asyncio.create_task(ticker())
    try:
        result = await provider.embed(["slow"], "query")
    finally:
        done.set()
        await tick_task
    assert result == [[0.0] * GEMINI_DIMS]
    assert ticks >= 10  # a blocked loop ticks once
    assert seen[0]["thread"] != threading.get_ident()


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose,task_type", [("document", "RETRIEVAL_DOCUMENT"),
                                               ("query", "RETRIEVAL_QUERY")])
@pytest.mark.parametrize("slug", sorted(
    s for s, e in EMBEDDING_MODELS.items() if e["provider"] == "gemini"))
async def test_gemini_wire_request_matches_the_legacy_shape(monkeypatch, slug, purpose,
                                                            task_type):
    """Through the REAL google-genai SDK (httpx MockTransport, no network),
    the provider's default shared client sends: one POST to
    {model}:batchEmbedContents on the Developer API with the key in a header,
    and per text one request with the fields the legacy SDK's embedContent
    carried (model, content.parts = [the text], taskType) and no
    outputDimensionality. Byte for byte, body and headers, that is what the
    async client.aio.models.embed_content call this provider made before the
    sync client sent. Per model: given plain strings, the SDK folds them into
    one multi-part content for gemini-embedding-2 only, hence a Content each."""
    from google import genai
    from google.genai import types as genai_types

    dims = EMBEDDING_MODELS[slug]["dims"]
    model_id = EMBEDDING_MODELS[slug]["model_id"]
    texts = ["first text", "second text"]
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "test-key-not-real")

    # the reference: the async call the provider made before this change
    before = []
    reference = genai.Client(
        api_key="test-key-not-real",
        http_options=genai_types.HttpOptions(
            async_client_args={"transport": httpx.MockTransport(_capture(before))}),
    )
    async with reference.aio as aio:
        await aio.models.embed_content(
            model=model_id,
            contents=[genai_types.Content(parts=[genai_types.Part(text=t)]) for t in texts],
            config=genai_types.EmbedContentConfig(
                task_type=task_type,
                http_options=genai_types.HttpOptions(
                    timeout=int(providers.GEMINI_EMBED_TIMEOUT_S * 1000))),
        )

    seen = []
    _route_default_client_through(monkeypatch, _capture(seen))
    provider = GeminiProvider(slug, EMBEDDING_MODELS[slug])
    result = await provider.embed(texts, purpose)

    assert result == [[0.0] * dims, [1.0] * dims]
    assert len(seen) == 1 and len(before) == 1
    sent, ref = seen[0], before[0]
    for field in ("method", "url", "content", "headers", "timeout"):
        assert sent[field] == ref[field], field
    assert sent["method"] == "POST"
    assert sent["url"].startswith("https://generativelanguage.googleapis.com/")
    assert sent["url"].endswith(f"/{model_id}:batchEmbedContents")
    assert sent["headers"]["x-goog-api-key"] == "test-key-not-real"
    # the SDK deadline reaches httpx per phase: it is what frees the worker
    # thread a hung request runs on (asyncio.wait_for cannot)
    deadline = providers.GEMINI_EMBED_TIMEOUT_S
    assert sent["timeout"] == {"connect": deadline, "read": deadline,
                               "write": deadline, "pool": deadline}
    requests_ = sent["json"]["requests"]
    assert [r["content"]["parts"] for r in requests_] == \
        [[{"text": "first text"}], [{"text": "second text"}]]
    for r in requests_:
        assert r["model"] == model_id
        assert r["taskType"] == task_type
        assert "outputDimensionality" not in r
        assert "title" not in r
        assert "role" not in r["content"]  # the legacy SDK sent none either


# ── purpose validation ───────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_purpose", ["retrieval_document", "doc", "", None])
async def test_invalid_purpose_raises_valueerror(bad_purpose):
    provider = get_provider(GEMINI_SLUG)
    embed = _with_gemini_client(provider, _gemini_mock_client())
    with pytest.raises(ValueError):
        await provider.embed(["hello"], purpose=bad_purpose)
    embed.assert_not_called()


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
        embed = _with_gemini_client(provider, _gemini_mock_client(dims))
        await provider.embed([text], purpose="document")
        sent = _sent_texts(embed.call_args)[0]
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
    good = _gemini_response([[0.0] * GEMINI_DIMS])
    embed = _with_gemini_client(provider, _gemini_mock_client(
        side_effect=[RuntimeError("boom1"), RuntimeError("boom2"), good]))
    result = await provider.embed(["hello"], purpose="document")
    assert result == [[0.0] * GEMINI_DIMS]
    assert sleeps == [1.0, 2.0]
    assert embed.call_count == 3


@pytest.mark.asyncio
async def test_retry_exhaustion_raises_provider_error():
    provider = get_provider(GEMINI_SLUG)
    sleeps = _record_sleeps(provider)
    embed = _with_gemini_client(provider, _gemini_mock_client(
        side_effect=RuntimeError("api down")))
    with pytest.raises(EmbeddingProviderError):
        await provider.embed(["hello"], purpose="document")
    assert sleeps == [1.0, 2.0, 4.0]
    assert embed.call_count == 4  # initial attempt + 3 retries


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
    embed = _with_gemini_client(provider, _gemini_mock_client(GEMINI_DIMS - 1))
    with pytest.raises(EmbeddingProviderError):
        await provider.embed(["hello"], purpose="document")
    assert embed.call_count == 1  # guard fires after a "successful" call — no retries
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
    embed = _with_gemini_client(provider, _gemini_mock_client())
    assert await provider.embed([], purpose="document") == []
    embed.assert_not_called()


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
async def test_localstack_failures_feed_the_embeddings_breaker(_localstack_base):
    """GET /local-models/status says "member failing" while the front door
    is up (audit O19-local-stack-05). Reporting only — the retry envelope
    above is unchanged — and a success closes it again."""
    from Orchestrator import local_stack
    provider = get_provider(LOCALSTACK_SLUG)
    _record_sleeps(provider)
    _localstack_with_mock_transport(provider, [], LOCALSTACK_DIMS, status=500)
    with pytest.raises(EmbeddingProviderError):
        await provider.embed(["text"], purpose="document")
    st = local_stack.breaker_state("embeddings")
    assert st["state"] == "open" and "HTTP 500" in st["last_error"]
    _localstack_with_mock_transport(provider, [], LOCALSTACK_DIMS)
    await provider.embed(["text"], purpose="document")
    assert local_stack.breaker_state("embeddings")["state"] == "closed"


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
