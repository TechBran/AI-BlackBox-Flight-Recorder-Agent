"""Per-box device placement for local models (WI-9, M10 task 10.2).

Covers the store accessors (placement.json beside the stores, keep_alive.json's
sibling), the OllamaProvider enforcement (placement=="cpu" → options.num_gpu: 0
on the wire, with num_ctx still present on BOTH placements — audit WI-9), and
the POST /embeddings/placement endpoint. All Ollama HTTP is mocked; nothing
touches a real daemon or Manifest/. Mirrors test_embeddings_keep_alive.py.
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

def test_get_placement_defaults_to_none_auto(stores_dir):
    assert store.get_placement(LOCAL_SLUG, base_dir=stores_dir) is None


def test_set_placement_roundtrip_and_clear(stores_dir):
    assert store.set_placement(LOCAL_SLUG, "cpu", base_dir=stores_dir) == "cpu"
    assert store.get_placement(LOCAL_SLUG, base_dir=stores_dir) == "cpu"

    assert store.set_placement(LOCAL_SLUG, "gpu", base_dir=stores_dir) == "gpu"
    assert store.get_placement(LOCAL_SLUG, base_dir=stores_dir) == "gpu"

    # None clears back to auto — the slug's key is REMOVED, not stored as null
    assert store.set_placement(LOCAL_SLUG, None, base_dir=stores_dir) is None
    assert store.get_placement(LOCAL_SLUG, base_dir=stores_dir) is None
    data = json.loads((stores_dir / store.PLACEMENT_FILE).read_text())
    assert LOCAL_SLUG not in data


def test_placement_is_per_slug_atomic_json(stores_dir):
    store.set_placement(LOCAL_SLUG, "cpu", base_dir=stores_dir)
    store.set_placement("qwen3-embedding-8b", "gpu", base_dir=stores_dir)
    data = json.loads((stores_dir / store.PLACEMENT_FILE).read_text())
    assert data == {LOCAL_SLUG: "cpu", "qwen3-embedding-8b": "gpu"}


def test_set_placement_rejects_cloud_model(stores_dir):
    with pytest.raises(ValueError, match="not a local model"):
        store.set_placement(CLOUD_SLUG, "cpu", base_dir=stores_dir)


def test_set_placement_rejects_unknown_slug(stores_dir):
    with pytest.raises(ValueError, match="unknown"):
        store.set_placement("no-such-model", "cpu", base_dir=stores_dir)


def test_set_placement_rejects_bad_value(stores_dir):
    with pytest.raises(ValueError, match="placement must be"):
        store.set_placement(LOCAL_SLUG, "tpu", base_dir=stores_dir)


def test_get_placement_corrupt_file_reads_auto(stores_dir):
    stores_dir.mkdir(parents=True, exist_ok=True)
    (stores_dir / store.PLACEMENT_FILE).write_text("{not json", encoding="utf-8")
    assert store.get_placement(LOCAL_SLUG, base_dir=stores_dir) is None


def test_get_placement_unknown_value_reads_auto(stores_dir):
    """A hand-edited/garbage value fails open to auto, never to a surprise
    CPU pin."""
    stores_dir.mkdir(parents=True, exist_ok=True)
    (stores_dir / store.PLACEMENT_FILE).write_text(
        json.dumps({LOCAL_SLUG: "banana"}), encoding="utf-8"
    )
    assert store.get_placement(LOCAL_SLUG, base_dir=stores_dir) is None


# ── provider enforcement (the wire: options.num_gpu) ─────────────────────────

def _fresh_provider_with_capture(seen):
    import httpx

    providers._instances.pop(LOCAL_SLUG, None)
    provider = providers.get_provider(LOCAL_SLUG)

    def handler(request):
        seen.append(json.loads(request.content))
        dims = EMBEDDING_MODELS[LOCAL_SLUG]["dims"]
        return httpx.Response(200, json={"embeddings": [[0.1] * dims]})

    provider._transport = httpx.MockTransport(handler)
    return provider


@pytest.mark.asyncio
async def test_cpu_placement_pins_num_gpu_zero_and_keeps_num_ctx(stores_dir):
    """placement=="cpu" → options.num_gpu: 0 AND num_ctx STILL present:
    Ollama's VRAM-tiered default ctx changes with the device, so the explicit
    num_ctx must ride on BOTH placements (audit WI-9 critical note)."""
    seen = []
    provider = _fresh_provider_with_capture(seen)

    store.set_placement(LOCAL_SLUG, "cpu", base_dir=stores_dir)
    await provider.embed(["x"], "document")
    options = seen[-1]["options"]
    assert options["num_gpu"] == 0
    assert options["num_ctx"] == EMBEDDING_MODELS[LOCAL_SLUG]["max_input_tokens"]


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["gpu", None])
async def test_gpu_or_auto_placement_omits_num_gpu(stores_dir, placement):
    """"gpu"/auto omit num_gpu entirely — Ollama auto-offloads when a GPU
    exists; sending a count would hardcode a layer split."""
    seen = []
    provider = _fresh_provider_with_capture(seen)

    store.set_placement(LOCAL_SLUG, placement, base_dir=stores_dir)
    await provider.embed(["x"], "document")
    options = seen[-1]["options"]
    assert "num_gpu" not in options
    assert options["num_ctx"] == EMBEDDING_MODELS[LOCAL_SLUG]["max_input_tokens"]


@pytest.mark.asyncio
async def test_placement_toggle_applies_next_call_no_restart(stores_dir):
    """The provider reads placement.json fresh per call — flip mid-life and
    the very next embed carries the change (the no-restart acceptance)."""
    seen = []
    provider = _fresh_provider_with_capture(seen)

    store.set_placement(LOCAL_SLUG, "cpu", base_dir=stores_dir)
    await provider.embed(["x"], "document")
    assert seen[-1]["options"]["num_gpu"] == 0

    store.set_placement(LOCAL_SLUG, None, base_dir=stores_dir)
    await provider.embed(["x"], "document")
    assert "num_gpu" not in seen[-1]["options"]


# ── endpoint ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client(stores_dir):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_endpoint_sets_cpu_and_persists(client, stores_dir):
    r = client.post("/embeddings/placement",
                    json={"slug": LOCAL_SLUG, "placement": "cpu"})
    assert r.status_code == 200
    assert r.json() == {"slug": LOCAL_SLUG, "placement": "cpu"}
    assert store.get_placement(LOCAL_SLUG, base_dir=stores_dir) == "cpu"


def test_endpoint_null_clears_to_auto(client, stores_dir):
    store.set_placement(LOCAL_SLUG, "cpu", base_dir=stores_dir)
    r = client.post("/embeddings/placement",
                    json={"slug": LOCAL_SLUG, "placement": None})
    assert r.status_code == 200
    assert r.json() == {"slug": LOCAL_SLUG, "placement": None}
    assert store.get_placement(LOCAL_SLUG, base_dir=stores_dir) is None


def test_endpoint_404_unknown_slug(client):
    r = client.post("/embeddings/placement",
                    json={"slug": "nope", "placement": "cpu"})
    assert r.status_code == 404


def test_endpoint_400_cloud_model(client):
    r = client.post("/embeddings/placement",
                    json={"slug": CLOUD_SLUG, "placement": "cpu"})
    assert r.status_code == 400


def test_endpoint_400_bad_value(client):
    r = client.post("/embeddings/placement",
                    json={"slug": LOCAL_SLUG, "placement": "tpu"})
    assert r.status_code == 400
    assert "placement must be" in r.json()["detail"]
