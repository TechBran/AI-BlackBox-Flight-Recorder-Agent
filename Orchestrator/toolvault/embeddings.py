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
import os
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


def embed_tool_description(description: str) -> Optional[List[float]]:
    """Embed a tool description via the shared active provider.

    purpose="document": this text will be searched against. Truncation and
    retry/backoff live in the provider layer. Returns None on failure — the
    shared layer's contract, identical to the old in-module behavior.
    """
    from Orchestrator.embeddings.search import generate_embedding_sync

    return generate_embedding_sync(description, purpose="document")


def embed_query(query: str) -> Optional[List[float]]:
    """Embed a search query via the shared active provider.

    purpose="query": optimized for finding relevant docs. None on failure.
    """
    from Orchestrator.embeddings.search import generate_embedding_sync

    return generate_embedding_sync(query, purpose="query")


# ---------------------------------------------------------------------------
# Embedding cache store + hash-keyed sync (Task 2.1)
# ---------------------------------------------------------------------------

def _emb_hash(text: str) -> str:
    """sha256 hex of the embedding-target text (the tool DESCRIPTION)."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def load_embeddings_store(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read the embeddings cache store.

    Missing file → ``{}``. Corrupt JSON → ``{}`` (logged, never raised).
    """
    p = Path(path) if path is not None else EMBEDDINGS_PATH
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            print(f"[TOOLVAULT-EMB] Store at {p} is not an object; treating as empty")
            return {}
        return data
    except Exception as e:
        print(f"[TOOLVAULT-EMB] Failed to read store at {p}: {e}; treating as empty")
        return {}


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

    Returns:
        The new store dict.
    """
    existing = load_embeddings_store(path)
    new_store: Dict[str, Any] = {}

    active = _active_slug()
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

        vec = embed_tool_description(description)
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

    save_embeddings_store(new_store, path)
    print(f"[TOOLVAULT-EMB] embedded={embedded} skipped={skipped} pruned={pruned}")
    return new_store


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

    scores = []
    for name, entry in store.items():
        vector = entry.get("vector")
        if not vector:
            continue
        sim = cosine_similarity(query_vec, vector)
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
