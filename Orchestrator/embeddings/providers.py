"""Pluggable embeddings — provider layer (Task 3).

One async interface over Gemini / OpenAI / Ollama:

    provider = get_provider(slug)                 # cached per registry slug
    vectors  = await provider.embed(texts, purpose)   # purpose: document|query

Shared semantics (all providers):
- each text truncated to EMBEDDING_MAX_CHARS + "..." (matches the legacy
  monitoring.generate_embedding behavior)
- 3 retries after the initial attempt, exponential backoff 1s/2s/4s
  (`_sleep` is an instance attribute so tests can record instead of sleep)
- EmbeddingProviderError after final failure — callers decide whether to
  swallow (mint path) or surface (migration/preflight)
- every returned vector must match the registry dims for the slug; a
  mismatch means a vendor-side dim change or a wrong-model response

The purpose parameter exists because the legacy code embedded QUERIES with
task_type="retrieval_document" — Gemini queries now map to retrieval_query,
and Ollama/Qwen3 queries get the registry query_instruction prefix.
"""
import asyncio
import threading

import google.generativeai as genai
import httpx
import openai

from Orchestrator import config
from Orchestrator.embeddings.registry import EMBEDDING_MAX_CHARS, EMBEDDING_MODELS


class EmbeddingProviderError(Exception):
    """Embedding failed after all retries, or the response was malformed."""


_BACKOFF_SECONDS = (1.0, 2.0, 4.0)          # sleep between attempts
_MAX_ATTEMPTS = len(_BACKOFF_SECONDS) + 1   # initial attempt + 3 retries

_GEMINI_TASK_TYPES = {"document": "retrieval_document", "query": "retrieval_query"}

# Per-request deadline for Gemini embeds. request_options={"timeout": ...} is
# the real gRPC deadline (frees the SDK call server-side); the asyncio.wait_for
# at 2x in GeminiProvider._embed is the outer guard that unpins the embed-pool
# worker even if the gRPC deadline never fires (DNS/connect hangs outside the
# deadline scope) — without it a hung embed pins a worker forever.
GEMINI_EMBED_TIMEOUT_S = 60.0

# Ollama runs models on local CPU; a cold large model (e.g. Qwen3-8B, ~4.7GB)
# loading off disk AND embedding a batch of 8 full documents is LEGITIMATELY
# slow — measured ~51s warm, longer cold/under memory contention. Unlike a
# cloud API (where a long read means a hung connection), a slow local read is
# the model working. A 120s cap timed out the first cold 8B batch and the
# disconnect left Ollama still computing, so every retry hit a busy daemon —
# the whole 4-attempt envelope failed. Read cap is generous; connect stays
# short so a genuinely dead daemon still fails fast.
OLLAMA_READ_TIMEOUT_S = 600.0


def _truncate(text: str) -> str:
    if len(text) > EMBEDDING_MAX_CHARS:
        return text[:EMBEDDING_MAX_CHARS] + "..."
    return text


class _BaseProvider:
    def __init__(self, slug: str, entry: dict):
        self.slug = slug
        self.entry = entry
        self.model_id = entry["model_id"]
        self.dims = entry["dims"]
        self._sleep = asyncio.sleep  # injectable for tests

    async def embed(self, texts: list[str], purpose: str) -> list[list[float]]:
        if purpose not in ("document", "query"):
            raise ValueError(
                f"purpose must be 'document' or 'query', got {purpose!r}"
            )
        if not texts:
            return []
        texts = [_truncate(t) for t in texts]

        for attempt in range(_MAX_ATTEMPTS):
            try:
                vectors = await self._embed(texts, purpose)
                break
            except Exception as e:
                if attempt == _MAX_ATTEMPTS - 1:
                    raise EmbeddingProviderError(
                        f"{self.slug}: embedding failed after {_MAX_ATTEMPTS} attempts: {e}"
                    ) from e
                print(
                    f"[EMBEDDING] {self.slug} attempt {attempt + 1}/{_MAX_ATTEMPTS} "
                    f"failed: {type(e).__name__}: {e}"
                )
                await self._sleep(_BACKOFF_SECONDS[attempt])

        # dims guard outside the retry loop: a wrong-sized vector is a
        # wrong-model/vendor-change signal, not a transient fault
        if len(vectors) != len(texts):
            raise EmbeddingProviderError(
                f"{self.slug}: got {len(vectors)} vectors for {len(texts)} texts"
            )
        for vec in vectors:
            if len(vec) != self.dims:
                raise EmbeddingProviderError(
                    f"{self.slug}: provider returned {len(vec)}-dim vector, expected {self.dims}"
                )
        return vectors

    async def _embed(self, texts: list[str], purpose: str) -> list[list[float]]:
        raise NotImplementedError


