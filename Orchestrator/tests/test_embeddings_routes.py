"""Tests for /embeddings/status + /embeddings/validate (Task 7).

The status JSON shape is a BINDING contract (wizard step, Portal card, Android
card in Tasks 13-15) — shape assertions here are deliberate lock-in.

Lightweight pattern: a small FastAPI() with just the embeddings router (the
full app's startup hooks are irrelevant here). All filesystem state lives in
tmp_path fixtures — never the real Manifest/ (same isolation recipe as
test_embeddings_mint.py).
"""
import json
import re

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator import config, fossils, hardware
from Orchestrator.embeddings import ollama_io, providers
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import get_store, set_active_slug
from Orchestrator.routes.embeddings_routes import router

SLUG = "gemini-embedding-001"
DIMS = 3072

# hardware is the WI-9/M10 ADDITIVE contract extension (host probe rollup).
STATUS_KEYS = {"active", "health", "job", "stores", "models", "ollama", "hardware"}
# schema + rows are the M6e ADDITIVE contract extension (chunked-store ops
# currency); placement + recommended_placement are WI-9/M10 (device placement);
# cpu_warning is the reranker-tiering M9 ADDITIVE advisory (re-embed slowness /
# LOW-tier cloud steering); strategy is the re-embed-UI ADDITIVE label derived
# from the store schema (chunked / whole_document / none); every pre-existing
# key below is unchanged.
MODEL_KEYS = {
    "slug", "label", "dims", "ram_gb", "cost_per_1m_tokens", "privacy",
    "quality_note", "store_exists", "schema", "rows", "missing", "ready",
    "blockers", "keep_alive", "warm", "placement", "recommended_placement",
    "cpu_warning", "strategy", "member_id", "downloadable",
}
STORE_KEYS = {"slug", "dims", "count", "schema", "rows", "missing", "last_updated"}

