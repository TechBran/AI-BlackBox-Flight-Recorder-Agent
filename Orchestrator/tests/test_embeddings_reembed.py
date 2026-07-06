"""Re-embed engine + activation seam tests (per-card Re-embed feature)."""
import json

import numpy as np
import pytest

from Orchestrator.embeddings import migrate
from Orchestrator.embeddings.store import get_store, set_active_slug, get_active_slug
from Orchestrator.tests.test_embeddings_migrate_v2 import (   # noqa: F401 — pytest fixtures + helpers
    env, fake_provider, cutover_spies, app, _build_volume, _wait_for_state,
    TARGET, TARGET_DIMS, OLD_SLUG,
)


@pytest.mark.asyncio
async def test_clear_build_candidate_removes_dir_and_cache(env, fake_provider):
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=2)
    await migrate.run_rebuild(TARGET)                 # populate _build/{slug}
    bdir = stores_dir / migrate.BUILD_DIR_NAME / TARGET
    assert bdir.exists()
    migrate._clear_build_candidate(TARGET)
    assert not bdir.exists()
    fresh = get_store(TARGET, base_dir=stores_dir / migrate.BUILD_DIR_NAME, schema=2)
    assert fresh.missing(["SNAP-0", "SNAP-1"]) == ["SNAP-0", "SNAP-1"]


def test_prune_old_rollbacks_keeps_newest(env):
    _, stores_dir, _ = env
    stores_dir.mkdir(parents=True, exist_ok=True)
    for ts in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
        (stores_dir / f"{TARGET}.pre-rebuild.{ts}").mkdir()
    migrate._prune_old_rollbacks(TARGET, keep=1)
    remaining = sorted(p.name for p in stores_dir.glob(f"{TARGET}.pre-rebuild.*"))
    assert remaining == [f"{TARGET}.pre-rebuild.20260103T000000Z"]


@pytest.mark.asyncio
async def test_catch_up_fill_embeds_only_the_gate_window_mint(env, fake_provider):
    import Orchestrator.embeddings.search as search
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=3)
    store = get_store(TARGET, base_dir=stores_dir, schema=2)   # active v2 store
    store.append_group("SNAP-0", [np.zeros(TARGET_DIMS)])
    store.append_group("SNAP-1", [np.zeros(TARGET_DIMS)])
    assert store.missing(sorted(bodies)) == ["SNAP-2"]
    set_active_slug(TARGET, base_dir=stores_dir)
    search._active_store = store

    filled = await migrate._catch_up_fill(TARGET)

    assert filled == 1                        # ONLY the gate-window mint
    assert store.missing(sorted(bodies)) == []
    assert "SNAP-2" in store.ids()


# ── _activate_candidate: in-service candidate promotion ──────────────────────

@pytest.mark.asyncio
async def test_activate_candidate_non_active_swaps_dir_no_search_repoint(
    env, fake_provider, cutover_spies
):
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=3)
    set_active_slug(OLD_SLUG, base_dir=stores_dir)        # a DIFFERENT active model
    await migrate.run_rebuild(TARGET)                     # candidate under _build
    assert (stores_dir / migrate.BUILD_DIR_NAME / TARGET).exists()

    await migrate._activate_candidate(TARGET)

    live = stores_dir / TARGET
    assert (live / "meta.json").exists()
    assert not (stores_dir / migrate.BUILD_DIR_NAME / TARGET).exists()
    promoted = get_store(TARGET, base_dir=stores_dir)
    assert promoted.schema == 2 and promoted.missing(sorted(bodies)) == []
    assert cutover_spies["swap_active"] == []             # NON-active: no repoint
    assert get_active_slug(base_dir=stores_dir) == OLD_SLUG


@pytest.mark.asyncio
async def test_activate_candidate_active_repoints_search_and_catches_up(
    env, fake_provider, monkeypatch
):
    import Orchestrator.embeddings.search as search
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=3)
    seed = get_store(TARGET, base_dir=stores_dir)         # live v1 store → a rollback is created
    seed.append_many([(sid, np.zeros(TARGET_DIMS)) for sid in bodies])
    set_active_slug(TARGET, base_dir=stores_dir)          # TARGET IS active
    await migrate.run_rebuild(TARGET)
    caught = {"n": 0}
    async def _spy_catch(slug):
        caught["n"] += 1; return 0
    monkeypatch.setattr(migrate, "_catch_up_fill", _spy_catch)

    await migrate._activate_candidate(TARGET)

    assert search._active_store is not None
    assert search._active_store.slug == TARGET
    assert search._active_store.schema == 2              # live handle is the new store
    assert caught["n"] == 1                              # catch-up ran for active model
    assert list(stores_dir.glob(f"{TARGET}.pre-rebuild.*"))   # rollback retained


