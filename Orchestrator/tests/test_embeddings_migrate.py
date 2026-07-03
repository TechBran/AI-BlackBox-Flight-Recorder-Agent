"""Tests for the migration job — diff-and-fill + atomic cutover (Task 8).

Isolation recipe (same as test_embeddings_routes.py): all filesystem state in
tmp_path (index, stores dir, volume file), fossils' import-time SNAPSHOT_INDEX
binding + mtime cache patched on the fossils module, provider faked via
providers._instances, and migrate's module-level singleton job state reset per
test. The fake volume is a real bytes file with known byte offsets so the
volume-slice read path is exercised for real.
"""
import asyncio
import json
import threading
import time

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator import config, fossils
from Orchestrator.embeddings import migrate, ollama_io, providers, search
from Orchestrator.embeddings.providers import EmbeddingProviderError
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import get_active_slug, get_store
from Orchestrator.routes.embeddings_routes import router

OLD_SLUG = "gemini-embedding-001"          # config default active model
TARGET = "qwen3-embedding-0.6b"            # migration target (1024 dims)
TARGET_DIMS = EMBEDDING_MODELS[TARGET]["dims"]

JOB_KEYS = {
    "target", "state", "done", "total", "started_at", "finished_at",
    "error", "skipped", "raced",
}
# get_job_status() adds the computed cancel flag; the persisted file does not.
STATUS_KEYS = JOB_KEYS | {"cancel_requested"}


# The real Task 11 hook, captured at import time so the hook-specific tests
# can exercise it even though the autouse fixture below stubs the module attr.
REAL_TOOLVAULT_HOOK = migrate._toolvault_cutover_hook


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def toolvault_hook(monkeypatch):
    """Stub the ToolVault cutover hook with a recorder.

    The real hook (Task 11) spawns a daemon thread that re-embeds the REAL
    ToolVault store via the active provider — migration tests must never
    touch it (or the network). The recorded targets double as the
    fired-at-cutover assertion.
    """
    calls = []
    monkeypatch.setattr(
        migrate, "_toolvault_cutover_hook", lambda slug: calls.append(slug)
    )
    return calls


