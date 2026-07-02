"""Chunk-aware rebuild mode + gap-heal chunk batching + CLI liveness guard
(M6 task 6d, audit amendments A5/A6).

Isolation recipe is test_embeddings_migrate.py's: all filesystem state in
tmp_path (index, stores dir, volume file), fossils' import-time binding +
mtime cache patched on the fossils module, provider faked via
providers._instances, migrate's module-level singleton job state reset per
test. The fake volume is a real bytes file with known byte offsets so the
volume-slice read path is exercised for real.

The rebuild path is BUILD-ONLY by design: it must never call
set_active_slug or search.swap_active (activation is the explicit M6f
dir-swap). Both are spied here and asserted silent on every rebuild test.
"""
import asyncio
import json
import threading

import numpy as np
import pytest

from Orchestrator import backfill_embeddings as backfill
from Orchestrator import config, fossils
from Orchestrator.embeddings import migrate, ollama_io, providers, search, watcher
from Orchestrator.embeddings.providers import EmbeddingProviderError
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import get_active_slug, get_store, set_active_slug

OLD_SLUG = "gemini-embedding-001"          # config default active model
TARGET = "qwen3-embedding-0.6b"            # rebuild target (1024 dims)
TARGET_DIMS = EMBEDDING_MODELS[TARGET]["dims"]

BASE_JOB_KEYS = {
    "target", "state", "done", "total", "started_at", "finished_at",
    "error", "skipped", "raced",
}
REBUILD_JOB_KEYS = BASE_JOB_KEYS | {"kind", "activate"}


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def toolvault_hook(monkeypatch):
    """Recorder stub for the cutover-only ToolVault hook: rebuild never cuts
    over, so this must stay EMPTY on every rebuild test."""
    calls = []
    monkeypatch.setattr(
        migrate, "_toolvault_cutover_hook", lambda slug: calls.append(slug)
    )
    return calls


@pytest.fixture(autouse=True)
def health_refresh(monkeypatch):
    """Stub the post-cutover watcher health refresh (network) — only the
    plain-migration resume test reaches it; rebuilds never cut over."""
    calls = []

    async def _recorder():
        calls.append(True)
        return {"state": "ok"}

    monkeypatch.setattr(watcher, "run_health_check", _recorder)
    return calls


@pytest.fixture
def cutover_spies(monkeypatch):
    """Spy set_active_slug + search.swap_active at migrate's call sites.

    The 6d prohibition made executable: a rebuild that touches either has
    performed a cutover and must fail these tests.
    """
    calls = {"set_active_slug": [], "swap_active": []}
    monkeypatch.setattr(
        migrate, "set_active_slug",
        lambda slug, base_dir=None: calls["set_active_slug"].append(slug),
    )
    monkeypatch.setattr(
        search, "swap_active", lambda slug: calls["swap_active"].append(slug)
    )
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
    monkeypatch.setattr(ollama_io, "binary_installed", lambda: False)
    monkeypatch.setattr(ollama_io, "daemon_version", lambda: None)
    monkeypatch.setattr(ollama_io, "local_models", lambda: [])
    monkeypatch.setattr(ollama_io, "ram_preflight", lambda ram_gb: None)
    return index_path, stores_dir, volume_path


class FakeProvider:
    """Deterministic per-text vectors; per-call hook for mid-job injections."""

    def __init__(self, dims):
        self.dims = dims
        self.calls = []          # [(texts, purpose), ...]
        self.hook = None         # one-shot sync fn(texts) called BEFORE embedding
        self.fail_substring = None   # text containing this → EmbeddingProviderError
        self.fail_all = False

    @property
    def embedded_texts(self):
        return [t for texts, _ in self.calls for t in texts]

    @property
    def call_sizes(self):
        return [len(texts) for texts, _ in self.calls]

    async def embed(self, texts, purpose):
        if self.hook is not None:
            hook, self.hook = self.hook, None
            hook(texts)
        if self.fail_all:
            raise EmbeddingProviderError("synthetic dead provider")
        if self.fail_substring is not None and any(
            self.fail_substring in t for t in texts
        ):
            raise EmbeddingProviderError("synthetic provider failure")
        self.calls.append((list(texts), purpose))
        return [self._vec(t) for t in texts]

    def _vec(self, text):
        rng = np.random.default_rng(sum(text.encode()) % (2**32))
        return [float(x) for x in rng.standard_normal(self.dims)]