@pytest.mark.asyncio
async def test_activate_candidate_empty_corpus_is_noop(env, fake_provider, cutover_spies):
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=0)           # 0 snapshots
    set_active_slug(TARGET, base_dir=stores_dir)
    await migrate.run_rebuild(TARGET)                     # builds nothing → no _build/{slug}
    await migrate._activate_candidate(TARGET)             # must NOT raise
    assert cutover_spies["swap_active"] == []             # took the early no-op path
    assert not (stores_dir / TARGET).exists()
    assert not (stores_dir / f"{TARGET}.incoming").exists()
    assert not list(stores_dir.glob(f"{TARGET}.pre-rebuild.*"))


@pytest.mark.asyncio
async def test_activate_candidate_survives_stale_incoming(env, fake_provider):
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=2)
    seed = get_store(TARGET, base_dir=stores_dir)
    seed.append_many([(sid, np.zeros(TARGET_DIMS)) for sid in bodies])
    set_active_slug(TARGET, base_dir=stores_dir)
    await migrate.run_rebuild(TARGET)
    stale = stores_dir / f"{TARGET}.incoming"; stale.mkdir(parents=True, exist_ok=True)
    (stale / "junk").write_text("x")                      # non-empty stale staging
    await migrate._activate_candidate(TARGET)             # must clear .incoming, not ENOTEMPTY
    assert get_store(TARGET, base_dir=stores_dir).schema == 2


@pytest.mark.asyncio
async def test_activate_candidate_retires_racing_mint_handle(env, fake_provider):
    import Orchestrator.embeddings.search as search
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=2)
    seed = get_store(TARGET, base_dir=stores_dir)
    seed.append_many([(sid, np.zeros(TARGET_DIMS)) for sid in bodies])
    set_active_slug(TARGET, base_dir=stores_dir)
    search._active_store = seed
    captured = search.get_active_store()                  # what a mid-flight mint holds
    await migrate.run_rebuild(TARGET)
    await migrate._activate_candidate(TARGET)             # promotes + retires `seed`
    with pytest.raises(RuntimeError, match="retired"):
        captured.append("SNAP-LATE", np.zeros(TARGET_DIMS))
    live = get_store(TARGET, base_dir=stores_dir)
    assert live.schema == 2 and live.missing(sorted(bodies)) == []


@pytest.mark.asyncio
async def test_activate_candidate_retires_before_repoint(env, fake_provider, monkeypatch):
    import Orchestrator.embeddings.search as search
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=2)
    seed = get_store(TARGET, base_dir=stores_dir)
    seed.append_many([(sid, np.zeros(TARGET_DIMS)) for sid in bodies])
    set_active_slug(TARGET, base_dir=stores_dir)
    search._active_store = seed
    captured = search.get_active_store()
    raised = {"v": None}
    real_swap = search.swap_active
    def spy_swap(slug):
        # by the time repoint runs, the old handle must already be retired
        try:
            captured.append("SNAP-LATE", np.zeros(TARGET_DIMS)); raised["v"] = False
        except RuntimeError:
            raised["v"] = True
        return real_swap(slug)
    monkeypatch.setattr(search, "swap_active", spy_swap)
    await migrate.run_rebuild(TARGET)
    await migrate._activate_candidate(TARGET)
    assert raised["v"] is True                                  # closed BEFORE swap_active
    live = get_store(TARGET, base_dir=stores_dir)
    assert live.schema == 2 and live.missing(sorted(bodies)) == []


@pytest.mark.asyncio
async def test_activate_candidate_rolls_forward_orphaned_incoming(env, fake_provider):
    import os as _os
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=2)
    set_active_slug(TARGET, base_dir=stores_dir)
    await migrate.run_rebuild(TARGET)
    cand = stores_dir / migrate.BUILD_DIR_NAME / TARGET
    incoming = stores_dir / f"{TARGET}.incoming"
    _os.replace(cand, incoming)                                 # crash state: store at .incoming, live absent
    assert not (stores_dir / TARGET).exists()
    await migrate._activate_candidate(TARGET)                   # must roll forward, not misread as empty
    live = get_store(TARGET, base_dir=stores_dir)
    assert live.schema == 2 and live.missing(sorted(bodies)) == []
    assert not incoming.exists()


# ── run_reembed / start_reembed: rebuild THEN activate (the wired seam) ───────

@pytest.mark.asyncio
async def test_run_reembed_active_model_full_flow(env, fake_provider):
    import Orchestrator.embeddings.search as search
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=4)
    seed = get_store(TARGET, base_dir=stores_dir)          # v1 active store (the upgrade case)
    seed.append_many([(sid, np.zeros(TARGET_DIMS)) for sid in bodies])
    set_active_slug(TARGET, base_dir=stores_dir)
    search._active_store = get_store(TARGET, base_dir=stores_dir)
    assert search._active_store.schema == 1               # starts stale/whole-doc

    result = await migrate.run_reembed(TARGET)

    assert result["state"] == "done"
    assert result["kind"] == "reembed" and result["activate"] is True
    assert result.get("phase") == "done"                  # terminal phase cleared
    live = get_store(TARGET, base_dir=stores_dir)
    assert live.schema == 2                                # upgraded in place
    assert live.rows > live.snapshots                      # chunk groups
    assert live.missing(sorted(bodies)) == []
    assert search._active_store.schema == 2                # live search on new store
    assert list(stores_dir.glob(f"{TARGET}.pre-rebuild.*"))