class GeminiProvider(_BaseProvider):
    async def _embed(self, texts, purpose):
        task_type = _GEMINI_TASK_TYPES[purpose]
        vectors = []
        for text in texts:  # one call per text; SDK batching optional later
            # Double timeout (see GEMINI_EMBED_TIMEOUT_S): gRPC deadline via
            # request_options + wait_for outer guard. wait_for's TimeoutError
            # feeds the retry loop like any other transient failure (the
            # orphaned to_thread worker dies when the gRPC deadline fires).
            result = await asyncio.wait_for(
                asyncio.to_thread(  # sync SDK — keep the loop free
                    genai.embed_content,
                    model=self.model_id,
                    content=text,
                    task_type=task_type,
                    request_options={"timeout": GEMINI_EMBED_TIMEOUT_S},
                ),
                timeout=GEMINI_EMBED_TIMEOUT_S * 2,
            )
            vectors.append(list(result["embedding"]))
        return vectors


class OpenAIProvider(_BaseProvider):
    def __init__(self, slug, entry):
        super().__init__(slug, entry)
        # client per call — cached clients bind their connection pool to the
        # creating event loop; the sync mint bridge uses ephemeral loops (see
        # CU cross-loop queue scar). Factory is injectable for tests; the
        # client is only constructed inside _embed (lazy: AsyncOpenAI() raises
        # without an API key, and import must work without one).
        self._client_factory = lambda: openai.AsyncOpenAI(
            api_key=config.OPENAI_API_KEY or None
        )

    async def _embed(self, texts, purpose):
        # purpose unused: the OpenAI embeddings API has no task types
        async with self._client_factory() as client:
            resp = await client.embeddings.create(model=self.model_id, input=texts)
        items = sorted(resp.data, key=lambda item: item.index)
        return [list(item.embedding) for item in items]


class OllamaProvider(_BaseProvider):
    # local CPU embeds of large batches are slow; connects should fail fast
    TIMEOUT = httpx.Timeout(OLLAMA_READ_TIMEOUT_S, connect=5.0)

    def __init__(self, slug, entry):
        super().__init__(slug, entry)
        self._transport = None  # tests inject httpx.MockTransport

    async def _embed(self, texts, purpose):
        instruction = self.entry.get("query_instruction")
        if purpose == "query" and instruction is not None:
            texts = [instruction + t for t in texts]
        payload = {"model": self.model_id, "input": texts}
        # Effective keep_alive = per-box override (wizard toggle) → registry
        # default → this entry's value (synthetic test entries). Read fresh per
        # call so a live toggle takes effect on the next embed without a restart.
        from Orchestrator.embeddings import store  # lazy: avoid import cycle
        keep_alive = store.get_keep_alive(
            self.slug, fallback=self.entry.get("keep_alive")
        )
        if keep_alive is not None:  # omit the key entirely when None
            payload["keep_alive"] = keep_alive
        async with httpx.AsyncClient(
            timeout=self.TIMEOUT, transport=self._transport
        ) as client:
            resp = await client.post(
                f"{config.OLLAMA_BASE_URL}/api/embed", json=payload
            )
            resp.raise_for_status()
            return resp.json()["embeddings"]


_PROVIDER_CLASSES = {
    "gemini": GeminiProvider,
    "openai": OpenAIProvider,
    "ollama": OllamaProvider,
}

_instances: dict = {}
_instances_lock = threading.Lock()


def get_provider(slug: str):
    """Cached provider instance for a registry slug; ValueError if unknown."""
    entry = EMBEDDING_MODELS.get(slug)
    if entry is None:
        raise ValueError(f"Unknown embedding-model slug: {slug!r}")
    with _instances_lock:
        provider = _instances.get(slug)
        if provider is None:
            provider = _PROVIDER_CLASSES[entry["provider"]](slug, entry)
            _instances[slug] = provider
        return provider