@pytest.fixture
def fake_provider(monkeypatch):
    fake = FakeProvider(TARGET_DIMS)
    monkeypatch.setitem(providers._instances, TARGET, fake)
    return fake


def _long_body(i: int, n_lines: int = 400) -> str:
    """Guaranteed multi-chunk under every tokenizer backend (test_chunker.py's
    recipe): ~17k chars >> the 1024-token window on exact AND floor backends."""
    return "\n".join(
        f"snap{i:02d} line {j:05d} " + "abcdefghij" * 3 for j in range(n_lines)
    )


def _build_volume(index_path, volume_path, n=5, body_fn=_long_body):
    """Concatenated snapshot bodies with correct byte offsets in the index."""
    index, bodies, blob = {}, {}, b""
    for i in range(n):
        sid = f"SNAP-{i}"
        body = body_fn(i)
        raw = body.encode("utf-8")
        index[sid] = {
            "byte_start": len(blob), "byte_end": len(blob) + len(raw),
            "operator": "Brandon", "timestamp": "2026-07-01T00:00:00Z",
            "type": "normal",
        }
        blob += raw
        bodies[sid] = body
    volume_path.write_bytes(blob)
    index_path.write_text(json.dumps(index), encoding="utf-8")
    fossils._index_cache = None
    return bodies


def _fixed_chunker(counts: dict):
    """chunk_snapshot stand-in with exact per-snapshot chunk counts, keyed by
    the 'snapNN' body prefix (deterministic packing math in the cap tests)."""
    def chunk(text, model_key=None):
        sid_tag = text[:6]  # "snapNN"
        n = counts[sid_tag]
        return [f"{sid_tag}::chunk{j:02d}" for j in range(n)]
    return chunk


def _read_group_layout(store_dir):
    """(ids, ordinals) straight from disk for contiguity assertions."""
    ids = json.loads((store_dir / "ids.json").read_text(encoding="utf-8"))
    ordinals = json.loads((store_dir / "ordinals.json").read_text(encoding="utf-8"))
    return ids, ordinals


def _assert_contiguous_groups(ids, ordinals):
    """Every snapshot's chunks form ONE contiguous run with ordinals 0..n-1."""
    assert len(ids) == len(ordinals)
    seen_groups = set()
    i = 0
    while i < len(ids):
        sid = ids[i]
        assert sid not in seen_groups, f"{sid}: second (non-contiguous) group"
        seen_groups.add(sid)
        j = i
        while j < len(ids) and ids[j] == sid:
            assert ordinals[j] == j - i, f"{sid}: ordinal gap at row {j}"
            j += 1
        i = j
    return seen_groups


def _build_store_dir(stores_dir):
    return stores_dir / migrate.BUILD_DIR_NAME / TARGET


# ── pack_chunk_batches (pure unit) ───────────────────────────────────────────

def test_pack_respects_cap_with_whole_snapshots():
    chunked = [(f"S{i}", [f"c{i}-{j}" for j in range(10)]) for i in range(5)]
    batches = migrate.pack_chunk_batches(chunked, cap=32)
    # 10+10+10 = 30 fits; adding the 4th (→40) would exceed 32
    assert [[sid for sid, _ in b] for b in batches] == [
        ["S0", "S1", "S2"], ["S3", "S4"],
    ]
    for batch in batches:
        assert sum(len(c) for _, c in batch) <= 32


def test_pack_exact_cap_boundary_fits():
    chunked = [("A", ["a"] * 10), ("B", ["b"] * 22), ("C", ["c"] * 1)]
    batches = migrate.pack_chunk_batches(chunked, cap=32)
    # 10+22 == 32 exactly fits; C starts the next batch
    assert [[sid for sid, _ in b] for b in batches] == [["A", "B"], ["C"]]


