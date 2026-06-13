"""Tests for /embeddings/status + /embeddings/validate (Task 7).

The status JSON shape is a BINDING contract (wizard step, Portal card, Android
card in Tasks 13-15) — shape assertions here are deliberate lock-in.

Lightweight pattern: a small FastAPI() with just the embeddings router (the
full app's startup hooks are irrelevant here). All filesystem state lives in
tmp_path fixtures — never the real Manifest/ (same isolation recipe as
test_embeddings_mint.py).
"""
import json

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator import config, fossils
from Orchestrator.embeddings import ollama_io, providers
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import get_store, set_active_slug
from Orchestrator.routes.embeddings_routes import router

SLUG = "gemini-embedding-001"
DIMS = 3072

STATUS_KEYS = {"active", "health", "job", "stores", "models", "ollama"}
MODEL_KEYS = {
    "slug", "label", "dims", "ram_gb", "cost_per_1m_tokens", "privacy",
    "quality_note", "store_exists", "missing", "ready", "blockers",
    "keep_alive", "warm",
}
STORE_KEYS = {"slug", "dims", "count", "missing", "last_updated"}


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