@pytest.mark.asyncio
async def test_run_reembed_non_active_upgrades_without_switch(env, fake_provider, cutover_spies):
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=3)
    set_active_slug(OLD_SLUG, base_dir=stores_dir)         # a DIFFERENT active model
    result = await migrate.run_reembed(TARGET)
    assert result["state"] == "done"
    assert cutover_spies["swap_active"] == []              # active model unchanged
    assert get_active_slug(base_dir=stores_dir) == OLD_SLUG
    live = get_store(TARGET, base_dir=stores_dir)
    assert live.schema == 2 and live.missing(sorted(bodies)) == []


@pytest.mark.asyncio
async def test_run_reembed_empty_corpus_is_done_noop(env, fake_provider):
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=0)            # 0 snapshots
    set_active_slug(TARGET, base_dir=stores_dir)
    result = await migrate.run_reembed(TARGET)
    assert result["state"] == "done"                      # NOT stalled
    assert not list(stores_dir.glob(f"{TARGET}.pre-rebuild.*"))


@pytest.mark.asyncio
async def test_run_reembed_unknown_slug_raises(env):
    with pytest.raises(ValueError, match="no-such-model"):
        await migrate.run_reembed("no-such-model")
    assert migrate.get_job_status() is None


# ── boot resume of a re-embed: build-THEN-activate (Task 0.7) ─────────────────

@pytest.mark.asyncio
async def test_boot_resume_of_reembed_activates(env, fake_provider):
    """A restart mid-re-embed resumes into the build engine with activate=True:
    it TOPS UP the in-progress candidate and swaps it live (schema 2), never a
    silent v1 cutover-downgrade and never stuck build-only."""
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=3)
    set_active_slug(TARGET, base_dir=stores_dir)
    stores_dir.mkdir(parents=True, exist_ok=True)
    (stores_dir / migrate.STATE_FILE).write_text(json.dumps({
        "target": TARGET, "state": "running", "kind": "reembed", "activate": True,
        "phase": "building", "done": 0, "total": 3,
        "started_at": "2026-07-01T00:00:00+00:00", "finished_at": None,
        "error": None, "skipped": [], "raced": [],
    }), encoding="utf-8")
    task = migrate.resume_if_interrupted()
    assert task is migrate._JOB_TASK
    await task
    status = migrate.get_job_status()
    assert status["state"] == "done" and status["kind"] == "reembed"
    assert get_store(TARGET, base_dir=stores_dir).schema == 2      # activated, not cutover-downgraded
    assert get_store(TARGET, base_dir=stores_dir).missing(sorted(bodies)) == []


# ── cancel during build is NON-destructive (Task 0.8) ────────────────────────

@pytest.mark.asyncio
async def test_reembed_cancel_during_build_leaves_active_store_untouched(
    env, fake_provider, cutover_spies
):
    """A cancel mid-build returns BEFORE the activate block: the live store is
    never swapped and no rollback backup is created (candidate-only work)."""
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=4)
    seed = get_store(TARGET, base_dir=stores_dir)
    seed.append("SNAP-0", np.zeros(TARGET_DIMS))       # a real v1 active store
    set_active_slug(TARGET, base_dir=stores_dir)
    fake_provider.hook = lambda _texts: migrate.request_cancel()

    result = await migrate.run_reembed(TARGET)

    assert result["state"] == "cancelled"
    assert cutover_spies["swap_active"] == []           # never activated
    assert get_store(TARGET, base_dir=stores_dir).schema == 1   # live store untouched
    assert not list(stores_dir.glob(f"{TARGET}.pre-rebuild.*"))


# ── activation failure → clean terminal state, resumable (Minor fix) ─────────

@pytest.mark.asyncio
async def test_reembed_activation_failure_stalls_not_activating(
    env, fake_provider, monkeypatch
):
    """A raising _activate_candidate must park the job stalled/phase='failed',
    NOT leave the stale phase='activating' the generic except would keep."""
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=3)
    set_active_slug(TARGET, base_dir=stores_dir)

    async def _boom(slug):
        raise RuntimeError("swap exploded")
    monkeypatch.setattr(migrate, "_activate_candidate", _boom)

    result = await migrate.run_reembed(TARGET)

    assert result["state"] == "stalled"
    assert result["phase"] == "failed"                  # NOT "activating"
    assert "activation failed" in result["error"]