def test_pack_oversized_snapshot_goes_alone_in_one_call():
    # Whole-snapshot atomicity beats the cap: a 40-chunk snapshot is ONE
    # provider call by itself (its group must come from one aligned call).
    chunked = [("A", ["a"] * 3), ("BIG", ["b"] * 40), ("C", ["c"] * 3)]
    batches = migrate.pack_chunk_batches(chunked, cap=32)
    assert [[sid for sid, _ in b] for b in batches] == [["A"], ["BIG"], ["C"]]
    assert sum(len(c) for _, c in batches[1]) == 40


def test_pack_preserves_order_and_default_cap():
    chunked = [(f"S{i}", ["x"] * 1) for i in range(70)]
    batches = migrate.pack_chunk_batches(chunked)  # default CHUNK_BATCH_CAP=32
    assert [len(b) for b in batches] == [32, 32, 6]
    flat = [sid for b in batches for sid, _ in b]
    assert flat == [f"S{i}" for i in range(70)]


# ── rebuild: convergence on a multi-chunk corpus ─────────────────────────────

@pytest.mark.asyncio
async def test_rebuild_converges_multichunk_corpus(env, fake_provider):
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=5)

    result = await migrate.run_rebuild(TARGET)

    assert result["state"] == "done"
    assert result["kind"] == "rebuild"
    assert result["activate"] is False
    assert result["done"] == 5 and result["total"] == 5
    assert result["skipped"] == [] and result["error"] is None

    # the candidate landed under {stores}/_build/{slug}, schema 2
    bstore = get_store(TARGET, base_dir=stores_dir / migrate.BUILD_DIR_NAME,
                       schema=2)
    assert bstore.schema == 2
    assert bstore.snapshots == 5
    assert bstore.rows > bstore.snapshots          # multi-chunk texts
    # snapshot-currency convergence: nothing missing at the end
    assert bstore.missing(sorted(bodies)) == []
    # completion counts recorded in the job state
    assert result["rows"] == bstore.rows
    assert result["snapshots"] == 5

    # groups contiguous, ordinals 0..n-1, document order
    ids, ordinals = _read_group_layout(_build_store_dir(stores_dir))
    assert _assert_contiguous_groups(ids, ordinals) == set(bodies)

    # chunks embedded as documents, every chunk a verbatim slice of its body
    assert all(purpose == "document" for _, purpose in fake_provider.calls)
    for text in fake_provider.embedded_texts:
        assert any(text in body for body in bodies.values())
    # every provider call obeyed the flatten cap (no ~10-chunk snapshot here
    # exceeds it alone; the oversize case has its own test below)
    for texts, _ in fake_provider.calls:
        assert len(texts) <= migrate.CHUNK_BATCH_CAP
    assert len(fake_provider.calls) > 1              # cap forced sub-batching


@pytest.mark.asyncio
async def test_rebuild_build_dir_invisible_to_list_stores(env, fake_provider):
    from Orchestrator.embeddings.store import list_stores
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=2)

    await migrate.run_rebuild(TARGET)

    # _build has no meta.json at its root and list_stores does not recurse,
    # so status/list surfaces never see the candidate store.
    assert [s["slug"] for s in list_stores(stores_dir)] == []
    assert (_build_store_dir(stores_dir) / "meta.json").exists()


# ── rebuild: build-only (the A5 prohibition, executable) ────────────────────

@pytest.mark.asyncio
async def test_rebuild_never_activates(env, fake_provider, cutover_spies,
                                       toolvault_hook):
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=3)
    # a real active pointer exists — must come through byte-identical
    set_active_slug(OLD_SLUG, base_dir=stores_dir)
    active_path = stores_dir / "active.json"
    before_bytes = active_path.read_bytes()
    before_mtime = active_path.stat().st_mtime_ns

    result = await migrate.run_rebuild(TARGET)

    assert result["state"] == "done"
    assert cutover_spies["set_active_slug"] == []
    assert cutover_spies["swap_active"] == []
    assert toolvault_hook == []                      # cutover-only hook silent
    assert active_path.read_bytes() == before_bytes
    assert active_path.stat().st_mtime_ns == before_mtime
    assert get_active_slug(base_dir=stores_dir) == OLD_SLUG
    assert search._active_store is None              # in-memory handle untouched


