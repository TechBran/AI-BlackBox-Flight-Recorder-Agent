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
from datetime import datetime, timezone

from Orchestrator import config
from Orchestrator.embeddings.chunker import chunk_snapshot
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


def active_threshold(fallback: float) -> float:
    """Per-model semantic-similarity floor; `fallback` (the config global) when
    the active model declares none. Registry is the only place model-specific
    values live (Task-16 ratchet), so a model whose score distribution differs
    (Gemini retrieval_query vs Qwen instruct-prefixed) carries its own floor."""
    entry = EMBEDDING_MODELS.get(get_active_slug(), {})
    value = entry.get("semantic_threshold")
    return float(value) if value is not None else float(fallback)


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


# ── fast provider-down health signal (F8) ────────────────────────────────────
#
# semantic_search returns [] gracefully when the active provider's query embed
# fails — but the UI health banner used to stay "ok" until the watcher's next
# pass (WATCH_INTERVAL_OK_S = 24h). That is up to a day of silently-empty
# searches with no signal. A small consecutive-failure counter flips
# health.json to "degraded" after config.EMBEDDINGS_QUERY_FAIL_THRESHOLD
# consecutive QUERY-embed failures, and a success resets it. We reuse the
# watcher's health.json (same dir, same shape) so /embeddings/status surfaces
# it with zero new plumbing — and a single blip (under the threshold) never
# raises a false banner. Everything here is BEST-EFFORT: the call site wraps
# _signal_query_health in try/except so a failed health write can never break
# search.

_query_fail_lock = threading.Lock()
_query_fail_count = 0
_query_degraded_signalled = False  # we wrote degraded; restore ok on recovery


def _reset_query_fail_counter() -> None:
    """Reset the consecutive query-fail counter (startup / tests)."""
    global _query_fail_count, _query_degraded_signalled
    with _query_fail_lock:
        _query_fail_count = 0
        _query_degraded_signalled = False


def _signal_query_health(state: str, detail: str) -> None:
    """Write the watcher's health.json with our degraded/ok signal.

    Reuses watcher._write_health (lazy import — watcher→migrate→search would
    cycle at module load) so the file shape stays identical to the watcher's:
    {state, detail, successor, successor_slug, checked_at}. An ok restore is
    conservative: it only clears a health WE flipped to degraded, never a
    watcher-written broken/superseded banner.
    """
    # lazy import breaks the search↔watcher↔migrate import cycle
    from Orchestrator.embeddings import watcher as _watcher

    if state == "ok":
        # Only clear a degraded WE wrote — never clobber a watcher catalog state.
        try:
            current = _watcher._previous_state()
        except Exception:  # noqa: BLE001 — unreadable health = leave it alone
            current = None
        if current != "degraded":
            return
    _watcher._write_health({
        "state": state,
        "detail": detail,
        "successor": None,
        "successor_slug": None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    })


def _note_query_failure() -> None:
    """One consecutive query-embed failure; flips health degraded at threshold."""
    global _query_fail_count, _query_degraded_signalled
    with _query_fail_lock:
        _query_fail_count += 1
        threshold = config.EMBEDDINGS_QUERY_FAIL_THRESHOLD
        if _query_fail_count >= threshold and not _query_degraded_signalled:
            _query_degraded_signalled = True
            should_signal = True
        else:
            should_signal = False
    if should_signal:
        _signal_query_health(
            "degraded",
            "embedding provider unreachable - search temporarily degraded "
            f"({threshold} consecutive query-embed failures)",
        )


def _note_query_success() -> None:
    """A query embed succeeded: reset the counter; restore ok if we degraded."""
    global _query_fail_count, _query_degraded_signalled
    with _query_fail_lock:
        was_degraded = _query_degraded_signalled
        _query_fail_count = 0
        _query_degraded_signalled = False
    if was_degraded:
        _signal_query_health("ok", "")


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


def embed_snapshot_for_index(text: str) -> dict:
    """Embed one snapshot body for the mint path, shaped to the ACTIVE store.

    Returns update_snapshot_index-ready kwargs (M6 task 6c / audit A4):

    - v1 active store → ``{"embedding": vec}`` — the whole (provider-clamped)
      body embedded ONCE, exactly today's behavior. Schema-derived, not
      flag-derived (audit A6 rollback safety): while production stays v1,
      mint behavior is byte-identical.
    - v2 active store → ``{"chunk_vectors": [v0..vn]}`` — chunk_snapshot
      scoring windows embedded in ONE provider.embed call (the provider layer
      batches: ollama/openai one request, gemini per-text loop), aligned to
      chunk order for a contiguous append_group.
    - any failure (store unavailable, provider down/mid-batch death, empty
      body) → ``{}`` — the mint completes vector-less and the catch-up loop
      (migrate diff / watcher gap-heal keyed on store.missing()) re-embeds
      later. Mirrors generate_embedding_sync's None-on-failure semantics;
      NEVER raises.

    SNAPSHOT-ONLY seam (audit A7): this is the single place chunker output
    meets the provider. generate_embedding_sync stays single-vector for its
    other callers (ToolVault descriptions, watcher probe, queries).
    fossils.update_snapshot_index re-validates the payload shape against the
    store's CURRENT schema at append time, covering the race where the active
    store changed between this embed and the append.
    """
    try:
        store = get_active_store()
        schema = store.schema
        slug = store.slug
    except Exception as e:
        print(f"[EMBEDDING] active store unavailable; snapshot embed skipped: {e}")
        return {}

    if schema != 2:
        vec = generate_embedding_sync(text, purpose="document")
        return {"embedding": vec} if vec else {}

    try:
        chunks = chunk_snapshot(text, model_key=slug)
        if not chunks:
            print(f"[EMBEDDING] {slug}: empty snapshot body — nothing to embed")
            return {}
        provider = get_provider(slug)
        vectors = _run_async(provider.embed(chunks, "document"))
    except Exception as e:
        print(f"[EMBEDDING] {slug}: chunk embedding generation failed: {e}")
        return {}
    if not vectors or len(vectors) != len(chunks):
        # The provider layer already enforces len(vectors) == len(texts);
        # belt-and-braces so a drifting/fake provider can never misalign a
        # chunk group (partial groups must not exist — audit A3).
        print(
            f"[EMBEDDING] {slug}: provider returned {len(vectors or [])} "
            f"vectors for {len(chunks)} chunks — dropped"
        )
        return {}
    return {"chunk_vectors": vectors}


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
        # F8: flip health.json to "degraded" fast after a run of consecutive
        # query-embed failures, instead of waiting up to 24h for the watcher.
        # Best-effort — a health-signal failure must NEVER break search.
        try:
            _note_query_failure()
        except Exception as e:  # noqa: BLE001
            print(f"[SEMANTIC] health signal failed (non-fatal): {e}")
        return []
    # query embed succeeded → clear any consecutive-failure streak (and restore
    # an ok banner if we had flipped it to degraded). Best-effort, never raises.
    try:
        _note_query_success()
    except Exception as e:  # noqa: BLE001
        print(f"[SEMANTIC] health reset failed (non-fatal): {e}")

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
