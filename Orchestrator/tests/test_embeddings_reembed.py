"""Re-embed engine + activation seam tests (per-card Re-embed feature)."""
import asyncio
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