# ── rebuild: interruption + resume, no duplicate groups ─────────────────────

@pytest.mark.asyncio
async def test_interrupted_rebuild_resumes_without_duplicates(
    env, fake_provider, monkeypatch
):
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=5)
    # 5 snapshots x 8 chunks: batch 1 = 4 snaps (32 chunks), batch 2 = 1 snap
    counts = {f"snap{i:02d}": 8 for i in range(5)}
    monkeypatch.setattr(migrate, "chunk_snapshot", _fixed_chunker(counts))
    fake_provider.hook = lambda _texts: migrate.request_cancel()

    result = await migrate.run_rebuild(TARGET)

    assert result["state"] == "cancelled"
    bstore = get_store(TARGET, base_dir=stores_dir / migrate.BUILD_DIR_NAME,
                       schema=2)
    assert bstore.snapshots == 4 and bstore.rows == 32   # batch 1 kept
    # persisted state still carries the rebuild kind (resume metadata)
    persisted = json.loads(
        (stores_dir / migrate.STATE_FILE).read_text(encoding="utf-8")
    )
    assert persisted["kind"] == "rebuild" and persisted["activate"] is False
    assert persisted["state"] == "cancelled"

    # re-run converges on the delta only — zero duplicate groups
    result2 = await migrate.run_rebuild(TARGET)
    assert result2["state"] == "done"
    assert result2["done"] == 1                      # only the remaining snapshot
    assert bstore.snapshots == 5 and bstore.rows == 40   # exact, no dups
    assert fake_provider.call_sizes == [32, 8]
    ids, ordinals = _read_group_layout(_build_store_dir(stores_dir))
    _assert_contiguous_groups(ids, ordinals)

    # group-skip idempotency at the store seam: a re-append writes nothing
    rng = np.random.default_rng(3)
    assert bstore.append_group(
        "SNAP-0", [rng.standard_normal(TARGET_DIMS) for _ in range(8)]
    ) == 0
    assert bstore.rows == 40


# ── rebuild: boot resume stays build-only ────────────────────────────────────

@pytest.mark.asyncio
async def test_boot_resume_of_rebuild_stays_build_only(
    env, fake_provider, cutover_spies, toolvault_hook
):
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=3)
    stores_dir.mkdir(parents=True, exist_ok=True)
    (stores_dir / migrate.STATE_FILE).write_text(json.dumps({
        "target": TARGET, "state": "running", "kind": "rebuild",
        "activate": False, "done": 1, "total": 3,
        "started_at": "2026-07-01T00:00:00+00:00", "finished_at": None,
        "error": None, "skipped": [], "raced": [],
    }), encoding="utf-8")

    task = migrate.resume_if_interrupted()

    assert isinstance(task, asyncio.Task)
    assert task is migrate._JOB_TASK          # resume routes through _launch
    await task
    status = migrate.get_job_status()
    assert status["state"] == "done"
    assert status["kind"] == "rebuild" and status["activate"] is False
    bstore = get_store(TARGET, base_dir=stores_dir / migrate.BUILD_DIR_NAME,
                       schema=2)
    assert bstore.ids() == set(bodies)
    # build-only held through the resume path
    assert cutover_spies["set_active_slug"] == []
    assert cutover_spies["swap_active"] == []
    assert toolvault_hook == []
    assert not (stores_dir / "active.json").exists()


