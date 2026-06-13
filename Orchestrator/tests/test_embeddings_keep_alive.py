"""Per-box keep_alive override for local models (wizard warm/cold toggle).

Covers the store accessors, the OllamaProvider read-through (a live override
beats the registry default with no restart), and the POST /embeddings/keep_alive
endpoint. All Ollama HTTP is mocked; nothing touches a real daemon or Manifest/.
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator import config
from Orchestrator.embeddings import providers, store
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.routes.embeddings_routes import router

LOCAL_SLUG = "qwen3-embedding-0.6b"
CLOUD_SLUG = "gemini-embedding-001"


@pytest.fixture
def stores_dir(tmp_path, monkeypatch):
    d = tmp_path / "embeddings"
    monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(d))
    return d


# ── store accessors ──────────────────────────────────────────────────────────

def test_get_keep_alive_defaults_to_registry(stores_dir):
    # no override file → registry default for the model
    assert store.get_keep_alive(LOCAL_SLUG, base_dir=stores_dir) == \
        EMBEDDING_MODELS[LOCAL_SLUG]["keep_alive"]


def test_set_keep_alive_warm_then_cold_roundtrip(stores_dir):
    assert store.set_keep_alive(LOCAL_SLUG, warm=True, base_dir=stores_dir) == \
        store.KEEP_ALIVE_WARM
    assert store.get_keep_alive(LOCAL_SLUG, base_dir=stores_dir) == store.KEEP_ALIVE_WARM
    assert store.is_warm(store.get_keep_alive(LOCAL_SLUG, base_dir=stores_dir))

    assert store.set_keep_alive(LOCAL_SLUG, warm=False, base_dir=stores_dir) == \
        store.KEEP_ALIVE_COLD
    assert store.get_keep_alive(LOCAL_SLUG, base_dir=stores_dir) == store.KEEP_ALIVE_COLD
    assert not store.is_warm(store.get_keep_alive(LOCAL_SLUG, base_dir=stores_dir))


def test_override_is_per_slug_atomic_json(stores_dir):
    store.set_keep_alive(LOCAL_SLUG, warm=True, base_dir=stores_dir)
    store.set_keep_alive("qwen3-embedding-8b", warm=False, base_dir=stores_dir)
    data = json.loads((stores_dir / store.KEEP_ALIVE_FILE).read_text())
    assert data == {LOCAL_SLUG: store.KEEP_ALIVE_WARM,
                    "qwen3-embedding-8b": store.KEEP_ALIVE_COLD}


def test_set_keep_alive_rejects_cloud_model(stores_dir):
    with pytest.raises(ValueError, match="not a local model"):
        store.set_keep_alive(CLOUD_SLUG, warm=True, base_dir=stores_dir)


def test_set_keep_alive_rejects_unknown_slug(stores_dir):
    with pytest.raises(ValueError, match="unknown"):
        store.set_keep_alive("no-such-model", warm=True, base_dir=stores_dir)


def test_get_keep_alive_corrupt_file_falls_back(stores_dir):
    stores_dir.mkdir(parents=True, exist_ok=True)
    (stores_dir / store.KEEP_ALIVE_FILE).write_text("{not json", encoding="utf-8")
    assert store.get_keep_alive(LOCAL_SLUG, base_dir=stores_dir) == \
        EMBEDDING_MODELS[LOCAL_SLUG]["keep_alive"]


def test_get_keep_alive_synthetic_slug_uses_fallback(stores_dir):
    assert store.get_keep_alive("synthetic", base_dir=stores_dir, fallback="9m") == "9m"


# ── provider read-through (live override, no restart) ────────────────────────

def _ollama_capture(provider, seen, dims):
    import httpx

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"embeddings": [[0.1] * dims]})

    provider._transport = httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_provider_sends_overridden_keep_alive(stores_dir, monkeypatch):
    monkeypatch.setitem(providers._instances, LOCAL_SLUG, None)
    providers._instances.pop(LOCAL_SLUG, None)
    provider = providers.get_provider(LOCAL_SLUG)
    seen = []
    _ollama_capture(provider, seen, EMBEDDING_MODELS[LOCAL_SLUG]["dims"])

    store.set_keep_alive(LOCAL_SLUG, warm=False, base_dir=stores_dir)
    await provider.embed(["x"], "document")
    assert seen[-1]["keep_alive"] == store.KEEP_ALIVE_COLD

    # flip the override mid-life; the very next embed carries the new value
    store.set_keep_alive(LOCAL_SLUG, warm=True, base_dir=stores_dir)
    await provider.embed(["x"], "document")
    assert seen[-1]["keep_alive"] == store.KEEP_ALIVE_WARM


# ── endpoint ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client(stores_dir, monkeypatch):
    # block the warm-up embed from hitting a real provider
    async def _noop_warmup(slug):
        return None
    monkeypatch.setattr(
        "Orchestrator.routes.embeddings_routes._warmup_model", _noop_warmup
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_endpoint_sets_warm_and_reports(client, stores_dir):
    r = client.post("/embeddings/keep_alive", json={"slug": LOCAL_SLUG, "warm": True})
    assert r.status_code == 200
    assert r.json() == {"slug": LOCAL_SLUG, "warm": True,
                        "keep_alive": store.KEEP_ALIVE_WARM}
    assert store.get_keep_alive(LOCAL_SLUG, base_dir=stores_dir) == store.KEEP_ALIVE_WARM


def test_endpoint_sets_cold(client, stores_dir):
    r = client.post("/embeddings/keep_alive", json={"slug": LOCAL_SLUG, "warm": False})
    assert r.status_code == 200
    assert r.json()["keep_alive"] == store.KEEP_ALIVE_COLD


def test_endpoint_404_unknown_slug(client):
    r = client.post("/embeddings/keep_alive", json={"slug": "nope", "warm": True})
    assert r.status_code == 404


def test_endpoint_400_cloud_model(client):
    r = client.post("/embeddings/keep_alive", json={"slug": CLOUD_SLUG, "warm": True})
    assert r.status_code == 400


def test_status_exposes_keep_alive_and_warm(client, stores_dir, monkeypatch):
    import Orchestrator.fossils as fossils
    idx = stores_dir.parent / "snapshot_index.json"
    idx.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(fossils, "SNAPSHOT_INDEX", idx)
    monkeypatch.setattr(fossils, "_index_cache", None)
    monkeypatch.setattr(fossils, "_index_cache_mtime", 0.0)

    store.set_keep_alive(LOCAL_SLUG, warm=True, base_dir=stores_dir)
    models = {m["slug"]: m for m in client.get("/embeddings/status").json()["models"]}

    assert models[LOCAL_SLUG]["keep_alive"] == store.KEEP_ALIVE_WARM
    assert models[LOCAL_SLUG]["warm"] is True
    # cloud model: keep_alive concept does not apply
    assert models[CLOUD_SLUG]["keep_alive"] is None
    assert models[CLOUD_SLUG]["warm"] is None
