"""
ToolVault Embeddings - Semantic vector generation and search.

Vector generation rides the SHARED embedding provider layer
(``Orchestrator.embeddings.search.generate_embedding_sync``): whatever model
is active for snapshot embeddings (gemini / openai / ollama-qwen3) also
embeds tool descriptions. The key difference from snapshots: ToolVault
embeds only the DESCRIPTION field — a focused, high-signal target for
semantic retrieval.

Cache scheme (``ToolVault/embeddings.json`` — the only cached artifact)::

    { "<tool_name>": {"hash":   "<sha256 of the description>",
                      "model":  "<active model slug at embed time>",
                      "vector": [...]} }

An entry is fresh only when BOTH its ``hash`` matches the current description
AND its ``model`` matches the current ACTIVE slug — switching embedding models
therefore invalidates the whole cache cleanly (the migration job's cutover
hook re-syncs it). Legacy pre-slug entries (``model`` holding an old genai
model-id literal, or missing entirely) never match a registry slug and are
treated as stale: re-embedded lazily on the next sync.

Imports of the shared layer are LAZY (inside functions): the embeddings
package and toolvault must never import each other at module level (the
ToolResult→context.py import cycle bit this codebase before).

This enables the core ToolVault promise: given a natural language
prompt like "send a text message", find the right tool (send_sms)
without the model needing to see all 41+ tool schemas.
"""

import hashlib
import json
import math
import operator
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

from Orchestrator.toolvault.config import (
    PROJECT_ROOT,
    KEYWORD_WEIGHT,
    SEMANTIC_WEIGHT,
    DEFAULT_SEARCH_LIMIT,
    SIMILARITY_THRESHOLD,
)

# ---------------------------------------------------------------------------
# Embedding cache store (Task 2.1)
# ---------------------------------------------------------------------------
# The store is the ONLY cached artifact in ToolVault v2. Tool modules are the
# source of truth; embeddings are regenerated when a tool's DESCRIPTION
# changes (sha256 hash) OR the active embedding model changes (slug). Format:
#   { "<tool_name>": {"hash": "<sha256>", "model": "<slug>", "vector": [..]} }
#
# Module global so tests can override it (read at call time, not captured).
EMBEDDINGS_PATH = PROJECT_ROOT / "ToolVault" / "embeddings.json"


# ---------------------------------------------------------------------------
# Embedding Generation (shared provider layer; lazy imports — no cycle)
# ---------------------------------------------------------------------------

def _active_slug() -> str:
    """The shared layer's active embedding-model slug (lazy import)."""
    from Orchestrator.embeddings.search import get_active_slug

    return get_active_slug()


def embed_tool_description(
    description: str, slug: Optional[str] = None
) -> Optional[List[float]]:
    """Embed a tool description via the shared active provider.

    purpose="document": this text will be searched against. Truncation and
    retry/backoff live in the provider layer. Returns None on failure — the
    shared layer's contract, identical to the old in-module behavior.

    ``slug`` pins the embedding model; omitting it (the default) resolves the
    live active pointer exactly as before. An explicit slug lets the sync run
    under a TARGET model while the live pointer still points elsewhere (the
    model-switch precompute path).
    """
    from Orchestrator.embeddings.search import generate_embedding_sync

    return generate_embedding_sync(description, purpose="document", slug=slug)


# A query vector is a pure function of (embedding model, text), and one chat
# turn injects twice with the same prompt (the system-prompt tool instructions,
# then the provider's tools array), so the second pass reuses the first one's
# vector instead of another provider round trip (audit O17-toolvault-runtime-02).
# A failed embed (None) is reused only briefly: long enough that a down provider
# is not paid for twice in one turn (its retries take ~7 s), short enough that a
# recovery is seen on the next turn. The key also carries the embed function
# and the slug's provider instance (the one that made the vector: the embed
# itself creates it on a box's first query), so swapping either (a
# re-registered provider, a test double) never serves a vector made by another.
QUERY_VEC_TTL_S = 300.0
QUERY_VEC_FAIL_TTL_S = 10.0
_QUERY_VEC_MAX = 16
_query_vecs: "OrderedDict[tuple, Tuple[float, Optional[List[float]]]]" = OrderedDict()
_query_vecs_lock = threading.Lock()
_monotonic = time.monotonic  # module global so tests can drive the clock


