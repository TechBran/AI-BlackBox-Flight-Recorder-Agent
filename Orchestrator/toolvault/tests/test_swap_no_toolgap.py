"""Regression: a model swap must never open a semantic tool-selection GAP.

THE BUG (proven, closed by this test's subject):
    When the active embedding model is swapped, the ToolVault query embed
    (embeddings.search.generate_embedding_sync, governed by get_active_slug())
    flips to the new model the instant migrate sets the active pointer. If the
    live ToolVault/embeddings.json still held the OLD model's vectors at that
    instant, cosine_similarity would return 0.0 for EVERY tool (dim mismatch),
    so semantic-only tools (use_computer, the CLI-agent tools) would vanish
    from the model's system prompt for the seconds a fire-and-forget re-embed
    took. The fix precomputes the target-model tool vectors while the OLD
    pointer is still live, then flips + promotes them back-to-back.

INVARIANT UNDER TEST:
    After run_migration() cuts over from a model with N dims to one with M
    dims, a tool query embedded under the NEW active model finds tool vectors
    of MATCHING dims (semantic score > 0) — never an all-zero-cosine gap.

MUTATION CHECK (how to reproduce RED — the test's teeth):
    In migrate._run_engine's cutover, delete/skip the promote step
    (``await asyncio.to_thread(save_embeddings_store, new_tool_store, None)``),
    i.e. revert to the old "flip now, re-embed the tool store later"
    ordering. The on-disk tool store then keeps its OLD-model (N-dim) vectors
    while the pointer already points at the M-dim model, so the query below
    scores 0.0 and this test FAILS. Restoring the promote turns it GREEN.

Hermetic: providers are fakes in providers._instances (per-slug dims), the
snapshot corpus is EMPTY (cutover is immediate), the ToolVault store lives in
tmp_path, and the canonical tool list is a single stubbed tool.
"""

import asyncio
import json
import threading

import pytest

from Orchestrator import config, fossils
from Orchestrator.embeddings import migrate, providers, search
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import get_active_slug, set_active_slug
from Orchestrator.toolvault import embeddings as tv_embeddings
from Orchestrator.toolvault import registry as tv_registry

OLD_SLUG = "gemini-embedding-001"      # active BEFORE the swap
NEW_SLUG = "qwen3-embedding-0.6b"      # migration target
OLD_DIMS = EMBEDDING_MODELS[OLD_SLUG]["dims"]   # 3072
NEW_DIMS = EMBEDDING_MODELS[NEW_SLUG]["dims"]   # 1024
assert OLD_DIMS != NEW_DIMS, "test needs two models with DIFFERENT dims"

TOOL = "use_computer"
TOOL_DESC = "drive the computer: move the mouse, click, type, take a screenshot"


class DimFakeProvider:
    """Returns a constant unit vector sized to a fixed dim, for any purpose.

    Matching-dim query vs tool vector → cosine 1.0; mismatched-dim → 0.0.
    """

    def __init__(self, dims):
        self.dims = dims

    async def embed(self, texts, purpose):
        return [[1.0] * self.dims for _ in texts]


@pytest.fixture
def swap_env(tmp_path, monkeypatch):
    """Empty corpus + tmp stores + tmp ToolVault store + fake providers."""
    stores_dir = tmp_path / "embeddings"
    index_path = tmp_path / "snapshot_index.json"
    volume_path = tmp_path / "volume.txt"
    tv_store_path = tmp_path / "toolvault_embeddings.json"

    # Empty snapshot index → the diff loop breaks immediately and cuts over.
    index_path.write_text("{}", encoding="utf-8")
    volume_path.write_bytes(b"")

    monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(stores_dir))
    monkeypatch.setattr(config, "VOL_PATH", volume_path)
    monkeypatch.setattr(fossils, "SNAPSHOT_INDEX", index_path)
    monkeypatch.setattr(fossils, "_index_cache", None)
    monkeypatch.setattr(fossils, "_index_cache_mtime", 0.0)

    # Reset migrate + search singletons (mirror test_embeddings_migrate env).
    monkeypatch.setattr(migrate, "_JOB", None)
    monkeypatch.setattr(migrate, "_JOB_TASK", None)
    monkeypatch.setattr(migrate, "_CANCEL", threading.Event())
    monkeypatch.setattr(migrate, "BATCH_SLEEP_S", 0.0)
    monkeypatch.setattr(search, "_active_store", None)

    # The cutover's post-hooks must not touch the network.
    async def _no_health():
        return {"state": "ok"}
    from Orchestrator.embeddings import watcher
    monkeypatch.setattr(watcher, "run_health_check", _no_health)
    # Only reached on the FALLBACK path (precompute failure) — stub it so a
    # regression there can't spawn a real network re-embed thread.
    monkeypatch.setattr(migrate, "_toolvault_cutover_hook", lambda slug: None)

    # ToolVault store on a tmp path; canonical = one semantic-only tool.
    monkeypatch.setattr(tv_embeddings, "EMBEDDINGS_PATH", tv_store_path)
    monkeypatch.setattr(
        tv_registry, "load_canonical",
        lambda *a, **k: [{"name": TOOL, "description": TOOL_DESC}],
    )

    # Fake providers keyed per slug (different dims).
    monkeypatch.setitem(providers._instances, OLD_SLUG, DimFakeProvider(OLD_DIMS))
    monkeypatch.setitem(providers._instances, NEW_SLUG, DimFakeProvider(NEW_DIMS))

    # Live pointer starts on the OLD model, and the on-disk tool store is
    # consistent with it: a use_computer vector in the OLD model's space.
    set_active_slug(OLD_SLUG)
    tv_embeddings.save_embeddings_store(
        {TOOL: {"hash": tv_embeddings._emb_hash(TOOL_DESC),
                "model": OLD_SLUG, "vector": [0.5] * OLD_DIMS}},
        tv_store_path,
    )
    return tv_store_path


def test_swap_leaves_no_semantic_tool_gap(swap_env):
    tv_store_path = swap_env

    result = asyncio.run(migrate.run_migration(NEW_SLUG))

    # Cutover landed on the new model.
    assert result["state"] == "done"
    assert get_active_slug() == NEW_SLUG

    # The live tool store was promoted into the NEW model's space at flip time
    # (this is exactly what the fire-and-forget bug failed to do in-window).
    promoted = tv_embeddings.load_embeddings_store(tv_store_path)
    entry = promoted[TOOL]
    assert entry["model"] == NEW_SLUG
    assert len(entry["vector"]) == NEW_DIMS      # NOT still the old dims

    # THE INVARIANT: a query embedded under the now-active NEW model finds the
    # tool with matching dims → a real (non-zero) semantic score, no gap.
    query_vec = tv_embeddings.embed_query("please control my computer screen")
    assert query_vec is not None
    assert len(query_vec) == NEW_DIMS

    results = tv_embeddings.semantic_search_store(query_vec, promoted, limit=5)
    assert results, "semantic search returned nothing after the swap"
    top_name, top_score = results[0]
    assert top_name == TOOL
    assert top_score > 0.0, (
        "all-zero-cosine gap: tool vectors did not match the active model's "
        "dims at cutover — the promote step regressed"
    )