@pytest.mark.asyncio
async def test_resume_of_plain_migration_still_cuts_over(env, fake_provider,
                                                         cutover_spies):
    """The ADDITIVE guarantee: a kind-less persisted job (the pre-6d shape and
    every model-switch job) still resumes into the cutover engine."""
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=2)
    stores_dir.mkdir(parents=True, exist_ok=True)
    (stores_dir / migrate.STATE_FILE).write_text(json.dumps({
        "target": TARGET, "state": "running", "done": 0, "total": 2,
        "started_at": "2026-07-01T00:00:00+00:00", "finished_at": None,
        "error": None, "skipped": [], "raced": [],
    }), encoding="utf-8")

    task = migrate.resume_if_interrupted()
    await task

    assert migrate.get_job_status()["state"] == "done"
    assert cutover_spies["set_active_slug"] == [TARGET]
    assert cutover_spies["swap_active"] == [TARGET]


# ── rebuild: chunk-cap batching through the engine ──────────────────────────

@pytest.mark.asyncio
async def test_oversized_snapshot_is_one_provider_call(env, fake_provider,
                                                       monkeypatch):
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=1)
    monkeypatch.setattr(
        migrate, "chunk_snapshot", _fixed_chunker({"snap00": 40})
    )

    result = await migrate.run_rebuild(TARGET)

    assert result["state"] == "done"
    assert fake_provider.call_sizes == [40]          # alone, ONE call, over cap
    bstore = get_store(TARGET, base_dir=stores_dir / migrate.BUILD_DIR_NAME,
                       schema=2)
    assert bstore.rows == 40 and bstore.snapshots == 1


@pytest.mark.asyncio
async def test_mixed_batch_packs_whole_snapshots_under_cap(
    env, fake_provider, monkeypatch
):
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=5)
    counts = {f"snap{i:02d}": 10 for i in range(4)}
    counts["snap04"] = 2
    monkeypatch.setattr(migrate, "chunk_snapshot", _fixed_chunker(counts))

    result = await migrate.run_rebuild(TARGET)

    assert result["state"] == "done"
    # 10+10+10=30 fits; the 4th snapshot would hit 40 → second call gets 10+2
    assert fake_provider.call_sizes == [30, 12]
    bstore = get_store(TARGET, base_dir=stores_dir / migrate.BUILD_DIR_NAME,
                       schema=2)
    assert bstore.rows == 42 and bstore.snapshots == 5


# ── rebuild: quarantine + stall guard (engine failure semantics mirrored) ────

@pytest.mark.asyncio
async def test_rebuild_quarantines_failing_batch_and_completes(
    env, fake_provider, monkeypatch, capsys
):
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=5)
    counts = {f"snap{i:02d}": 2 for i in range(5)}
    monkeypatch.setattr(migrate, "chunk_snapshot", _fixed_chunker(counts))
    monkeypatch.setattr(migrate, "CHUNK_BATCH_CAP", 2)   # one snapshot per call
    fake_provider.fail_substring = "snap03"

    result = await migrate.run_rebuild(TARGET)

    assert result["state"] == "done"                 # completes, never spins
    assert result["skipped"] == ["SNAP-3"]
    bstore = get_store(TARGET, base_dir=stores_dir / migrate.BUILD_DIR_NAME,
                       schema=2)
    assert bstore.ids() == set(bodies) - {"SNAP-3"}
    # quarantined ids stay missing() so a later run retries them
    assert bstore.missing(sorted(bodies)) == ["SNAP-3"]
    assert "quarantining" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_rebuild_dead_provider_stalls_not_done(env, fake_provider,
                                                     capsys):
    """Every batch failing (revoked key, daemon down) is failure, not a
    completed candidate — mirror of the migrate all-quarantined guard."""
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=3)
    fake_provider.fail_all = True

    result = await migrate.run_rebuild(TARGET)

    assert result["state"] == "stalled"
    assert "no progress" in result["error"]
    assert sorted(result["skipped"]) == ["SNAP-0", "SNAP-1", "SNAP-2"]
    assert "[MIGRATE] ERROR" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_rebuild_unknown_slug_raises(env):
    with pytest.raises(ValueError, match="no-such-model"):
        await migrate.run_rebuild("no-such-model")
    assert migrate.get_job_status() is None