class _Same:
    """A memo-key part that compares by identity and keeps its object alive, so
    a freed object's address (id) can never be mistaken for a new one's."""

    __slots__ = ("obj",)

    def __init__(self, obj):
        self.obj = obj

    def __hash__(self):
        return id(self.obj)

    def __eq__(self, other):
        return isinstance(other, _Same) and other.obj is self.obj


def _query_key(slug: str, query: str) -> tuple:
    from Orchestrator.embeddings import providers
    from Orchestrator.embeddings import search

    return (slug, query, _Same(search.generate_embedding_sync),
            _Same(providers._instances.get(slug)))


def _reset_query_cache() -> None:
    """Forget every memoised query vector (the test suite does, per test)."""
    with _query_vecs_lock:
        _query_vecs.clear()


def embed_query(query: str) -> Optional[List[float]]:
    """Embed a search query via the shared active provider.

    purpose="query": optimized for finding relevant docs. None on failure.
    Reused for the same text under the same model (see QUERY_VEC_TTL_S).
    """
    from Orchestrator.embeddings.search import generate_embedding_sync

    slug = _active_slug()
    now = _monotonic()
    with _query_vecs_lock:
        key = _query_key(slug, query)
        hit = _query_vecs.get(key)
        if hit is not None and hit[0] > now:
            _query_vecs.move_to_end(key)
            return hit[1]
    vec = generate_embedding_sync(query, purpose="query")
    ttl = QUERY_VEC_TTL_S if vec else QUERY_VEC_FAIL_TTL_S
    with _query_vecs_lock:
        key = _query_key(slug, query)  # under the provider that made the vector
        _query_vecs[key] = (now + ttl, vec)
        _query_vecs.move_to_end(key)
        while len(_query_vecs) > _QUERY_VEC_MAX:
            _query_vecs.popitem(last=False)
    return vec


# ---------------------------------------------------------------------------
# Embedding cache store + hash-keyed sync (Task 2.1)
# ---------------------------------------------------------------------------

def _emb_hash(text: str) -> str:
    """sha256 hex of the embedding-target text (the tool DESCRIPTION)."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# The parsed store, keyed on (path, mtime_ns, size). Every chat prompt reads
# it, and re-parsing the whole file (5.8 MB, ~48 ms measured) each time was pure
# overhead (audit O17-toolvault-runtime-03). An edit or a save (which also
# drops the entry) is a cache miss.
_store_cache: Dict[str, Any] = {"key": None, "store": None}
_store_cache_lock = threading.Lock()


def load_embeddings_store(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read the embeddings cache store.

    Missing file → ``{}``. Corrupt JSON → ``{}`` (logged, never raised).
    Parsed once per file version; each call gets its own top-level dict, but
    the entries are shared with the cache, so treat them as read-only.
    """
    p = Path(path) if path is not None else EMBEDDINGS_PATH
    try:
        st = p.stat()
    except OSError:
        return {}
    key = (str(p), st.st_mtime_ns, st.st_size)
    with _store_cache_lock:
        if _store_cache["key"] == key:
            return dict(_store_cache["store"])
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            print(f"[TOOLVAULT-EMB] Store at {p} is not an object; treating as empty")
            return {}
    except Exception as e:
        print(f"[TOOLVAULT-EMB] Failed to read store at {p}: {e}; treating as empty")
        return {}
    with _store_cache_lock:
        _store_cache["key"] = key
        _store_cache["store"] = data
        _vector_norms.clear()  # the previous store's vectors are gone
    return dict(data)


def _forget_cached_store() -> None:
    with _store_cache_lock:
        _store_cache["key"] = None
        _store_cache["store"] = None


