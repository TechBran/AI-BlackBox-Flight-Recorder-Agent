"""Tests for the deprecation watcher — "auto only when forced" (Task 9).

NO NETWORK anywhere: the three vendor catalog seams (_gemini_catalog /
_openai_catalog / _ollama_tags) are monkeypatched on the watcher module, and
the active provider is faked via providers._instances (same recipe as
test_embeddings_migrate.py). Filesystem state (index, stores, volume) lives
in tmp_path; the broken-path migration kick is asserted by monkeypatching
watcher.start_migration.
"""
import asyncio
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator import config, fossils
from Orchestrator.embeddings import ollama_io, providers, watcher
from Orchestrator.embeddings.providers import EmbeddingProviderError
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import get_store, set_active_slug
from Orchestrator.routes.embeddings_routes import _read_health, router

ACTIVE = "gemini-embedding-001"                    # config default active model
ACTIVE_ID = EMBEDDING_MODELS[ACTIVE]["model_id"]   # "models/gemini-embedding-001"
ACTIVE_DIMS = EMBEDDING_MODELS[ACTIVE]["dims"]
OPENAI_SLUG = "openai-text-embedding-3-large"
OPENAI_ID = EMBEDDING_MODELS[OPENAI_SLUG]["model_id"]
QWEN = "qwen3-embedding-0.6b"
QWEN_ID = EMBEDDING_MODELS[QWEN]["model_id"]       # "qwen3-embedding:0.6b"

HEALTH_KEYS = {"state", "detail", "successor", "successor_slug", "checked_at"}


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _silence_notify(monkeypatch):
    """MN.7: the watcher now fires notify() on a health-state transition. These
    tests exercise watcher LOGIC (probe/catalog/migration/gap-heal) and assert
    exact provider-call lists; a real notify() would drive mint_with_content →
    an extra embed call. Stub notify to a no-op so the bus stays out of the way.
    The transition-notify behaviour itself is covered in test_notify_producer_watcher.
    """
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(watcher, "notify", _noop)

