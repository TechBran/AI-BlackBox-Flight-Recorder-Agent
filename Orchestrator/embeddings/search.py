"""Pluggable embeddings — live search layer (Task 5 cutover).

monitoring.generate_embedding / monitoring.semantic_search delegate here, so
every existing caller (checkpoint mint paths, task_routes, fossils hybrid
search, APScheduler jobs) flows through this module. It owns:

- the ACTIVE VectorStore handle (`get_active_store` / `swap_active` — Task 8's
  migration job calls `swap_active` at cutover)
- the sync→async bridge into the provider layer (`_run_async`)
- the behavior-preserving public functions `generate_embedding_sync` and
  `semantic_search` (exact contracts of the legacy monitoring functions:
  None on embed failure, [] on query-embed failure, operator "" or "system"
  sees ALL snapshots, results are [(snap_id, score)] top-k sorted desc)

Pre-transcode window: until Task 4's transcode has run on a box, the active
store is empty and inline `entry["embedding"]` vectors still live in the
snapshot index — `semantic_search` falls back to the legacy pure-python
cosine loop in that case (removed in Task 16).
"""
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from Orchestrator.embeddings.providers import get_provider
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import VectorStore, get_active_slug, get_store

# ── active-store state ───────────────────────────────────────────────────────

_active_store: VectorStore | None = None
_active_lock = threading.Lock()


def get_active_store() -> VectorStore:
    """The live VectorStore for the active model; lazily opened on first use."""
    global _active_store
    with _active_lock:
        if _active_store is None:
            _active_store = get_store(get_active_slug())
        return _active_store


def swap_active(slug: str) -> VectorStore:
    """Point live searches at another model's store (migration cutover seam).

    Validates the slug against the registry and OPENS the new store before
    taking the lock, so a bad slug / dims-mismatch refusal can never leave
    searches pointed at a half-swapped store. Callers (Task 8) persist the
    pointer via store.set_active_slug() alongside this in-memory swap.
    """
    if slug not in EMBEDDING_MODELS:
        raise ValueError(
            f"unknown embedding model slug {slug!r}; known: {sorted(EMBEDDING_MODELS)}"
        )
    global _active_store
    store = get_store(slug)  # canonical instance, already open()ed
    with _active_lock:
        _active_store = store
    return store


# ── sync→async bridge ────────────────────────────────────────────────────────

# Provider coroutines always run on this ONE dedicated worker thread via
# asyncio.run (fresh ephemeral loop per call — safe because providers create
# their network clients per call; see providers.py). Callers are sync
# functions invoked from plain threads (checkpoint mint path, APScheduler
# executors) and occasionally from the uvloop event-loop thread itself; in the
# latter case the blocking .result() stalls the loop for the duration of the
# embed call — exactly what the legacy blocking monitoring.generate_embedding
# did, so this preserves (not worsens) existing behavior. Never call
# _run_async FROM the "emb" thread itself: with max_workers=1 that would
# deadlock (all production callers are external, so this cannot happen today).
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="emb")


def _run_async(coro):
    """Run a coroutine to completion from sync code, loop or no loop."""
    return _EXECUTOR.submit(asyncio.run, coro).result()


# ── public API (legacy monitoring contracts) ─────────────────────────────────

def generate_embedding_sync(text: str, purpose: str = "document") -> list[float] | None:
    """Embed one text with the active provider; None on failure.

    Exact contract of the legacy monitoring.generate_embedding: prints an
    [EMBEDDING]-prefixed line and returns None instead of raising (mint paths
    treat a missing embedding as a soft failure). Truncation + retry/backoff
    live in the provider layer.
    """
    slug = get_active_slug()
    try:
        provider = get_provider(slug)
        vectors = _run_async(provider.embed([text], purpose))
    except Exception as e:
        print(f"[EMBEDDING] {slug}: embedding generation failed: {e}")
        return None
    if not vectors:
        print(f"[EMBEDDING] {slug}: provider returned no vector")
        return None
    return vectors[0]


def semantic_search(query: str, operator: str = "", k: int = 10) -> list[tuple[str, float]]:
    """Top-k semantic matches as [(snap_id, score), ...], sorted desc.

    Operator rule (unchanged from legacy): "" or "system" sees ALL snapshots;
    any other operator only sees its own index entries. The query is embedded
    with purpose="query" (the retrieval_query fix — legacy embedded queries as
    retrieval_document).
    """
    query_embedding = generate_embedding_sync(query, purpose="query")
    if not query_embedding:
        print("[SEMANTIC] Query embedding failed, falling back to keyword search")
        return []

    store = get_active_store()
    if store.count > 0:
        allowed_ids = None
        if operator and operator != "system":
            from Orchestrator.fossils import load_snapshot_index  # lazy: avoid cycle
            allowed_ids = {
                snap_id
                for snap_id, entry in load_snapshot_index().items()
                if entry.get("operator") == operator
            }
        return store.search(query_embedding, k, allowed_ids)

    # TODO(Task 16): remove inline fallback — covers only the pre-transcode
    # window where the active store is empty but the snapshot index still
    # carries inline "embedding" vectors.
    return _inline_fallback_search(query_embedding, operator, k)


# ── legacy inline-JSON fallback (TEMPORARY — deleted in Task 16) ─────────────

def _cosine(vec1, vec2) -> float:
    """Pure-python cosine — local copy of monitoring.cosine_similarity so the
    fallback scores are bit-identical to the legacy path without importing
    monitoring (which lazily imports this module)."""
    import math

    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    magnitude1 = math.sqrt(sum(a * a for a in vec1))
    magnitude2 = math.sqrt(sum(b * b for b in vec2))
    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0
    return dot_product / (magnitude1 * magnitude2)


def _inline_fallback_search(query_embedding, operator: str, k: int) -> list[tuple[str, float]]:
    """Replicates the legacy monitoring.semantic_search index loop."""
    from Orchestrator.fossils import load_snapshot_index  # lazy: avoid cycle

    index = load_snapshot_index()
    if not index:
        return []
    scores = []
    for snap_id, data in index.items():
        if operator and operator != "system" and data.get("operator") != operator:
            continue
        if not data.get("embedding"):
            continue
        scores.append((snap_id, _cosine(query_embedding, data["embedding"])))
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:k]
