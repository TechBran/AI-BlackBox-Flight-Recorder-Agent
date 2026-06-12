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

The active VectorStore is the ONLY search source: the Task-5 inline-JSON
fallback (pre-transcode window) was removed in Task 16 — Task 4's transcode
moves inline vectors into the binary store on the first post-merge boot, so
an empty store simply means nothing is embedded yet and yields [].
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

# Provider coroutines run via per-call asyncio.run on this pool's threads
# (fresh ephemeral loop per call — safe because providers create their network
# clients per call; see providers.py — so no loop-bound state leaks between
# calls or threads). Callers are sync functions invoked from plain threads
# (checkpoint mint path, APScheduler executors) and occasionally from the
# uvloop event-loop thread itself; in the latter case the blocking .result()
# stalls the loop for the duration of the embed call — same as the legacy
# blocking monitoring.generate_embedding. Four workers bound concurrent embeds
# without serializing mints behind searches: a single worker queues every
# embed box-wide behind the slowest in-flight call (e.g. a provider retry
# storm). Never call generate_embedding_sync FROM the pool's own "emb"
# threads: N nested calls from pool threads would exhaust the pool and
# deadlock (no caller does this — all production callers are external).
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="emb")


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

    # Opening the store can raise (corrupt dir, dims mismatch) — legacy
    # semantic_search never raised, and callers like agent_context.py catch
    # ValueError believing it means bad operator input. Log + empty instead
    # (protective catch kept from Task 5; the inline-scan fallback it used to
    # feed was removed in Task 16).
    try:
        store = get_active_store()
    except Exception as e:
        print(f"[SEMANTIC] active store unavailable ({e}): returning no results")
        return []
    if store.count == 0:
        # Nothing embedded yet (fresh box, or a just-switched model whose
        # backfill hasn't landed a row) — skip the index load entirely.
        return []
    allowed_ids = None
    if operator and operator != "system":
        from Orchestrator.fossils import load_snapshot_index  # lazy: avoid cycle
        allowed_ids = {
            snap_id
            for snap_id, entry in load_snapshot_index().items()
            if entry.get("operator") == operator
        }
    return store.search(query_embedding, k, allowed_ids)