# Hermetic no-GPU probe result (this box's live shape) for the env fixture.
# tier is the reranker-tiering M1 field the real probe now always returns; this
# box is LOW (no GPU, <32 GB RAM), so the default env exercises LOW-tier copy.
NO_GPU_HW = {
    "gpu": False, "gpu_name": None, "vram_mb": None,
    "ram_mb": 31167, "source": "none", "tier": "LOW",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated index + stores dir; fossils.SNAPSHOT_INDEX is bound at import
    time so it (and its mtime cache) is patched on the fossils module."""
    index_path = tmp_path / "snapshot_index.json"
    stores_dir = tmp_path / "embeddings"
    monkeypatch.setattr(fossils, "SNAPSHOT_INDEX", index_path)
    monkeypatch.setattr(fossils, "_index_cache", None)
    monkeypatch.setattr(fossils, "_index_cache_mtime", 0.0)
    monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(stores_dir))
    set_active_slug(SLUG, base_dir=stores_dir)
    # Hermetic ollama world (no real daemon probes from these tests): binary
    # absent, daemon down, RAM fine. test_embeddings_ollama.py exercises the
    # real seams; here they would make assertions environment-dependent.
    monkeypatch.setattr(ollama_io, "binary_installed", lambda: False)
    monkeypatch.setattr(ollama_io, "daemon_version", lambda: None)
    monkeypatch.setattr(ollama_io, "local_models", lambda: [])
    monkeypatch.setattr(ollama_io, "ram_preflight", lambda ram_gb: None)
    from Orchestrator import local_stack
    monkeypatch.setattr(local_stack, "is_installed", lambda: False)
    monkeypatch.setattr(local_stack, "is_healthy", lambda: False)
    monkeypatch.setattr(local_stack, "model_downloaded", lambda mid: False)
    monkeypatch.setattr(local_stack, "get_member_ttl", lambda mid: None)
    # Hermetic hardware probe (WI-9): no subprocess from these tests; the real
    # command seams are exercised by test_hardware.py.
    monkeypatch.setattr(hardware, "probe", lambda ttl_s=60.0: dict(NO_GPU_HW))
    return index_path, stores_dir


@pytest.fixture
def client(env):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _write_index(index_path, snap_ids):
    index = {
        sid: {
            "byte_start": 0, "byte_end": 9, "operator": "Brandon",
            "timestamp": "2026-06-11T00:00:00Z", "type": "normal",
        }
        for sid in snap_ids
    }
    index_path.write_text(json.dumps(index), encoding="utf-8")


def _populate_store(stores_dir, slug, snap_ids):
    dims = EMBEDDING_MODELS[slug]["dims"]
    store = get_store(slug, base_dir=stores_dir)
    rng = np.random.default_rng(42)
    store.append_many([(sid, rng.standard_normal(dims)) for sid in snap_ids])


# ── status shape (binding contract) ──────────────────────────────────────────

def test_status_shape(env, client):
    index_path, stores_dir = env
    _write_index(index_path, ["SNAP-1", "SNAP-2"])
    _populate_store(stores_dir, SLUG, ["SNAP-1"])

    resp = client.get("/embeddings/status")
    assert resp.status_code == 200
    # no-store so a heuristically-caching WebView can't keep drawing a stale
    # "upgrade available" banner after a migration/registration flips the state
    assert resp.headers.get("cache-control") == "no-store"
    body = resp.json()

    assert set(body.keys()) == STATUS_KEYS
    assert body["active"] == SLUG
    assert body["health"] == {
        "state": "ok", "detail": "", "successor": None, "successor_slug": None,
    }
    assert body["job"] is None  # Task 8 fills
    assert isinstance(body["stores"], list)
    for store in body["stores"]:
        assert set(store.keys()) == STORE_KEYS

    # models = exactly the registry, every entry with the full contract keys
    assert len(body["models"]) == len(EMBEDDING_MODELS)
    assert {m["slug"] for m in body["models"]} == set(EMBEDDING_MODELS)
    for model in body["models"]:
        assert set(model.keys()) == MODEL_KEYS
        entry = EMBEDDING_MODELS[model["slug"]]
        assert model["label"] == entry["label"]
        assert model["dims"] == entry["dims"]
        assert model["ram_gb"] == entry["ram_gb"]
        assert model["cost_per_1m_tokens"] == entry["cost_per_1m_tokens"]
        assert model["privacy"] == entry["privacy"]
        assert model["quality_note"] == entry["quality_note"]
        assert isinstance(model["store_exists"], bool)
        assert isinstance(model["ready"], bool)
        assert isinstance(model["blockers"], list)

    # required-keys style (Task 10 made the block real and added `pull`);
    # values reflect the hermetic mocks in the env fixture
    ollama = body["ollama"]
    assert {"installed", "running", "models", "pull"} <= set(ollama.keys())
    assert ollama["installed"] is False
    assert ollama["running"] is False
    assert ollama["models"] == []
    assert ollama["pull"] is None


def test_stores_reflect_fixture_store(env, client):
    index_path, stores_dir = env
    _write_index(index_path, ["SNAP-1", "SNAP-2", "SNAP-3", "SNAP-4", "SNAP-5"])
    _populate_store(stores_dir, SLUG, ["SNAP-1", "SNAP-2", "SNAP-3"])

    body = client.get("/embeddings/status").json()

    assert len(body["stores"]) == 1
    store = body["stores"][0]
    assert store["slug"] == SLUG
    assert store["dims"] == DIMS
    assert store["count"] == 3
    assert store["missing"] == 2  # 5 index ids - 3 store ids
    assert isinstance(store["last_updated"], str) and store["last_updated"]


def test_models_missing_math_and_store_exists(env, client):
    index_path, stores_dir = env
    _write_index(index_path, ["SNAP-1", "SNAP-2", "SNAP-3", "SNAP-4", "SNAP-5"])
    _populate_store(stores_dir, SLUG, ["SNAP-1", "SNAP-2", "SNAP-3"])

    models = {m["slug"]: m for m in client.get("/embeddings/status").json()["models"]}

    assert models[SLUG]["store_exists"] is True
    assert models[SLUG]["missing"] == 2
    for slug, model in models.items():
        if slug == SLUG:
            continue
        assert model["store_exists"] is False
        assert model["missing"] is None


# ── schema/rows ops currency (M6e, additive to the binding contract) ─────────

def test_v1_store_reports_schema1_rows_eq_count(env, client):
    """v1 metas carry no schema/rows keys — status derives schema 1 and
    rows == count (one row per snapshot). count stays snapshot currency."""
    index_path, stores_dir = env
    _write_index(index_path, ["SNAP-1", "SNAP-2", "SNAP-3"])
    _populate_store(stores_dir, SLUG, ["SNAP-1", "SNAP-2", "SNAP-3"])

    body = client.get("/embeddings/status").json()

    store = body["stores"][0]
    assert store["schema"] == 1
    assert store["rows"] == store["count"] == 3
    model = next(m for m in body["models"] if m["slug"] == SLUG)
    assert model["schema"] == 1
    assert model["rows"] == 3


def test_v2_store_reports_schema2_rows_and_snapshot_count(env, client):
    """A chunked (schema-2) store reports count in SNAPSHOT currency with the
    raw chunk-row count in rows — count must never inflate with chunking."""
    index_path, stores_dir = env
    _write_index(index_path, ["SNAP-1", "SNAP-2", "SNAP-3"])
    store = get_store(SLUG, base_dir=stores_dir, schema=2)
    rng = np.random.default_rng(7)
    store.append_group("SNAP-1", [rng.standard_normal(DIMS) for _ in range(3)])
    store.append_group("SNAP-2", [rng.standard_normal(DIMS) for _ in range(2)])

    body = client.get("/embeddings/status").json()

    entry = body["stores"][0]
    assert entry["slug"] == SLUG
    assert entry["count"] == 2      # snapshots, NOT rows (binding contract)
    assert entry["schema"] == 2
    assert entry["rows"] == 5       # 3 + 2 chunk rows
    assert entry["missing"] == 1    # snapshot currency too
    model = next(m for m in body["models"] if m["slug"] == SLUG)
    assert model["schema"] == 2
    assert model["rows"] == 5
    assert model["missing"] == 1


def test_model_without_store_has_null_schema_and_rows(env, client):
    index_path, stores_dir = env
    _write_index(index_path, ["SNAP-1"])
    _populate_store(stores_dir, SLUG, ["SNAP-1"])

    models = {m["slug"]: m for m in client.get("/embeddings/status").json()["models"]}

    for slug, model in models.items():
        if slug == SLUG:
            continue
        assert model["store_exists"] is False
        assert model["schema"] is None
        assert model["rows"] is None


# ── models[].strategy label (derived from store schema, for the re-embed UI) ──

def test_strategy_whole_document_for_schema1_store(env, client):
    """A schema-1 (whole-document) store → strategy 'whole_document'; every
    model without a readable store → strategy 'none' (mirrors the schema-null
    convention)."""
    index_path, stores_dir = env
    _write_index(index_path, ["SNAP-1", "SNAP-2", "SNAP-3"])
    _populate_store(stores_dir, SLUG, ["SNAP-1", "SNAP-2", "SNAP-3"])

    models = {m["slug"]: m for m in client.get("/embeddings/status").json()["models"]}

    assert models[SLUG]["schema"] == 1
    assert models[SLUG]["strategy"] == "whole_document"
    for slug, model in models.items():
        if slug == SLUG:
            continue
        assert model["schema"] is None
        assert model["strategy"] == "none"


def test_strategy_chunked_for_schema2_store(env, client):
    """A chunked (schema-2) store → strategy 'chunked', derived from the same
    schema the payload reports."""
    index_path, stores_dir = env
    _write_index(index_path, ["SNAP-1", "SNAP-2", "SNAP-3"])
    store = get_store(SLUG, base_dir=stores_dir, schema=2)
    rng = np.random.default_rng(7)
    store.append_group("SNAP-1", [rng.standard_normal(DIMS) for _ in range(3)])
    store.append_group("SNAP-2", [rng.standard_normal(DIMS) for _ in range(2)])

    model = next(
        m for m in client.get("/embeddings/status").json()["models"]
        if m["slug"] == SLUG
    )
    assert model["schema"] == 2
    assert model["strategy"] == "chunked"


# ── health.json ──────────────────────────────────────────────────────────────

def test_health_present_is_reflected(env, client):
    _, stores_dir = env
    (stores_dir / "health.json").write_text(json.dumps({
        "state": "superseded",
        "detail": "A newer Gemini embedding model is available",
        "successor": "some-newer-slug",
        "successor_slug": "some-newer-slug",
    }), encoding="utf-8")

    body = client.get("/embeddings/status").json()
    assert body["health"] == {
        "state": "superseded",
        "detail": "A newer Gemini embedding model is available",
        "successor": "some-newer-slug",
        "successor_slug": "some-newer-slug",
    }


def test_health_absent_defaults_ok(env, client):
    body = client.get("/embeddings/status").json()
    assert body["health"] == {
        "state": "ok", "detail": "", "successor": None, "successor_slug": None,
    }


def test_health_corrupt_defaults_ok(env, client):
    _, stores_dir = env
    (stores_dir / "health.json").write_text("{not valid json", encoding="utf-8")

    body = client.get("/embeddings/status").json()
    assert body["health"] == {
        "state": "ok", "detail": "", "successor": None, "successor_slug": None,
    }


# ── models[].ready / blockers preflight ──────────────────────────────────────

def _models_by_provider(client):
    models = client.get("/embeddings/status").json()["models"]
    by_provider = {}
    for m in models:
        provider = EMBEDDING_MODELS[m["slug"]]["provider"]
        by_provider.setdefault(provider, []).append(m)
    return by_provider


def test_gemini_ready_with_key_blocked_without(env, client, monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "test-google-key")
    for m in _models_by_provider(client)["gemini"]:
        assert m["ready"] is True and m["blockers"] == []

    monkeypatch.setattr(config, "GOOGLE_API_KEY", "")
    for m in _models_by_provider(client)["gemini"]:
        assert m["ready"] is False
        assert m["blockers"] == ["Add a Google API key in onboarding → API Keys"]


def test_openai_ready_with_key_blocked_without(env, client, monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-openai-key")
    for m in _models_by_provider(client)["openai"]:
        assert m["ready"] is True and m["blockers"] == []

    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    for m in _models_by_provider(client)["openai"]:
        assert m["ready"] is False
        assert m["blockers"] == ["Add an OpenAI API key in onboarding → API Keys"]


def test_ollama_models_blocked_when_not_installed(env, client):
    """env fixture mocks the ollama_io seams: binary absent + daemon down →
    every local model is blocked on the install one-liner. The full blocker
    matrix lives in test_embeddings_ollama.py."""
    ollama_models = _models_by_provider(client)["ollama"]
    assert ollama_models  # registry has local models
    for m in ollama_models:
        assert m["ready"] is False
        assert m["blockers"] == [
            "Install Ollama: curl -fsSL https://ollama.com/install.sh | sh"
        ]


# ── hardware + placement (WI-9/M10, additive to the binding contract) ────────

def test_status_hardware_block_reflects_probe(env, client):
    body = client.get("/embeddings/status").json()
    assert body["hardware"] == NO_GPU_HW


def test_no_gpu_box_recommends_cpu_for_locals_null_for_cloud(env, client):
    """This box's live acceptance shape (audit WI-9): every local model still
    offers a CPU path — recommended, never blocked."""
    models = {m["slug"]: m for m in client.get("/embeddings/status").json()["models"]}
    for slug, m in models.items():
        if EMBEDDING_MODELS[slug]["provider"] == "ollama":
            assert m["recommended_placement"] == "cpu"
            assert m["placement"] is None  # no override persisted → auto
        else:
            assert m["recommended_placement"] is None
            assert m["placement"] is None


@pytest.mark.parametrize("vram_mb,expect_8b,expect_06b", [
    # 16GB (RTX 2000 Ada): both fit with >=1GB headroom → gpu, gpu
    (16380, "gpu", "gpu"),
    # 8GB: qwen-8b needs 6*1024+1024=7168 <= 8192 → gpu; 0.6b trivially fits
    (8192, "gpu", "gpu"),
    # 4GB: 8b doesn't fit (7168 > 4096) → cpu; 0.6b (1024+1024=2048) fits → gpu
    (4096, "cpu", "gpu"),
])
def test_gpu_box_recommendations_follow_vram_budget(
    env, client, monkeypatch, vram_mb, expect_8b, expect_06b
):
    monkeypatch.setattr(hardware, "probe", lambda ttl_s=60.0: {
        "gpu": True, "gpu_name": "NVIDIA Test GPU", "vram_mb": vram_mb,
        "ram_mb": 31167, "source": "nvidia-smi",
    })
    models = {m["slug"]: m for m in client.get("/embeddings/status").json()["models"]}
    assert models["qwen3-embedding-8b"]["recommended_placement"] == expect_8b
    assert models["qwen3-embedding-0.6b"]["recommended_placement"] == expect_06b


def test_gpu_without_known_vram_recommends_cpu(env, client, monkeypatch):
    """lspci-only probe: presence without VRAM — an unverifiable fit is
    recommended against (user can still pin gpu explicitly)."""
    monkeypatch.setattr(hardware, "probe", lambda ttl_s=60.0: {
        "gpu": True, "gpu_name": "NVIDIA Corporation GA104", "vram_mb": None,
        "ram_mb": 31167, "source": "lspci",
    })
    models = {m["slug"]: m for m in client.get("/embeddings/status").json()["models"]}
    assert models["qwen3-embedding-0.6b"]["recommended_placement"] == "cpu"
    assert models["qwen3-embedding-8b"]["recommended_placement"] == "cpu"


def test_hardware_never_adds_blockers(env, client, monkeypatch):
    """The probe is advisory: blockers on a no-GPU box are byte-identical to
    the pre-WI-9 set (install one-liner from the hermetic env fixture)."""
    for m in client.get("/embeddings/status").json()["models"]:
        if EMBEDDING_MODELS[m["slug"]]["provider"] == "ollama":
            assert m["blockers"] == [
                "Install Ollama: curl -fsSL https://ollama.com/install.sh | sh"
            ]


def test_persisted_placement_surfaces_in_status(env, client):
    from Orchestrator.embeddings import store as store_mod
    _, stores_dir = env
    store_mod.set_placement("qwen3-embedding-0.6b", "cpu", base_dir=stores_dir)
    models = {m["slug"]: m for m in client.get("/embeddings/status").json()["models"]}
    assert models["qwen3-embedding-0.6b"]["placement"] == "cpu"
    assert models["qwen3-embedding-8b"]["placement"] is None


# ── cpu_warning advisory + tier steering (reranker-tiering M9, additive) ─────

_LOCAL_SLUGS = [s for s, e in EMBEDDING_MODELS.items() if e["provider"] == "ollama"]
_CLOUD_SLUGS = [s for s, e in EMBEDDING_MODELS.items() if e["provider"] != "ollama"]


def _est_minutes(warning: str) -> float:
    """Minutes represented by the '~N min' / '~N.N hr' estimate in the copy
    (the snapshot count '~N,NNN snapshots' is skipped — it lacks a min/hr unit)."""
    m = re.search(r"~([\d,.]+)\s*(min|hr)", warning)
    assert m, f"no duration estimate found in cpu_warning: {warning!r}"
    value = float(m.group(1).replace(",", ""))
    return value * (60 if m.group(2) == "hr" else 1)


def test_cpu_warning_present_on_local_model_without_gpu(env, client):
    """No GPU (the default env probe) → every LOCAL model carries an advisory
    cpu_warning whose estimate is the live corpus size (len(index_ids)) times a
    per-model CPU rate."""
    from Orchestrator.routes.embeddings_routes import (
        _CPU_SECONDS_PER_GB_SNAPSHOT,
        _fmt_cpu_duration,
    )
    index_path, _ = env
    _write_index(index_path, [f"SNAP-{i}" for i in range(1000)])

    models = {m["slug"]: m for m in client.get("/embeddings/status").json()["models"]}
    for slug in _LOCAL_SLUGS:
        w = models[slug]["cpu_warning"]
        assert isinstance(w, str) and w
        assert "1,000 snapshots" in w  # scaled from the live snapshot index
        # rate = model RAM footprint (ram_gb) × the per-GB factor
        rate = EMBEDDING_MODELS[slug]["ram_gb"] * _CPU_SECONDS_PER_GB_SNAPSHOT
        assert _fmt_cpu_duration(1000 * rate) in w  # count × per-model rate


def test_cpu_warning_scales_with_snapshot_count(env, client):
    """The estimate grows with the corpus — a 10× larger index → a larger
    estimated re-embed for the same model."""
    import Orchestrator.fossils as _fossils
    index_path, _ = env

    _write_index(index_path, [f"SNAP-{i}" for i in range(200)])
    small = {m["slug"]: m for m in client.get("/embeddings/status").json()["models"]}
    _fossils._index_cache = None  # force a reload of the rewritten index
    _write_index(index_path, [f"SNAP-{i}" for i in range(2000)])
    _fossils._index_cache = None
    large = {m["slug"]: m for m in client.get("/embeddings/status").json()["models"]}

    for slug in _LOCAL_SLUGS:
        assert _est_minutes(large[slug]["cpu_warning"]) > _est_minutes(
            small[slug]["cpu_warning"]
        )


def test_cpu_warning_absent_with_gpu(env, client, monkeypatch):
    """A GPU box embeds fine — no cpu_warning on any local model."""
    monkeypatch.setattr(hardware, "probe", lambda ttl_s=60.0: {
        "gpu": True, "gpu_name": "NVIDIA Test GPU", "vram_mb": 16380,
        "ram_mb": 31167, "source": "nvidia-smi", "tier": "HIGH",
    })
    index_path, _ = env
    _write_index(index_path, [f"SNAP-{i}" for i in range(500)])
    models = {m["slug"]: m for m in client.get("/embeddings/status").json()["models"]}
    for slug in _LOCAL_SLUGS:
        assert models[slug]["cpu_warning"] is None


def test_cloud_models_have_no_cpu_warning(env, client):
    """Cloud models (ram_gb 0) embed off-box — never a CPU concern, GPU or not."""
    index_path, _ = env
    _write_index(index_path, [f"SNAP-{i}" for i in range(500)])
    models = {m["slug"]: m for m in client.get("/embeddings/status").json()["models"]}
    for slug in _CLOUD_SLUGS:
        assert models[slug]["cpu_warning"] is None


def test_cpu_warning_never_in_blockers_and_ready_unchanged(env, client, monkeypatch):
    """cpu_warning is ADVISORY: even a fully-available local model (ready True,
    no blockers) still carries it, and it never enters blockers[]. CPU is never
    a dead end (audit WI-9)."""
    monkeypatch.setattr(ollama_io, "binary_installed", lambda: True)
    monkeypatch.setattr(ollama_io, "daemon_version", lambda: "0.1.0")
    monkeypatch.setattr(
        ollama_io, "local_models",
        lambda: [EMBEDDING_MODELS[s]["model_id"] for s in _LOCAL_SLUGS],
    )
    monkeypatch.setattr(ollama_io, "ram_preflight", lambda ram_gb: None)
    index_path, _ = env
    _write_index(index_path, [f"SNAP-{i}" for i in range(500)])

    models = {m["slug"]: m for m in client.get("/embeddings/status").json()["models"]}
    for slug in _LOCAL_SLUGS:
        m = models[slug]
        assert m["ready"] is True          # no GPU did NOT block selection
        assert m["blockers"] == []         # cpu_warning is not a blocker
        assert isinstance(m["cpu_warning"], str) and m["cpu_warning"]
        assert m["cpu_warning"] not in m["blockers"]


def test_low_tier_cloud_steering(env, client):
    """LOW tier (no GPU + <32 GB RAM — the default env) → the local warning also
    carries positive steering toward a cloud model."""
    index_path, _ = env
    _write_index(index_path, [f"SNAP-{i}" for i in range(500)])
    models = {m["slug"]: m for m in client.get("/embeddings/status").json()["models"]}
    for slug in _LOCAL_SLUGS:
        w = models[slug]["cpu_warning"].lower()
        assert "recommended" in w and "cloud" in w


def test_mid_tier_softer_note_not_cloud_steering(env, client, monkeypatch):
    """MID tier (no GPU but >=32 GB RAM) → local is viable; a softer note, NOT
    the LOW 'cloud recommended' steering."""
    monkeypatch.setattr(hardware, "probe", lambda ttl_s=60.0: {
        "gpu": False, "gpu_name": None, "vram_mb": None,
        "ram_mb": 65536, "source": "none", "tier": "MID",
    })
    index_path, _ = env
    _write_index(index_path, [f"SNAP-{i}" for i in range(500)])
    models = {m["slug"]: m for m in client.get("/embeddings/status").json()["models"]}
    for slug in _LOCAL_SLUGS:
        w = models[slug]["cpu_warning"]
        assert isinstance(w, str) and w
        assert "locally" in w.lower()
        assert "recommended" not in w.lower()  # not the LOW steering copy


def test_8b_warning_larger_than_0_6b(env, client):
    """Per-model differentiation: the heavy 8B estimate exceeds the light 0.6B
    for the same corpus."""
    index_path, _ = env
    _write_index(index_path, [f"SNAP-{i}" for i in range(1000)])
    models = {m["slug"]: m for m in client.get("/embeddings/status").json()["models"]}
    assert _est_minutes(models["qwen3-embedding-8b"]["cpu_warning"]) > _est_minutes(
        models["qwen3-embedding-0.6b"]["cpu_warning"]
    )


# ── POST /embeddings/validate ────────────────────────────────────────────────

class _FakeProvider:
    def __init__(self, dims=DIMS, error=None):
        self.dims = dims
        self.error = error
        self.calls = []

    async def embed(self, texts, purpose):
        self.calls.append((texts, purpose))
        if self.error is not None:
            raise self.error
        return [[0.1] * self.dims for _ in texts]


def test_validate_success(env, client, monkeypatch):
    fake = _FakeProvider(dims=DIMS)
    monkeypatch.setitem(providers._instances, SLUG, fake)

    resp = client.post("/embeddings/validate", json={"slug": SLUG})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "dims": DIMS}
    # exactly one probe embed, as a document
    assert fake.calls == [(["probe"], "document")]


def test_validate_provider_failure_is_ok_false_not_500(env, client, monkeypatch):
    fake = _FakeProvider(error=RuntimeError("API key not valid"))
    monkeypatch.setitem(providers._instances, SLUG, fake)

    resp = client.post("/embeddings/validate", json={"slug": SLUG})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "API key not valid" in body["error"]


def test_validate_unknown_slug_404(env, client):
    resp = client.post("/embeddings/validate", json={"slug": "no-such-model"})
    assert resp.status_code == 404


def test_validate_slow_provider_times_out_ok_false(env, client, monkeypatch):
    """A wedged daemon must not hold the wizard click for the provider's full
    retry envelope (~8 min) — the route caps the probe at VALIDATE_TIMEOUT_S."""
    import asyncio

    from Orchestrator.routes import embeddings_routes

    class _HangingProvider:
        async def embed(self, texts, purpose):
            await asyncio.sleep(60)

    monkeypatch.setitem(providers._instances, SLUG, _HangingProvider())
    monkeypatch.setattr(embeddings_routes, "VALIDATE_TIMEOUT_S", 0.2)

    resp = client.post("/embeddings/validate", json={"slug": SLUG})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "timed out" in body["error"]


# ── no side effects ──────────────────────────────────────────────────────────

def test_status_creates_no_dirs_or_files(env, client):
    index_path, stores_dir = env
    _write_index(index_path, ["SNAP-1", "SNAP-2"])
    before = sorted(p.relative_to(stores_dir) for p in stores_dir.rglob("*"))

    assert client.get("/embeddings/status").status_code == 200

    after = sorted(p.relative_to(stores_dir) for p in stores_dir.rglob("*"))
    assert after == before  # status is strictly read-only