@pytest.fixture(autouse=True)
def health_refresh(monkeypatch):
    """Stub the post-cutover watcher health refresh with an async recorder.

    The real run_health_check probes provider + vendor catalogs (network) and
    rewrites health.json — migration unit tests must never touch it. Recorded
    calls double as the fired-at-cutover assertion. migrate imports watcher
    lazily (watcher.py imports migrate at load), so patch the module attribute
    the lazy import resolves to.
    """
    from Orchestrator.embeddings import watcher
    calls = []

    async def _recorder():
        calls.append(True)
        return {"state": "ok"}

    monkeypatch.setattr(watcher, "run_health_check", _recorder)
    return calls


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated index + stores + volume; migrate/search singletons reset."""
    index_path = tmp_path / "snapshot_index.json"
    stores_dir = tmp_path / "embeddings"
    volume_path = tmp_path / "volume.txt"
    monkeypatch.setattr(fossils, "SNAPSHOT_INDEX", index_path)
    monkeypatch.setattr(fossils, "_index_cache", None)
    monkeypatch.setattr(fossils, "_index_cache_mtime", 0.0)
    monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(stores_dir))
    monkeypatch.setattr(config, "VOL_PATH", volume_path)
    monkeypatch.setattr(migrate, "_JOB", None)
    monkeypatch.setattr(migrate, "_JOB_TASK", None)
    monkeypatch.setattr(migrate, "_CANCEL", threading.Event())
    monkeypatch.setattr(migrate, "BATCH_SLEEP_S", 0.0)
    monkeypatch.setattr(search, "_active_store", None)
    # Hermetic ollama seams (test_embeddings_routes.py recipe): the route
    # tests below GET /embeddings/status, whose _ollama_state() would
    # otherwise probe a real daemon on :11434.
    monkeypatch.setattr(ollama_io, "binary_installed", lambda: False)
    monkeypatch.setattr(ollama_io, "daemon_version", lambda: None)
    monkeypatch.setattr(ollama_io, "local_models", lambda: [])
    monkeypatch.setattr(ollama_io, "ram_preflight", lambda ram_gb: None)
    return index_path, stores_dir, volume_path


@pytest.fixture
def fake_provider(monkeypatch):
    fake = FakeProvider(TARGET_DIMS)
    monkeypatch.setitem(providers._instances, TARGET, fake)
    return fake


class FakeProvider:
    """Deterministic per-text vectors; per-call hook for mid-job injections."""

    def __init__(self, dims):
        self.dims = dims
        self.calls = []          # [(texts, purpose), ...]
        self.hook = None         # sync fn(texts) called BEFORE embedding
        self.fail_substring = None   # text containing this → EmbeddingProviderError
        self.raise_unexpected = None  # exception raised on first call (then cleared)

    @property
    def embedded_texts(self):
        return [t for texts, _ in self.calls for t in texts]

    async def embed(self, texts, purpose):
        if self.hook is not None:
            hook, self.hook = self.hook, None  # one-shot
            hook(texts)
        if self.raise_unexpected is not None:
            exc, self.raise_unexpected = self.raise_unexpected, None
            raise exc
        if self.fail_substring is not None and any(
            self.fail_substring in t for t in texts
        ):
            raise EmbeddingProviderError("synthetic provider failure")
        self.calls.append((list(texts), purpose))
        return [self._vec(t) for t in texts]

    def _vec(self, text):
        rng = np.random.default_rng(sum(text.encode()) % (2**32))
        return [float(x) for x in rng.standard_normal(self.dims)]


def _build_volume(index_path, volume_path, n=10):
    """Concatenated snapshot bodies with correct byte offsets in the index.

    Returns {snap_id: body_text} for asserting embedded text content.
    """
    index, bodies, blob = {}, {}, b""
    for i in range(n):
        sid = f"SNAP-{i}"
        body = f"=== snapshot body {i} — conversation text for {sid} ===\n"
        raw = body.encode("utf-8")
        index[sid] = {
            "byte_start": len(blob), "byte_end": len(blob) + len(raw),
            "operator": "Brandon", "timestamp": "2026-06-11T00:00:00Z",
            "type": "normal",
        }
        blob += raw
        bodies[sid] = body
    volume_path.write_bytes(blob)
    index_path.write_text(json.dumps(index), encoding="utf-8")
    fossils._index_cache = None
    return bodies


def _append_snapshot(index_path, volume_path, sid, body):
    """Mint simulation: append body to the volume + entry to the index."""
    raw = body.encode("utf-8")
    start = volume_path.stat().st_size
    volume_path.write_bytes(volume_path.read_bytes() + raw)
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index[sid] = {
        "byte_start": start, "byte_end": start + len(raw),
        "operator": "Brandon", "timestamp": "2026-06-11T00:00:00Z",
        "type": "normal",
    }
    index_path.write_text(json.dumps(index), encoding="utf-8")
    fossils._index_cache = None
    return body


def _stores_dir(env):
    return env[1]


# ── full migration ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_migration_embeds_all_and_cuts_over(env, fake_provider):
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path)

    result = await migrate.run_migration(TARGET)

    assert result["state"] == "done"
    assert result["done"] == 10 and result["total"] == 10
    assert result["skipped"] == [] and result["raced"] == []
    assert result["error"] is None
    assert result["finished_at"]

    # every text embedded as a document, content == exact volume slices
    assert all(purpose == "document" for _, purpose in fake_provider.calls)
    assert sorted(fake_provider.embedded_texts) == sorted(bodies.values())

    # store filled
    target = get_store(TARGET, base_dir=stores_dir)
    assert target.ids() == set(bodies)

    # cutover: disk pointer AND in-memory search store both flipped
    assert get_active_slug(base_dir=stores_dir) == TARGET
    assert search.get_active_store().slug == TARGET

    # singleton status reflects the finished job
    status = migrate.get_job_status()
    assert set(status.keys()) == STATUS_KEYS
    assert status["state"] == "done" and status["done"] == 10
    assert status["cancel_requested"] is False


@pytest.mark.asyncio
async def test_toolvault_hook_fires_at_cutover_with_target(env, fake_provider, toolvault_hook):
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=2)

    result = await migrate.run_migration(TARGET)

    assert result["state"] == "done"
    assert toolvault_hook == [TARGET]


@pytest.mark.asyncio
async def test_health_refresh_fires_at_cutover(env, fake_provider, health_refresh):
    """A successful cutover recomputes health for the NEW active model, so the
    superseded banner clears immediately instead of pointing at the model the
    operator just switched TO (until the next daily watcher run / a restart)."""
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=2)

    result = await migrate.run_migration(TARGET)

    assert result["state"] == "done"
    assert health_refresh == [True]  # run_health_check awaited once at cutover


@pytest.mark.asyncio
async def test_toolvault_hook_raise_does_not_fail_migration(
    env, fake_provider, monkeypatch, capsys
):
    """A ToolVault hiccup inside the hook must never fail the cutover."""
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=2)

    def boom(slug):
        raise RuntimeError("toolvault on fire")

    monkeypatch.setattr(migrate, "_toolvault_cutover_hook", boom)

    result = await migrate.run_migration(TARGET)

    assert result["state"] == "done"            # migration still completes
    assert result["error"] is None
    assert get_active_slug(base_dir=stores_dir) == TARGET  # cutover landed
    assert "toolvault cutover hook raised (non-fatal)" in capsys.readouterr().out


def test_real_toolvault_hook_calls_sync_and_is_idempotent(monkeypatch):
    """The real hook re-embeds via toolvault's sync_embeddings (patched at
    the lazy-import site) on a joinable daemon thread; a re-fire (resume
    after a cutover crash) just syncs again — wasteful, never harmful."""
    import Orchestrator.toolvault.embeddings as tv_embeddings
    import Orchestrator.toolvault.registry as tv_registry

    canonical = [{"name": "t", "description": "d"}]
    synced = []
    monkeypatch.setattr(tv_registry, "load_canonical", lambda: canonical)
    monkeypatch.setattr(
        tv_embeddings, "sync_embeddings",
        lambda canon, path=None, **kw: synced.append(canon) or {},
    )

    thread = REAL_TOOLVAULT_HOOK(TARGET)
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert synced == [canonical]

    thread2 = REAL_TOOLVAULT_HOOK(TARGET)   # idempotent re-fire
    thread2.join(timeout=5)
    assert synced == [canonical, canonical]


def test_real_toolvault_hook_contains_toolvault_exception(monkeypatch, capsys):
    """A raise inside ToolVault never propagates out of the hook thread."""
    import Orchestrator.toolvault.registry as tv_registry

    def explode():
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(tv_registry, "load_canonical", explode)

    thread = REAL_TOOLVAULT_HOOK(TARGET)
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert "toolvault re-embed failed (non-fatal)" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_unknown_slug_raises_value_error(env):
    with pytest.raises(ValueError, match="no-such-model"):
        await migrate.run_migration("no-such-model")
    assert migrate.get_job_status() is None  # bad slug never claims the job


# ── switch-back delta ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_switch_back_only_embeds_the_delta(env, fake_provider):
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path)
    pre_ids = [f"SNAP-{i}" for i in range(6)]
    target = get_store(TARGET, base_dir=stores_dir)
    rng = np.random.default_rng(7)
    target.append_many([(sid, rng.standard_normal(TARGET_DIMS)) for sid in pre_ids])

    result = await migrate.run_migration(TARGET)

    assert result["state"] == "done"
    assert result["done"] == 4 and result["total"] == 4
    embedded = fake_provider.embedded_texts
    assert sorted(embedded) == sorted(bodies[f"SNAP-{i}"] for i in range(6, 10))
    assert target.ids() == set(bodies)


# ── catch-up convergence ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_catch_up_picks_up_mid_job_mint(env, fake_provider):
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path)
    old_store = get_store(OLD_SLUG, base_dir=stores_dir)
    new_body = {}

    def mint_during_pass_one(_texts):
        # A mint lands during pass 1: index entry + volume bytes + a vector in
        # the ACTIVE (old) store — exactly what checkpoint.py does live.
        body = _append_snapshot(
            index_path, volume_path, "SNAP-NEW", "=== freshly minted mid-job ===\n"
        )
        new_body["SNAP-NEW"] = body
        old_store.append("SNAP-NEW", np.random.default_rng(1).standard_normal(3072))

    fake_provider.hook = mint_during_pass_one

    result = await migrate.run_migration(TARGET)

    assert result["state"] == "done"
    assert result["done"] == 11  # 10 originals + the mid-job mint
    target = get_store(TARGET, base_dir=stores_dir)
    assert "SNAP-NEW" in target.ids()
    assert new_body["SNAP-NEW"] in fake_provider.embedded_texts
    assert get_active_slug(base_dir=stores_dir) == TARGET


# ── cancel + resume ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_keeps_partial_store_and_restart_completes(env, fake_provider, toolvault_hook):
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path)
    fake_provider.hook = lambda _texts: migrate.request_cancel()

    result = await migrate.run_migration(TARGET)

    # batch 1 (8 ids) embedded before the flag is seen at the next batch gate
    assert result["state"] == "cancelled"
    assert result["finished_at"]
    target = get_store(TARGET, base_dir=stores_dir)
    assert target.count == 8  # partial progress kept
    # NO cutover on cancel — and the toolvault re-embed never fires
    assert get_active_slug(base_dir=stores_dir) == OLD_SLUG
    assert toolvault_hook == []

    # re-start resumes via the diff and completes
    result2 = await migrate.run_migration(TARGET)
    assert result2["state"] == "done"
    assert toolvault_hook == [TARGET]
    assert result2["done"] == 2  # only the remaining delta
    assert target.ids() == set(bodies)
    assert get_active_slug(base_dir=stores_dir) == TARGET


# ── quarantine (permanently-failing text must not stall the job) ─────────────

@pytest.mark.asyncio
async def test_failing_batch_is_quarantined_job_still_completes(
    env, fake_provider, monkeypatch, capsys
):
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path)
    monkeypatch.setattr(migrate, "BATCH_SIZE", 1)  # isolate the poison text
    fake_provider.fail_substring = "snapshot body 3 "

    result = await migrate.run_migration(TARGET)

    assert result["state"] == "done"           # completes, never spins
    assert result["skipped"] == ["SNAP-3"]
    target = get_store(TARGET, base_dir=stores_dir)
    assert target.ids() == set(bodies) - {"SNAP-3"}
    # quarantined ids stay missing() so a later run/watcher retries them
    assert target.missing(list(bodies)) == ["SNAP-3"]
    assert get_active_slug(base_dir=stores_dir) == TARGET
    assert "quarantining" in capsys.readouterr().out


# ── all-quarantined cutover guard (dead provider must not cut over) ──────────

@pytest.mark.asyncio
async def test_all_batches_quarantined_aborts_cutover(env, fake_provider, capsys, toolvault_hook):
    """Dead provider (revoked key, daemon down): every batch fails, nothing is
    appended — the job must STALL, never cut over to a near-empty store."""
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path)
    fake_provider.fail_substring = "snapshot body"  # matches every text

    result = await migrate.run_migration(TARGET)

    assert result["state"] == "stalled"
    assert toolvault_hook == []  # no cutover → no toolvault re-embed
    assert "cutover aborted" in result["error"]
    assert "active store unchanged" in result["error"]
    assert f"all {len(bodies)} snapshots" in result["error"]
    assert sorted(result["skipped"]) == sorted(bodies)
    # NO cutover: disk pointer untouched, in-memory search store never swapped
    assert get_active_slug(base_dir=stores_dir) == OLD_SLUG
    assert search._active_store is None
    assert get_store(TARGET, base_dir=stores_dir).count == 0
    assert "[MIGRATE] ERROR" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_switch_back_with_nothing_missing_still_cuts_over(env, fake_provider):
    """Fast path: everything already present (no appends AND no skips) is a
    completed switch-back, not a failure — the guard must not fire."""
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path)
    target = get_store(TARGET, base_dir=stores_dir)
    rng = np.random.default_rng(11)
    target.append_many(
        [(sid, rng.standard_normal(TARGET_DIMS)) for sid in bodies]
    )

    result = await migrate.run_migration(TARGET)

    assert result["state"] == "done"
    assert result["done"] == 0 and result["skipped"] == []
    assert fake_provider.calls == []  # nothing embedded
    assert get_active_slug(base_dir=stores_dir) == TARGET


# ── raced-mint detection (Task 6 advisory) ───────────────────────────────────

@pytest.mark.asyncio
async def test_foreign_append_is_detected_as_raced(env, fake_provider, capsys):
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path)

    def foreign_append(_texts):
        # Simulates a mint embedded under the OLD model landing in the TARGET
        # store (dims aside, the mechanism is: not preexisting, in the index,
        # NOT written by the job). The handle is resolved AT APPEND TIME, as a
        # real foreign writer's would be: get_store hands back the engine's
        # canonical instance (post-flip a fresh target is schema 2 — a plain
        # probe BEFORE the engine would cache a conflicting v1 instance the
        # engine then refuses, which is why open_migration_target exists).
        target = get_store(TARGET, base_dir=stores_dir)
        target.append("SNAP-9", np.random.default_rng(2).standard_normal(TARGET_DIMS))

    fake_provider.hook = foreign_append

    result = await migrate.run_migration(TARGET)

    assert result["state"] == "done"
    assert result["raced"] == ["SNAP-9"]
    out = capsys.readouterr().out
    assert "WARNING" in out and "raced the cutover" in out and "SNAP-9" in out


# ── stalled on unexpected error, re-run completes ────────────────────────────

@pytest.mark.asyncio
async def test_unexpected_error_stalls_then_rerun_completes(env, fake_provider):
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path)
    fake_provider.raise_unexpected = RuntimeError("disk on fire")

    result = await migrate.run_migration(TARGET)

    assert result["state"] == "stalled"
    assert result["error"] == "disk on fire"
    assert result["finished_at"]
    assert get_active_slug(base_dir=stores_dir) == OLD_SLUG  # no cutover

    result2 = await migrate.run_migration(TARGET)
    assert result2["state"] == "done"
    assert get_store(TARGET, base_dir=stores_dir).ids() == set(bodies)


# ── persistence + startup resume ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_state_file_written_during_and_after_run(env, fake_provider):
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path)
    state_path = stores_dir / migrate.STATE_FILE
    seen_running = {}

    def capture_mid_run(_texts):
        seen_running.update(json.loads(state_path.read_text(encoding="utf-8")))

    fake_provider.hook = capture_mid_run

    await migrate.run_migration(TARGET)

    # mid-run: persisted as a live, resumable job
    assert seen_running["state"] == "running"
    assert seen_running["target"] == TARGET
    # post-run: final transition persisted with full field set
    final = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(final.keys()) == JOB_KEYS
    assert final["state"] == "done"
    assert final["done"] == 10 and final["total"] == 10
    assert final["started_at"] and final["finished_at"]


@pytest.mark.asyncio
async def test_resume_if_interrupted_relaunches_running_state(env, fake_provider):
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path)
    stores_dir.mkdir(parents=True, exist_ok=True)
    (stores_dir / migrate.STATE_FILE).write_text(json.dumps({
        "target": TARGET, "state": "running", "done": 3, "total": 10,
        "started_at": "2026-06-11T00:00:00+00:00", "finished_at": None,
        "error": None, "skipped": [], "raced": [],
    }), encoding="utf-8")

    task = migrate.resume_if_interrupted()

    assert isinstance(task, asyncio.Task)
    assert task is migrate._JOB_TASK  # resume routes through _launch too
    await task
    assert migrate.get_job_status()["state"] == "done"
    assert get_store(TARGET, base_dir=stores_dir).ids() == set(bodies)


@pytest.mark.asyncio
async def test_resume_noop_when_not_interrupted(env):
    index_path, stores_dir, volume_path = env
    # no state file at all
    assert migrate.resume_if_interrupted() is None
    # a finished job is not relaunched
    stores_dir.mkdir(parents=True, exist_ok=True)
    (stores_dir / migrate.STATE_FILE).write_text(
        json.dumps({"target": TARGET, "state": "done"}), encoding="utf-8"
    )
    assert migrate.resume_if_interrupted() is None
    # unknown target is refused, not resumed
    (stores_dir / migrate.STATE_FILE).write_text(
        json.dumps({"target": "gone-model", "state": "running"}), encoding="utf-8"
    )
    assert migrate.resume_if_interrupted() is None


# ── task reference retention (loop holds only weak refs) ────────────────────

@pytest.mark.asyncio
async def test_start_migration_retains_engine_task(env, monkeypatch, capsys):
    """start_migration must keep a strong module-level ref to the engine Task
    (the loop's refs are weak — a GC'd task dies silently, _JOB stays
    "running" and every later POST 409s until restart)."""
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=3)
    gated = GatedProvider(TARGET_DIMS)
    monkeypatch.setitem(providers._instances, TARGET, gated)

    job = await migrate.start_migration(TARGET)

    assert job["state"] == "running"
    assert migrate._JOB_TASK is not None and not migrate._JOB_TASK.done()

    gated.release.set()
    result = await migrate._JOB_TASK
    assert result["state"] == "done"
    await asyncio.sleep(0)  # let the done-callback run
    assert migrate._JOB_TASK.exception() is None
    assert "[MIGRATE] ERROR" not in capsys.readouterr().out


# ── routes ───────────────────────────────────────────────────────────────────

class GatedProvider(FakeProvider):
    """Holds every embed call until .release is set (thread-safe poll)."""

    def __init__(self, dims):
        super().__init__(dims)
        self.release = threading.Event()

    async def embed(self, texts, purpose):
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        return await super().embed(texts, purpose)


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(router)
    return app


def _wait_for_state(state, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = migrate.get_job_status()
        if status is not None and status["state"] == state:
            return status
        time.sleep(0.02)
    raise AssertionError(f"job never reached state {state!r}: {migrate.get_job_status()}")


def test_migrate_unknown_slug_404(env, app):
    with TestClient(app) as client:
        resp = client.post("/embeddings/migrate", json={"target": "no-such-model"})
    assert resp.status_code == 404


def test_cancel_when_idle_is_false(env, app):
    with TestClient(app) as client:
        resp = client.post("/embeddings/migrate/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"cancelled": False}


def test_migrate_409_status_job_and_cancel_flow(env, app, monkeypatch):
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path)
    gated = GatedProvider(TARGET_DIMS)
    monkeypatch.setitem(providers._instances, TARGET, gated)

    # context manager = ONE persistent portal loop, so the background task
    # created by the POST survives across requests
    with TestClient(app) as client:
        resp = client.post("/embeddings/migrate", json={"target": TARGET})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == STATUS_KEYS
        assert body["state"] == "running" and body["target"] == TARGET
        assert body["cancel_requested"] is False

        # one job at a time
        resp2 = client.post("/embeddings/migrate", json={"target": TARGET})
        assert resp2.status_code == 409
        assert "already running" in resp2.json()["detail"]

        # status carries the live job
        job = client.get("/embeddings/status").json()["job"]
        assert job is not None and job["state"] == "running"
        assert job["target"] == TARGET

        # cancel endpoint
        resp3 = client.post("/embeddings/migrate/cancel")
        assert resp3.json() == {"cancelled": True}
        # wizard signal: "cancelling — finishing current batch"
        job = client.get("/embeddings/status").json()["job"]
        assert job["cancel_requested"] is True
        gated.release.set()  # unblock the in-flight batch
        _wait_for_state("cancelled")

    # after cancel: no cutover, job visible as cancelled in status state
    assert get_active_slug(base_dir=stores_dir) == OLD_SLUG