@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated index + stores + volume; cloud keys cleared (preflight = not ready)."""
    index_path = tmp_path / "snapshot_index.json"
    stores_dir = tmp_path / "embeddings"
    volume_path = tmp_path / "volume.txt"
    monkeypatch.setattr(fossils, "SNAPSHOT_INDEX", index_path)
    monkeypatch.setattr(fossils, "_index_cache", None)
    monkeypatch.setattr(fossils, "_index_cache_mtime", 0.0)
    monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(stores_dir))
    monkeypatch.setattr(config, "VOL_PATH", volume_path)
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    # Hermetic ollama seams (test_embeddings_routes.py recipe): the route
    # tests below GET /embeddings/status, whose _ollama_state() would
    # otherwise probe a real daemon on :11434. (Watcher viability uses its
    # own _ollama_tags seam — the `catalogs` fixture — so this is safe.)
    monkeypatch.setattr(ollama_io, "binary_installed", lambda: False)
    monkeypatch.setattr(ollama_io, "daemon_version", lambda: None)
    monkeypatch.setattr(ollama_io, "local_models", lambda: [])
    monkeypatch.setattr(ollama_io, "ram_preflight", lambda ram_gb: None)
    return index_path, stores_dir, volume_path


@pytest.fixture
def catalogs(monkeypatch):
    """All three catalog seams mocked. Values are lists (returned) or an
    Exception instance (raised — simulates an unreachable catalog endpoint)."""
    state = {
        "gemini": [ACTIVE_ID],   # active model listed, no successor
        "openai": [],
        "ollama": [],
    }

    def seam(key):
        async def fetch():
            value = state[key]
            if isinstance(value, Exception):
                raise value
            return list(value)
        return fetch

    monkeypatch.setattr(watcher, "_gemini_catalog", seam("gemini"))
    monkeypatch.setattr(watcher, "_openai_catalog", seam("openai"))
    monkeypatch.setattr(watcher, "_ollama_tags", seam("ollama"))
    return state


class FakeProvider:
    """Deterministic vectors; can fail outright (probe), for the first N calls
    (transient blip), or per-substring (heal)."""

    def __init__(self, dims, fail_all=False, fail_substring=None, fail_first_n=0):
        self.dims = dims
        self.fail_all = fail_all
        self.fail_substring = fail_substring
        self.fail_first_n = fail_first_n
        self.attempts = 0  # every embed() call, including the failing ones
        self.calls = []  # [(texts, purpose), ...] — successful calls only

    @property
    def embedded_texts(self):
        return [t for texts, _ in self.calls for t in texts]

    async def embed(self, texts, purpose):
        self.attempts += 1
        if self.fail_all or self.attempts <= self.fail_first_n:
            raise EmbeddingProviderError("synthetic dead provider")
        if self.fail_substring is not None and any(
            self.fail_substring in t for t in texts
        ):
            raise EmbeddingProviderError("synthetic batch failure")
        self.calls.append((list(texts), purpose))
        return [self._vec(t) for t in texts]

    def _vec(self, text):
        rng = np.random.default_rng(sum(text.encode()) % (2**32))
        return [float(x) for x in rng.standard_normal(self.dims)]


@pytest.fixture
def fake_provider(monkeypatch):
    fake = FakeProvider(ACTIVE_DIMS)
    monkeypatch.setitem(providers._instances, ACTIVE, fake)
    return fake


@pytest.fixture
def migration_spy(monkeypatch, env):
    """Replaces watcher.start_migration; records targets + the health.json
    contents AT CALL TIME (proves health is written BEFORE the job kicks)."""
    _, stores_dir, _ = env
    calls, health_at_call = [], []

    async def fake_start(target):
        health_at_call.append(
            json.loads((stores_dir / watcher.HEALTH_FILE).read_text(encoding="utf-8"))
        )
        calls.append(target)
        return {"state": "running", "target": target}

    monkeypatch.setattr(watcher, "start_migration", fake_start)
    return calls, health_at_call


def _build_volume(index_path, volume_path, n=5):
    """Concatenated snapshot bodies with correct byte offsets in the index."""
    index, bodies, blob = {}, {}, b""
    for i in range(n):
        sid = f"SNAP-{i}"
        body = f"=== snapshot body {i} for {sid} ===\n"
        raw = body.encode("utf-8")
        index[sid] = {
            "byte_start": len(blob), "byte_end": len(blob) + len(raw),
            "operator": "Brandon", "timestamp": "2026-06-12T00:00:00Z",
            "type": "normal",
        }
        blob += raw
        bodies[sid] = body
    volume_path.write_bytes(blob)
    index_path.write_text(json.dumps(index), encoding="utf-8")
    fossils._index_cache = None
    return bodies


def _read_health_file(stores_dir):
    return json.loads((stores_dir / watcher.HEALTH_FILE).read_text(encoding="utf-8"))


# ── ok state ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ok_probe_ok_and_listed(env, catalogs, fake_provider):
    _, stores_dir, _ = env

    health = await watcher.run_health_check()

    assert health["state"] == "ok"
    assert health["detail"] == ""
    assert health["successor"] is None
    assert health["checked_at"]  # ISO timestamp present
    # probe used the active provider with a document purpose
    assert fake_provider.calls == [(["health probe"], "document")]
    # written to disk with checked_at, atomically (no tmp leftover)
    on_disk = _read_health_file(stores_dir)
    assert set(on_disk.keys()) == HEALTH_KEYS
    assert on_disk["state"] == "ok" and on_disk["checked_at"]
    assert not (stores_dir / (watcher.HEALTH_FILE + ".tmp")).exists()


@pytest.mark.asyncio
async def test_catalog_unreachable_stays_ok_with_note(env, catalogs, fake_provider):
    _, stores_dir, _ = env
    catalogs["gemini"] = ConnectionError("dns down")

    health = await watcher.run_health_check()

    assert health["state"] == "ok"  # probe ok — a dead catalog is not a dead model
    assert "catalog check skipped" in health["detail"]
    assert health["successor"] is None
    assert _read_health_file(stores_dir)["state"] == "ok"


@pytest.mark.asyncio
async def test_live_gemini_catalog_today_produces_ok(env, catalogs, fake_provider):
    """Regression: the REAL current Gemini catalog (older families like
    text-embedding-004 and an -exp entry) must not brand a false successor."""
    catalogs["gemini"] = [
        "models/embedding-001",
        "models/text-embedding-004",
        "models/gemini-embedding-exp-03-07",
        ACTIVE_ID,
    ]

    health = await watcher.run_health_check()

    assert health["state"] == "ok"
    assert health["successor"] is None


# ── superseded ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_superseded_newest_same_family_successor(env, catalogs, fake_provider):
    _, stores_dir, _ = env
    catalogs["gemini"] = [
        ACTIVE_ID, "models/gemini-embedding-002", "models/gemini-embedding-003",
    ]

    health = await watcher.run_health_check()

    assert health["state"] == "superseded"
    # newest candidate wins; unmapped vendor id reported as a raw string
    assert health["successor"] == "models/gemini-embedding-003"
    assert health["successor_slug"] is None  # display copy only — not in registry
    assert "models/gemini-embedding-003" in health["detail"]
    assert "still works" in health["detail"]
    assert _read_health_file(stores_dir)["state"] == "superseded"


@pytest.mark.asyncio
async def test_preview_and_exp_successors_are_ignored(env, catalogs, fake_provider):
    catalogs["gemini"] = [
        ACTIVE_ID,
        "models/gemini-embedding-002-preview",
        "models/gemini-embedding-exp-03-07",
    ]

    health = await watcher.run_health_check()

    assert health["state"] == "ok"  # GA-over-preview: previews are not successors
    assert health["successor"] is None


@pytest.mark.asyncio
async def test_delisted_but_probing_is_superseded(env, catalogs, fake_provider):
    catalogs["gemini"] = ["models/gemini-embedding-002"]  # current model gone

    health = await watcher.run_health_check()

    assert health["state"] == "superseded"
    assert "no longer listed" in health["detail"]
    assert health["successor"] == "models/gemini-embedding-002"


@pytest.mark.asyncio
async def test_openai_active_successor_and_family_filter(env, catalogs, monkeypatch):
    set_active_slug(OPENAI_SLUG)
    fake = FakeProvider(EMBEDDING_MODELS[OPENAI_SLUG]["dims"])
    monkeypatch.setitem(providers._instances, OPENAI_SLUG, fake)

    # ada-002 / -small are different families — not successors of -3-large
    catalogs["openai"] = [
        OPENAI_ID, "text-embedding-3-small", "text-embedding-ada-002", "gpt-4o",
    ]
    health = await watcher.run_health_check()
    assert health["state"] == "ok"
    assert health["successor"] is None

    # a real same-family bump IS one
    catalogs["openai"] = [OPENAI_ID, "text-embedding-4-large", "text-embedding-ada-002"]
    health = await watcher.run_health_check()
    assert health["state"] == "superseded"
    assert health["successor"] == "text-embedding-4-large"


@pytest.mark.asyncio
async def test_ollama_active_listed_then_removed(env, catalogs, monkeypatch):
    set_active_slug(QWEN)
    fake = FakeProvider(EMBEDDING_MODELS[QWEN]["dims"])
    monkeypatch.setitem(providers._instances, QWEN, fake)

    catalogs["ollama"] = [QWEN_ID]
    health = await watcher.run_health_check()
    assert health["state"] == "ok"
    assert health["successor"] is None  # local models never get successors

    catalogs["ollama"] = []
    health = await watcher.run_health_check()
    assert health["state"] == "superseded"
    assert health["successor"] is None


# ── broken: auto-migrate target precedence ───────────────────────────────────

GEMINI_002 = {
    "provider": "gemini", "model_id": "models/gemini-embedding-002", "dims": 3072,
    "label": "Gemini 002 (test)", "ram_gb": 0.0, "cost_per_1m_tokens": 0.15,
    "privacy": "cloud", "quality_note": "test successor",
    "query_instruction": None, "keep_alive": None,
}


@pytest.fixture
def broken_provider(monkeypatch):
    fake = FakeProvider(ACTIVE_DIMS, fail_all=True)
    monkeypatch.setitem(providers._instances, ACTIVE, fake)
    # the in-run re-probe must not really wait 60s in tests
    monkeypatch.setattr(watcher, "RETRY_PROBE_DELAY_S", 0.01)
    return fake


@pytest.fixture
def prior_broken_health(env):
    """health.json from a 'previous run' that already said broken — the
    consecutive-failing-runs gate that lets auto-migration actually kick."""
    _, stores_dir, _ = env
    stores_dir.mkdir(parents=True, exist_ok=True)
    (stores_dir / watcher.HEALTH_FILE).write_text(
        json.dumps({
            "state": "broken",
            "detail": "previous run: probe failed",
            "successor": None,
            "successor_slug": None,
            "checked_at": "2026-06-11T00:00:00+00:00",
        }),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_broken_prefers_ready_registry_successor(
    env, catalogs, broken_provider, migration_spy, prior_broken_health, monkeypatch
):
    calls, health_at_call = migration_spy
    monkeypatch.setitem(EMBEDDING_MODELS, "gemini-embedding-002", GEMINI_002)
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "test-key")  # successor ready
    catalogs["gemini"] = [ACTIVE_ID, "models/gemini-embedding-002"]

    health = await watcher.run_health_check()

    assert health["state"] == "broken"
    assert calls == ["gemini-embedding-002"]   # registry slug, not the vendor id
    assert "vendor successor" in health["detail"]
    # health.json was written BEFORE the migration was kicked
    assert health_at_call[0]["state"] == "broken"
    assert "auto-migrating to gemini-embedding-002" in health_at_call[0]["detail"]


@pytest.mark.asyncio
async def test_broken_falls_back_to_most_complete_ready_store(
    env, catalogs, broken_provider, migration_spy, prior_broken_health, monkeypatch
):
    _, stores_dir, _ = env
    calls, _ = migration_spy
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key")  # openai ready
    rng = np.random.default_rng(3)
    # active store is the biggest but must be excluded (it's the broken one)
    get_store(ACTIVE).append_many(
        [(f"A-{i}", rng.standard_normal(ACTIVE_DIMS)) for i in range(20)]
    )
    # openai store: ready, 5 vectors → the viable pick
    get_store(OPENAI_SLUG).append_many(
        [(f"O-{i}", rng.standard_normal(3072)) for i in range(5)]
    )
    # qwen store: MORE vectors but ollama has no model pulled → not ready
    get_store(QWEN).append_many(
        [(f"Q-{i}", rng.standard_normal(1024)) for i in range(10)]
    )

    health = await watcher.run_health_check()

    assert health["state"] == "broken"
    assert calls == [OPENAI_SLUG]
    assert "most complete cloud ready store (5 vectors)" in health["detail"]


@pytest.mark.asyncio
async def test_broken_fallback_prefers_local_store_over_bigger_cloud_store(
    env, catalogs, broken_provider, migration_spy, prior_broken_health, monkeypatch
):
    """Spend consent: a ready LOCAL store wins the emergency fallback even
    when a ready cloud store holds more vectors."""
    calls, _ = migration_spy
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key")  # cloud ready
    catalogs["ollama"] = [QWEN_ID]                             # local ready too
    rng = np.random.default_rng(7)
    get_store(OPENAI_SLUG).append_many(
        [(f"O-{i}", rng.standard_normal(3072)) for i in range(8)]
    )
    get_store(QWEN).append_many(
        [(f"Q-{i}", rng.standard_normal(1024)) for i in range(5)]
    )

    health = await watcher.run_health_check()

    assert health["state"] == "broken"
    assert calls == [QWEN]  # local 5 beats cloud 8
    assert "most complete local ready store (5 vectors)" in health["detail"]


@pytest.mark.asyncio
async def test_broken_falls_back_to_local_0_6b(
    env, catalogs, broken_provider, migration_spy, prior_broken_health
):
    calls, health_at_call = migration_spy
    catalogs["ollama"] = [QWEN_ID]  # daemon up, model pulled — only viable target

    health = await watcher.run_health_check()

    assert health["state"] == "broken"
    assert calls == [QWEN]
    assert "local fallback" in health["detail"]
    assert health_at_call[0]["state"] == "broken"  # written before the kick


@pytest.mark.asyncio
async def test_broken_with_no_viable_target_stays_broken(
    env, catalogs, broken_provider, migration_spy, prior_broken_health
):
    _, stores_dir, _ = env
    calls, _ = migration_spy
    catalogs["ollama"] = ConnectionError("daemon down")  # local not ready either

    health = await watcher.run_health_check()

    assert health["state"] == "broken"
    assert calls == []  # no migration kicked
    assert "no viable auto-migrate target" in health["detail"]
    assert "no ready registry successor" in health["detail"]
    assert "no other ready store" in health["detail"]
    assert "local fallback not ready" in health["detail"]
    assert _read_health_file(stores_dir)["state"] == "broken"


# ── broken: recent-end gap guard on the candidate-store fallback (F4) ─────────
#
# A fallback store frozen at an old date (missing the NEWEST snapshots) must
# NOT be auto-activated — a broken active key would otherwise silently lose
# recent memory from search. The candidate-#2 block computes each store's
# missing set against the live snapshot index and rejects a store with a
# recent-end gap (more than RECENT_GAP_MAX total OR any of the newest
# RECENT_GAP_TAIL snap_ids). Prefer "stay broken with a loud banner".


def _build_index_only(index_path, n=60, start=100):
    """Snapshot index with monotonic counters SNAP-20260612-{start..start+n-1}.

    No volume bytes needed: the broken path never gap-heals, so the index is
    consulted ONLY by the gap guard (store.missing). Returns the ordered ids.
    """
    index = {}
    ids = []
    for i in range(n):
        sid = f"SNAP-20260612-{start + i}"
        index[sid] = {
            "byte_start": 0, "byte_end": 1,
            "operator": "Brandon", "timestamp": "2026-06-12T00:00:00Z",
            "type": "normal",
        }
        ids.append(sid)
    index_path.write_text(json.dumps(index), encoding="utf-8")
    fossils._index_cache = None
    return ids


@pytest.mark.asyncio
async def test_broken_rejects_candidate_store_missing_newest_snapshots(
    env, catalogs, broken_provider, migration_spy, prior_broken_health, monkeypatch
):
    """The only OTHER ready store is frozen old (missing the newest ids) → it is
    rejected for a recent-end gap and the watcher stays broken, loudly."""
    index_path, stores_dir, _ = env
    calls, _ = migration_spy
    ids = _build_index_only(index_path, n=60)          # 60 indexed snapshots
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key")  # openai ready
    rng = np.random.default_rng(11)
    # openai store holds ONLY the oldest 10 — missing all 50 newest → stale
    get_store(OPENAI_SLUG).append_many(
        [(ids[i], rng.standard_normal(3072)) for i in range(10)]
    )

    health = await watcher.run_health_check()

    assert health["state"] == "broken"
    assert calls == []  # the stale store was NOT auto-activated
    assert "no viable auto-migrate target" in health["detail"]
    # the rejection reason names the slug + the recent gap (newest-tail miss)
    assert OPENAI_SLUG in health["detail"]
    assert "newest" in health["detail"]
    assert _read_health_file(stores_dir)["state"] == "broken"


@pytest.mark.asyncio
async def test_broken_rejects_candidate_store_over_total_gap_cap(
    env, catalogs, broken_provider, migration_spy, prior_broken_health, monkeypatch
):
    """A store that HAS the newest tail but is missing more than RECENT_GAP_MAX
    total ids is still rejected (the total-gap arm of the guard)."""
    index_path, stores_dir, _ = env
    calls, _ = migration_spy
    ids = _build_index_only(index_path, n=60)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key")
    # tighten the tail so the newest-tail arm passes, isolating the total-gap arm
    monkeypatch.setattr(watcher, "RECENT_GAP_TAIL", 5)
    monkeypatch.setattr(watcher, "RECENT_GAP_MAX", 25)
    rng = np.random.default_rng(12)
    # has the newest 5 (tail ok) but a big hole in the middle: missing 30 total
    have = ids[:25] + ids[55:]   # 25 + 5 = 30 present, 30 missing (>25 cap)
    get_store(OPENAI_SLUG).append_many(
        [(sid, rng.standard_normal(3072)) for sid in have]
    )

    health = await watcher.run_health_check()

    assert health["state"] == "broken"
    assert calls == []
    assert OPENAI_SLUG in health["detail"]
    assert _read_health_file(stores_dir)["state"] == "broken"


@pytest.mark.asyncio
async def test_broken_accepts_complete_candidate_store_no_false_rejection(
    env, catalogs, broken_provider, migration_spy, prior_broken_health, monkeypatch
):
    """A COMPLETE candidate store (no recent-end gap) is still pickable — the
    guard must not produce false rejections."""
    index_path, stores_dir, _ = env
    calls, _ = migration_spy
    ids = _build_index_only(index_path, n=60)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key")
    rng = np.random.default_rng(13)
    # openai store has EVERY indexed snapshot → no gap → viable
    get_store(OPENAI_SLUG).append_many(
        [(sid, rng.standard_normal(3072)) for sid in ids]
    )

    health = await watcher.run_health_check()

    assert health["state"] == "broken"
    assert calls == [OPENAI_SLUG]  # complete store picked, no false rejection
    assert "most complete cloud ready store" in health["detail"]


@pytest.mark.asyncio
async def test_broken_accepts_store_with_small_tail_only_gap(
    env, catalogs, broken_provider, migration_spy, prior_broken_health, monkeypatch
):
    """A store missing only a handful of ids — none in the newest tail and
    under the total cap — is NOT rejected (the guard is recent-end specific,
    not 'must be 100% complete')."""
    index_path, stores_dir, _ = env
    calls, _ = migration_spy
    ids = _build_index_only(index_path, n=60)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(watcher, "RECENT_GAP_TAIL", 50)
    monkeypatch.setattr(watcher, "RECENT_GAP_MAX", 25)
    rng = np.random.default_rng(14)
    # missing exactly 3 ids, all OLD (indices 0-2), none in the newest 50 →
    # 3 total < 25 cap AND 0 of the newest-50 missing → still viable
    have = ids[3:]
    get_store(OPENAI_SLUG).append_many(
        [(sid, rng.standard_normal(3072)) for sid in have]
    )

    health = await watcher.run_health_check()

    assert health["state"] == "broken"
    assert calls == [OPENAI_SLUG]  # small old-only gap is acceptable
    assert "most complete cloud ready store" in health["detail"]


@pytest.mark.asyncio
async def test_pick_migration_target_gap_guard_unit(
    env, catalogs, monkeypatch
):
    """Direct _pick_migration_target unit: a stale candidate is rejected with a
    reason naming the recent gap; the function returns (None, reasons)."""
    index_path, stores_dir, _ = env
    ids = _build_index_only(index_path, n=60)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key")
    rng = np.random.default_rng(15)
    get_store(OPENAI_SLUG).append_many(
        [(ids[i], rng.standard_normal(3072)) for i in range(10)]  # oldest 10 only
    )

    target, why = await watcher._pick_migration_target(ACTIVE, None)

    assert target is None
    assert OPENAI_SLUG in why
    assert "newest" in why


@pytest.mark.asyncio
async def test_pick_target_ranks_by_snapshot_coverage_not_rows(
    env, catalogs, monkeypatch
):
    """M6e/audit A11 pin: candidate ranking is SNAPSHOT coverage. A v2 chunk
    store whose raw row count is inflated by chunking (100 snapshots / 250
    rows) must NOT outrank a v1 store covering more snapshots (120). Both
    candidates are cloud (ready) and pass the recent-end gap guard, so the
    count sort alone decides."""
    index_path, stores_dir, _ = env
    ids = _build_index_only(index_path, n=120)
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "test-key")   # gemini-2 ready
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key")   # openai ready
    # both stores must clear the F4 guard: v2 misses the 20 OLDEST ids only
    monkeypatch.setattr(watcher, "RECENT_GAP_TAIL", 20)
    monkeypatch.setattr(watcher, "RECENT_GAP_MAX", 30)
    rng = np.random.default_rng(21)
    # v1 candidate: every indexed snapshot, one row each → 120 snapshots
    get_store("gemini-embedding-2").append_many(
        [(sid, rng.standard_normal(3072)) for sid in ids]
    )
    # v2 candidate: the newest 100 snapshots as chunk GROUPS → 250 raw rows
    v2 = get_store(OPENAI_SLUG, schema=2)
    for i, sid in enumerate(ids[20:]):
        n_chunks = 3 if i < 50 else 2   # 50*3 + 50*2 = 250 rows
        v2.append_group(sid, [rng.standard_normal(3072) for _ in range(n_chunks)])
    assert v2.snapshots == 100 and v2.rows == 250  # fixture sanity

    target, why = await watcher._pick_migration_target(ACTIVE, None)

    # 120 snapshots beats 100 snapshots; 250 rows must never win the sort
    assert target == "gemini-embedding-2"
    assert "(120 vectors)" in why


@pytest.mark.asyncio
async def test_broken_while_migration_already_running(
    env, catalogs, broken_provider, prior_broken_health, monkeypatch
):
    _, stores_dir, _ = env
    catalogs["ollama"] = [QWEN_ID]

    async def already_running(target):
        raise RuntimeError(f"a migration to {target!r} is already running")

    monkeypatch.setattr(watcher, "start_migration", already_running)

    health = await watcher.run_health_check()  # must not raise

    assert health["state"] == "broken"
    assert "migration not started" in health["detail"]
    assert "already running" in health["detail"]
    # the rewrite landed on disk too
    assert "already running" in _read_health_file(stores_dir)["detail"]


# ── broken: false-broken debounce ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_broken_run_writes_health_but_defers_migration(
    env, catalogs, broken_provider, migration_spy, capsys
):
    """A single failing run (both probes) is NOT enough to auto-migrate, even
    with a viable target standing by — the kick needs a consecutive broken run."""
    _, stores_dir, _ = env
    calls, _ = migration_spy
    catalogs["ollama"] = [QWEN_ID]  # a viable local fallback IS available

    health = await watcher.run_health_check()

    assert health["state"] == "broken"
    assert calls == []  # debounced: no migration on the first failing run
    assert broken_provider.attempts == 2  # probe + the one in-run re-probe
    assert "deferred" in health["detail"]
    assert _read_health_file(stores_dir)["state"] == "broken"
    assert "EMBEDDINGS BROKEN" in capsys.readouterr().out  # loud banner


@pytest.mark.asyncio
async def test_consecutive_broken_runs_kick_migration(
    env, catalogs, broken_provider, migration_spy, prior_broken_health
):
    """Previous health.json already broken + still failing → migration kicks."""
    calls, health_at_call = migration_spy
    catalogs["ollama"] = [QWEN_ID]

    health = await watcher.run_health_check()

    assert health["state"] == "broken"
    assert calls == [QWEN]
    assert health_at_call[0]["state"] == "broken"  # written before the kick


@pytest.mark.asyncio
async def test_transient_probe_blip_is_absorbed(env, catalogs, monkeypatch):
    """First probe fails, the in-run re-probe succeeds → ok, never broken."""
    _, stores_dir, _ = env
    fake = FakeProvider(ACTIVE_DIMS, fail_first_n=1)
    monkeypatch.setitem(providers._instances, ACTIVE, fake)
    monkeypatch.setattr(watcher, "RETRY_PROBE_DELAY_S", 0.01)

    health = await watcher.run_health_check()

    assert health["state"] == "ok"
    assert fake.attempts == 2  # failed probe + successful re-probe
    assert _read_health_file(stores_dir)["state"] == "ok"


def test_recheck_interval_hourly_only_while_broken():
    """Broken state rechecks in 1h (confirm/recover fast); else daily."""
    assert watcher.WATCH_INTERVAL_BROKEN_S == 3600
    assert watcher.WATCH_INTERVAL_OK_S == 24 * 3600


def test_registry_maps_gemini_embedding_2_vendor_id():
    """Registering gemini-embedding-2 maps its vendor id → slug, so the watcher
    resolves successor_slug (which lights up the one-click Upgrade button on the
    superseded banner across every surface)."""
    from Orchestrator.embeddings.watcher import _registry_slug_for
    assert _registry_slug_for("gemini", "models/gemini-embedding-2") == "gemini-embedding-2"
    assert watcher._next_interval("broken") == watcher.WATCH_INTERVAL_BROKEN_S
    assert watcher._next_interval("ok") == watcher.WATCH_INTERVAL_OK_S
    assert watcher._next_interval("superseded") == watcher.WATCH_INTERVAL_OK_S


# ── gap-heal (ok state only) ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gap_heal_embeds_missing_ids(env, catalogs, fake_provider):
    index_path, stores_dir, volume_path = env
    bodies = _build_volume(index_path, volume_path, n=5)
    store = get_store(ACTIVE)
    rng = np.random.default_rng(9)
    store.append_many(
        [(f"SNAP-{i}", rng.standard_normal(ACTIVE_DIMS)) for i in range(2)]
    )

    health = await watcher.run_health_check()

    assert health["state"] == "ok"
    assert health["healed"] == 3
    assert store.ids() == set(bodies)
    healed_texts = [t for t in fake_provider.embedded_texts if t != "health probe"]
    assert sorted(healed_texts) == sorted(bodies[f"SNAP-{i}"] for i in range(2, 5))


@pytest.mark.asyncio
async def test_gap_heal_caps_at_50(env, catalogs, fake_provider):
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=55)

    health = await watcher.run_health_check()

    assert health["state"] == "ok"
    assert health["healed"] == 50
    assert get_store(ACTIVE).count == 50  # the remaining 5 wait for the next run


@pytest.mark.asyncio
async def test_gap_heal_provider_failure_keeps_state_ok(
    env, catalogs, monkeypatch, capsys
):
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=3)
    # probe text embeds fine; every real snapshot body fails
    fake = FakeProvider(ACTIVE_DIMS, fail_substring="snapshot body")
    monkeypatch.setitem(providers._instances, ACTIVE, fake)

    health = await watcher.run_health_check()  # must not raise

    assert health["state"] == "ok"
    assert "healed" not in health
    assert get_store(ACTIVE).count == 0
    assert "gap-heal failed" in capsys.readouterr().out
    assert _read_health_file(stores_dir)["state"] == "ok"


@pytest.mark.asyncio
async def test_gap_heal_threads_active_store_content_mode(
    env, catalogs, fake_provider, monkeypatch
):
    """M14.3e: gap-heal is mint catch-up, so it MUST chunk in the active store's
    content_mode. A body-mode active store must never gain full-mode
    (envelope-inclusive ordinal-0 + shifted ordinals) chunks from the healer, or
    the windower desyncs for every healed snapshot. Regression guard for the
    watcher.py:400 miss the M14.3 review caught."""
    index_path, stores_dir, volume_path = env
    _build_volume(index_path, volume_path, n=3)
    # Active store as a v2 BODY-mode store; append_group one snapshot so
    # meta.json persists content_mode="body", leaving the rest missing() to heal.
    store = get_store(ACTIVE, schema=2, content_mode="body")
    rng = np.random.default_rng(3)
    store.append_group("SNAP-0", [rng.standard_normal(ACTIVE_DIMS)])
    assert store.content_mode == "body"  # fixture sanity

    captured = {}

    def spy(ids_texts, model_key, content_mode="full"):
        captured["mode"] = content_mode
        return [], [sid for sid, _ in ids_texts]  # short-circuit: all "empty"

    monkeypatch.setattr(watcher, "chunk_group_batches", spy)

    await watcher._gap_heal(ACTIVE)

    # The healer must pass the store's mode through — not the "full" default.
    assert captured["mode"] == "body"


# ── health.json shape through the routes reader ──────────────────────────────

@pytest.mark.asyncio
async def test_health_file_round_trips_through_routes_reader(
    env, catalogs, fake_provider
):
    _, stores_dir, _ = env
    catalogs["gemini"] = [ACTIVE_ID, "models/gemini-embedding-002"]

    await watcher.run_health_check()

    via_routes = _read_health(stores_dir)
    assert set(via_routes.keys()) == {"state", "detail", "successor", "successor_slug"}
    assert via_routes["state"] == "superseded"
    assert via_routes["successor"] == "models/gemini-embedding-002"
    assert via_routes["successor_slug"] is None  # vendor id unmapped in registry
    assert "newer" in via_routes["detail"]


@pytest.mark.asyncio
async def test_registry_mapped_successor_slug_round_trips(
    env, catalogs, fake_provider, monkeypatch
):
    """A registry-mapped successor carries its slug in successor_slug — the
    ONLY field the Task 14/15 [Update] button may bind to — alongside the
    display successor, and it survives the routes reader."""
    _, stores_dir, _ = env
    monkeypatch.setitem(EMBEDDING_MODELS, "gemini-embedding-002", GEMINI_002)
    catalogs["gemini"] = [ACTIVE_ID, "models/gemini-embedding-002"]

    health = await watcher.run_health_check()

    assert health["state"] == "superseded"
    assert health["successor"] == "gemini-embedding-002"       # mapped → slug shown
    assert health["successor_slug"] == "gemini-embedding-002"  # registry-bound
    assert _read_health_file(stores_dir)["successor_slug"] == "gemini-embedding-002"
    assert _read_health(stores_dir)["successor_slug"] == "gemini-embedding-002"


# ── route: manual trigger ────────────────────────────────────────────────────

@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(router)
    return app


def test_health_check_route_returns_health_dict(env, catalogs, fake_provider, app):
    with TestClient(app) as client:
        resp = client.post("/embeddings/health/check")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "ok"
        assert body["successor"] is None
        assert body["successor_slug"] is None
        assert body["checked_at"]

        # the fresh health.json is what /embeddings/status now serves
        status_health = client.get("/embeddings/status").json()["health"]
        assert status_health == {
            "state": "ok", "detail": "", "successor": None, "successor_slug": None,
        }


# ── scheduling ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_watcher_is_idempotent(monkeypatch):
    monkeypatch.setattr(watcher, "_WATCHER_TASK", None)

    task1 = watcher.start_watcher()
    task2 = watcher.start_watcher()

    assert task1 is task2  # live task reused, never doubled
    task1.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task1


@pytest.mark.asyncio
async def test_watcher_task_death_logs_loud_error(monkeypatch, capsys):
    """A watcher task dying with a non-CancelledError must leave a loud
    [WATCHER] ERROR journal line (mirror of migrate's engine done-callback)."""
    monkeypatch.setattr(watcher, "_WATCHER_TASK", None)

    async def boom():
        raise RuntimeError("synthetic watcher death")

    monkeypatch.setattr(watcher, "_watch_forever", boom)
    task = watcher.start_watcher()
    with pytest.raises(RuntimeError, match="synthetic watcher death"):
        await task
    await asyncio.sleep(0)  # let the done-callback fire

    out = capsys.readouterr().out
    assert "[WATCHER] ERROR" in out
    assert "synthetic watcher death" in out


@pytest.mark.asyncio
async def test_watcher_task_cancel_is_not_an_error(monkeypatch, capsys):
    monkeypatch.setattr(watcher, "_WATCHER_TASK", None)

    task = watcher.start_watcher()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert "[WATCHER] ERROR" not in capsys.readouterr().out