@pytest.mark.asyncio
async def test_rebuild_claims_the_job_singleton(env, fake_provider):
    """One job at a time across BOTH kinds: a rebuild claim blocks a second."""
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=2)
    migrate._begin_job(TARGET, kind="rebuild")
    with pytest.raises(RuntimeError, match="already running"):
        await migrate.run_rebuild(TARGET)
    with pytest.raises(RuntimeError, match="already running"):
        await migrate.run_migration(TARGET)
    migrate._finish_job("cancelled")


@pytest.mark.asyncio
async def test_rebuild_progress_log_lines(env, fake_provider, capsys):
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=2)

    await migrate.run_rebuild(TARGET)

    out = capsys.readouterr().out
    assert f"[MIGRATE] rebuild {TARGET}:" in out
    assert "snapshots (" in out and "rows)" in out


# ── gap-heal: v2 active store heals in chunk groups ──────────────────────────

@pytest.mark.asyncio
async def test_gap_heal_v2_chunks_groups_and_sub_batches(env, fake_provider,
                                                         monkeypatch):
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=5)
    # v2 ACTIVE store (the M6f post-cutover state), 5 gaps, 16 chunks each:
    # 80 chunks → the 32-cap forces 3 provider calls (32, 32, 16)
    store = get_store(TARGET, base_dir=stores_dir, schema=2)
    counts = {f"snap{i:02d}": 16 for i in range(5)}
    monkeypatch.setattr(migrate, "chunk_snapshot", _fixed_chunker(counts))

    healed = await watcher._gap_heal(TARGET)

    assert healed == 80                               # rows appended
    assert store.snapshots == 5 and store.rows == 80
    assert store.missing(sorted(bodies)) == []
    assert fake_provider.call_sizes == [32, 32, 16]
    ids, ordinals = _read_group_layout(stores_dir / TARGET)
    assert _assert_contiguous_groups(ids, ordinals) == set(bodies)


@pytest.mark.asyncio
async def test_gap_heal_v1_path_is_todays_whole_text_single_call(
    env, fake_provider, monkeypatch
):
    """v1 active store: exactly today's path — ONE provider call with whole
    texts, one append_many, chunker never consulted."""
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=3)
    store = get_store(TARGET, base_dir=stores_dir)   # autodetect → fresh v1

    def _no_chunk(text, model_key=None):
        raise AssertionError("chunker must not run on the v1 heal path")

    monkeypatch.setattr(migrate, "chunk_snapshot", _no_chunk)

    healed = await watcher._gap_heal(TARGET)

    assert healed == 3
    assert store.schema == 1 and store.rows == 3
    assert fake_provider.call_sizes == [3]           # one whole-text call
    assert sorted(fake_provider.embedded_texts) == sorted(bodies.values())


@pytest.mark.asyncio
async def test_gap_heal_v2_provider_failure_keeps_partial_and_returns(
    env, fake_provider, monkeypatch, capsys
):
    """A mid-heal provider death keeps the already-appended groups and never
    raises (retried next run, quarantine-style)."""
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=5)
    store = get_store(TARGET, base_dir=stores_dir, schema=2)
    counts = {f"snap{i:02d}": 16 for i in range(5)}
    monkeypatch.setattr(migrate, "chunk_snapshot", _fixed_chunker(counts))
    fake_provider.fail_substring = "snap04"          # dies on the LAST batch

    healed = await watcher._gap_heal(TARGET)

    assert healed == 0                                # failure path returns 0
    assert store.snapshots == 4 and store.rows == 64  # first 2 batches kept
    assert "gap-heal failed (will retry next run)" in capsys.readouterr().out


# ── CLI: liveness guard + --rebuild wiring ───────────────────────────────────

@pytest.fixture
def cli_env(env, monkeypatch):
    """CLI runs in-process: silence its SIGINT rewiring."""
    monkeypatch.setattr(backfill, "_install_sigint_cancel", lambda: None)
    return env


def test_service_alive_probe_against_real_socket():
    import http.server
    import socket

    class Quiet(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(500)   # ANY response = a live listener
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Quiet)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        assert backfill._service_alive(
            f"http://127.0.0.1:{port}/embeddings/status", timeout=2.0
        ) is True
    finally:
        server.shutdown()
        server.server_close()

    # closed port → dead
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
    assert backfill._service_alive(
        f"http://127.0.0.1:{free_port}/embeddings/status", timeout=0.5
    ) is False