def save_embeddings_store(store: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Atomically write the embeddings store (.part + os.replace).

    Mirrors manifest.py:save_manifest's crash-safe pattern.
    """
    p = Path(path) if path is not None else EMBEDDINGS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".part")
    try:
        data = json.dumps(store, indent=2, ensure_ascii=False).encode("utf-8")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        _forget_cached_store()
    except Exception as e:
        print(f"[TOOLVAULT-EMB] Failed to save store: {e}")
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def sync_embeddings(
    canonical: List[Dict[str, Any]],
    path: Optional[Path] = None,
    *,
    force: bool = False,
    target_slug: Optional[str] = None,
    save: bool = True,
) -> Dict[str, Any]:
    """Sync the embedding cache against the canonical tool list.

    For each tool, the DESCRIPTION is hashed (sha256). A tool is re-embedded
    when its hash changed, its cached entry was embedded under a different
    model slug than the currently ACTIVE one (model switch → clean
    invalidation; legacy entries without a slug never match), ``force=True``,
    or no usable cached vector exists. Tools no longer present in
    ``canonical`` are pruned. The store is written atomically and returned.

    Embed failures (``embed_tool_description`` returns ``None``) never crash:
    any prior entry is kept intact; new tools are simply skipped.

    Args:
        canonical: list of schema dicts (each with ``name`` + ``description``).
        path: store path override (defaults to ``EMBEDDINGS_PATH``).
        force: re-embed every tool regardless of hash.
        target_slug: embed (and cache-key) under THIS model instead of the live
            active pointer. Omitting it (the default) resolves ``_active_slug()``
            exactly as before. The model-switch cutover passes the migration
            target so the store is computed in the NEW model's vector space
            while the OLD pointer is still live.
        save: when True (default) write the store to ``path`` atomically, as
            always. When False, SKIP the write and just return the computed
            dict — the cutover promotes it with a single atomic write of its own
            immediately after flipping the active pointer, so the live store is
            never left inconsistent with the pointer.

    Returns:
        The new store dict.
    """
    existing = load_embeddings_store(path)
    new_store: Dict[str, Any] = {}

    active = target_slug or _active_slug()
    embedded = 0
    skipped = 0
    canonical_names = set()

    for tool in canonical:
        name = tool.get("name")
        if not name:
            continue
        canonical_names.add(name)
        description = tool.get("description", "") or ""
        h = _emb_hash(description)

        prior = existing.get(name)
        prior_ok = (
            isinstance(prior, dict)
            and prior.get("model") == active
            and prior.get("hash") == h
            and bool(prior.get("vector"))
        )

        if not force and prior_ok:
            new_store[name] = prior
            skipped += 1
            continue

        vec = embed_tool_description(description, slug=active)
        if vec:
            new_store[name] = {"hash": h, "model": active, "vector": vec}
            embedded += 1
        else:
            # Embed failed: keep any prior entry intact; otherwise skip the tool.
            print(f"[TOOLVAULT-EMB] embed failed for '{name}'; "
                  f"{'keeping prior entry' if isinstance(prior, dict) else 'skipping'}")
            if isinstance(prior, dict):
                new_store[name] = prior
            skipped += 1

    pruned = len([n for n in existing if n not in canonical_names])

    if save:
        save_embeddings_store(new_store, path)
    print(f"[TOOLVAULT-EMB] embedded={embedded} skipped={skipped} pruned={pruned}"
          f"{'' if save else ' (computed, not saved)'}")
    return new_store


def sync_tool_embeddings_bg(reason: str = "") -> "object":
    """Fire-and-forget (re)embed of every ToolVault tool under the ACTIVE model,
    in a daemon thread. Returns the started Thread (tests join it).

    Idempotent (hash+model keyed) → a fast no-op when the cache is already warm,
    and it embeds whatever is stale/missing the MOMENT an embedding provider
    first becomes reachable. NEVER raises.

    The point of this seam is the fresh-box guarantee: ``embeddings.json`` is
    gitignored (each box embeds its own tool vectors under its OWN chosen model),
    and a box that keeps the DEFAULT embedding model never triggers an
    ``/embeddings/migrate`` — so nothing would embed the tools between first boot
    (no key yet → boot sync fails) and a manual restart. Calling this right after
    onboarding writes API keys makes semantic tool selection self-heal with no
    restart. ``registry`` is imported lazily inside the thread (module-level
    import-cycle guard — see this module's header)."""
    import threading

    def _run():
        try:
            from Orchestrator.toolvault import registry as tv_registry
            canonical = tv_registry.load_canonical()
            store = sync_embeddings(canonical)
            print(f"[TOOLVAULT-EMB] background sync ({reason or 'manual'}) "
                  f"complete: {len(store)} tool vectors cached")
        except Exception as e:  # noqa: BLE001 — background heal must never raise
            print(f"[TOOLVAULT-EMB] background sync ({reason or 'manual'}) "
                  f"failed (non-fatal): {type(e).__name__}: {e}")

    t = threading.Thread(target=_run, name="toolvault-embed-sync-bg", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------

def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """Calculate cosine similarity between two vectors.

    Returns 0.0-1.0 score. Same implementation as monitoring.py.
    A length mismatch scores 0.0 (skip-not-crash): mid-migration the store
    can hold mixed-dims vectors from two models — stale entries simply
    never rank until the sync re-embeds them.
    """
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0

    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    magnitude1 = math.sqrt(sum(a * a for a in vec1))
    magnitude2 = math.sqrt(sum(b * b for b in vec2))

    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0

    return dot_product / (magnitude1 * magnitude2)


def _dot_fallback(a: List[float], b: List[float]) -> float:
    return sum(map(operator.mul, a, b))


# math.sumprod is a C loop (Python 3.12+); the fallback keeps an older
# interpreter working at the old speed.
_dot = getattr(math, "sumprod", None) or _dot_fallback

# Stored vectors' norms, keyed by the vector object's id (the entry keeps the
# vector, so its id cannot be reused while it is here). Cleared whenever the
# cached store is replaced; bounded so ad-hoc stores cannot grow it forever.
_vector_norms: Dict[int, Tuple[List[float], float]] = {}
_VECTOR_NORMS_MAX = 4096


def _norm(vec: List[float]) -> float:
    hit = _vector_norms.get(id(vec))
    if hit is not None and hit[0] is vec:
        return hit[1]
    n = math.sqrt(_dot(vec, vec))
    if len(_vector_norms) >= _VECTOR_NORMS_MAX:
        _vector_norms.clear()
    _vector_norms[id(vec)] = (vec, n)
    return n


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def keyword_search(
    query: str,
    tools: Dict[str, Dict[str, Any]],
    tool_descriptions: Dict[str, str],
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> List[Tuple[str, float]]:
    """Keyword search over tool names and descriptions.

    Simple but effective: tokenize query, score by term overlap
    with tool name and description. Complements semantic search
    for exact-match cases (e.g., searching for "gmail" finds gmail tools).

    Args:
        query: Search query
        tools: {name: entry} dict from manifest
        tool_descriptions: {name: description_text} pre-extracted
        limit: Maximum results

    Returns:
        List of (tool_name, score) sorted by relevance.
    """
    query_lower = query.lower()
    query_tokens = set(query_lower.split())

    scores = []
    for name, desc in tool_descriptions.items():
        score = 0.0
        name_lower = name.lower()
        desc_lower = desc.lower()

        # Exact name match (highest signal)
        if query_lower == name_lower:
            score += 5.0

        # Query tokens found in tool name
        name_parts = set(name_lower.replace("_", " ").split())
        name_overlap = len(query_tokens & name_parts)
        score += name_overlap * 2.0

        # Query tokens found in description
        for token in query_tokens:
            if token in desc_lower:
                score += 1.0

        # Substring match in name
        if query_lower in name_lower or name_lower in query_lower:
            score += 3.0

        # Category match (from entry)
        entry = tools.get(name, {})
        category = entry.get("category", "").lower().replace("_", " ")
        for token in query_tokens:
            if token in category:
                score += 1.5

        if score > 0:
            scores.append((name, score))

    # Normalize scores to 0-1 range
    if scores:
        max_score = max(s for _, s in scores)
        if max_score > 0:
            scores = [(name, s / max_score) for name, s in scores]

    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:limit]


# ---------------------------------------------------------------------------
# Search over the embeddings.json store (Task 2.2)
# ---------------------------------------------------------------------------
# These functions read vectors from the v2 store shape
# ({name: {"hash","model","vector":[...]}}) produced by sync_embeddings.

def semantic_search_store(
    query_vec: List[float],
    store: Dict[str, Dict[str, Any]],
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> List[Tuple[str, float]]:
    """Cosine-similarity search of a pre-embedded query over the store.

    Args:
        query_vec: Already-embedded query vector (caller embeds; no network here)
        store: embeddings.json store {name: {"vector": [...]}}
        limit: Maximum results to return

    Returns:
        Top-``limit`` list of (tool_name, similarity_score) sorted desc.
        Tools with a missing or empty ``vector`` are skipped.
    """
    if not query_vec:
        return []

    # Same scores as cosine_similarity, with the query's norm computed once and
    # each stored vector's norm cached (it was recomputed per vector per prompt).
    q_norm = math.sqrt(_dot(query_vec, query_vec))
    scores = []
    for name, entry in store.items():
        vector = entry.get("vector")
        if not vector:
            continue
        if len(vector) != len(query_vec) or q_norm == 0:
            sim = 0.0
        else:
            v_norm = _norm(vector)
            sim = _dot(query_vec, vector) / (q_norm * v_norm) if v_norm else 0.0
        scores.append((name, sim))

    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:limit]


def hybrid_search_store(
    query: str,
    descriptions: Dict[str, str],
    store: Dict[str, Dict[str, Any]],
    limit: int = DEFAULT_SEARCH_LIMIT,
    threshold: float = SIMILARITY_THRESHOLD,
) -> List[Tuple[str, float]]:
    """Hybrid search (keyword + semantic) over the embeddings.json store.

    Blends keyword score (40%) + semantic score (60%), filtered by
    ``threshold``, sorted desc, top ``limit``.
    The query is embedded once via ``embed_query``; per-method results are
    gathered with no individual threshold (limit*3 candidates) before merging.

    A tool present in ``descriptions`` but absent from ``store`` (no vector yet)
    remains reachable via its keyword score — its semantic contribution is 0.

    Args:
        query: Natural language query
        descriptions: {name: description} for keyword search
        store: embeddings.json store {name: {"vector": [...]}}
        limit: Max results
        threshold: Minimum combined score

    Returns:
        List of (tool_name, combined_score) sorted by relevance.
    """
    query_vec = embed_query(query)
    if query_vec:
        semantic_results = semantic_search_store(
            query_vec, store,
            limit=limit * 3,  # Get extra candidates for merging
        )
    else:
        print("[TOOLVAULT-SEARCH] Query embedding failed")
        semantic_results = []

    keyword_results = keyword_search(
        query, {}, descriptions,
        limit=limit * 3,
    )

    # Build score maps
    semantic_scores = dict(semantic_results)
    keyword_scores = dict(keyword_results)

    # Combine with weights
    all_names = set(semantic_scores.keys()) | set(keyword_scores.keys())
    combined = {}

    for name in all_names:
        kw = keyword_scores.get(name, 0.0)
        sem = semantic_scores.get(name, 0.0)
        combined[name] = (KEYWORD_WEIGHT * kw) + (SEMANTIC_WEIGHT * sem)

    # Filter by threshold and sort
    results = [(name, score) for name, score in combined.items() if score >= threshold]
    results.sort(key=lambda x: x[1], reverse=True)

    Y = "\033[33m"  # Yellow
    R = "\033[0m"   # Reset
    top = results[:limit]
    print(f"{Y}[TOOLVAULT-SEARCH] Hybrid(store): {len(semantic_results)} semantic + "
          f"{len(keyword_results)} keyword → {len(top)} results{R}")
    for name, score in top:
        print(f"{Y}  ├─ {name:30s} score={score:.3f}{R}")

    return top