def test_cli_refuses_when_service_alive(cli_env, monkeypatch, capsys):
    index_path, stores_dir, volume_path = cli_env
    _build_volume(index_path, volume_path, n=1)
    monkeypatch.setattr(backfill, "_service_alive", lambda *a, **k: True)

    rc = backfill.main([
        "--target", TARGET,
        "--stores-dir", str(stores_dir), "--index", str(index_path),
    ])

    assert rc == 5
    out = capsys.readouterr().out
    assert "RUNNING" in out
    assert "POST /embeddings/migrate" in out and "--force" in out
    assert migrate.get_job_status() is None          # nothing ran
    assert not (stores_dir / TARGET).exists()        # nothing written


def test_cli_liveness_guard_skips_list_mode(cli_env, monkeypatch, capsys):
    """--list is the read-only ops view; a live service must not block it."""
    index_path, stores_dir, volume_path = cli_env
    _build_volume(index_path, volume_path, n=1)
    monkeypatch.setattr(
        backfill, "_service_alive",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("probed on --list")),
    )

    rc = backfill.main(
        ["--list", "--stores-dir", str(stores_dir), "--index", str(index_path)]
    )

    assert rc == 0
    assert "EMBEDDING STORES" in capsys.readouterr().out


def test_cli_rebuild_wires_run_rebuild_build_only(cli_env, fake_provider,
                                                  monkeypatch, capsys):
    index_path, stores_dir, volume_path = cli_env
    bodies = _build_volume(index_path, volume_path, n=3)
    monkeypatch.setattr(backfill, "_service_alive", lambda *a, **k: False)

    rc = backfill.main([
        "--rebuild", TARGET,
        "--stores-dir", str(stores_dir), "--index", str(index_path),
    ])

    assert rc == 0
    bstore = get_store(TARGET, base_dir=stores_dir / migrate.BUILD_DIR_NAME,
                       schema=2)
    assert bstore.ids() == set(bodies)
    assert not (stores_dir / "active.json").exists()  # build-only, no cutover
    out = capsys.readouterr().out
    assert "build-only" in out
    assert "separate explicit step" in out            # cutover ≠ this command


def test_cli_rebuild_respects_liveness_guard_and_force(
    cli_env, fake_provider, monkeypatch, capsys
):
    index_path, stores_dir, volume_path = cli_env
    bodies = _build_volume(index_path, volume_path, n=2)
    monkeypatch.setattr(backfill, "_service_alive", lambda *a, **k: True)

    rc = backfill.main([
        "--rebuild", TARGET,
        "--stores-dir", str(stores_dir), "--index", str(index_path),
    ])
    assert rc == 5                                    # refused: service alive

    rc2 = backfill.main([
        "--rebuild", TARGET, "--force",
        "--stores-dir", str(stores_dir), "--index", str(index_path),
    ])
    assert rc2 == 0                                   # explicit override runs
    bstore = get_store(TARGET, base_dir=stores_dir / migrate.BUILD_DIR_NAME,
                       schema=2)
    assert bstore.ids() == set(bodies)


def test_cli_rebuild_unknown_slug_exit_2(cli_env, monkeypatch, capsys):
    index_path, stores_dir, volume_path = cli_env
    monkeypatch.setattr(backfill, "_service_alive", lambda *a, **k: False)
    rc = backfill.main([
        "--rebuild", "no-such-model",
        "--stores-dir", str(stores_dir), "--index", str(index_path),
    ])
    assert rc == 2
    assert "unknown embedding model slug" in capsys.readouterr().out


def test_cli_rebuild_and_target_mutually_exclusive(cli_env, capsys):
    with pytest.raises(SystemExit) as exc:
        backfill.main(["--rebuild", TARGET, "--target", TARGET])
    assert exc.value.code == 2
    assert "not allowed with" in capsys.readouterr().err
