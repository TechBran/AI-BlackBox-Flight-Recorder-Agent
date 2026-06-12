"""Tests for the Ollama integration (Task 10).

Covers ollama_io (daemon probes, NDJSON pull-stream parsing, RAM preflight)
and the route layer (preflight blocker matrix in /embeddings/status models[],
POST /embeddings/ollama/pull, the status `ollama` block incl. `pull`).

All HTTP is mocked via httpx.MockTransport injected through the module seams
(ollama_io._transport for the sync GETs, ollama_io._async_transport for the
pull stream — providers.py's _transport pattern). psutil via monkeypatch.
"""
import asyncio
import json
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator import config, fossils
from Orchestrator.embeddings import ollama_io
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import set_active_slug
from Orchestrator.routes.embeddings_routes import router

LIGHT = "qwen3-embedding-0.6b"               # registry slug
LIGHT_ID = EMBEDDING_MODELS[LIGHT]["model_id"]   # "qwen3-embedding:0.6b"
HEAVY = "qwen3-embedding-8b"

INSTALL_BLOCKER = "Install Ollama: curl -fsSL https://ollama.com/install.sh | sh"
START_BLOCKER = "Start it: sudo systemctl start ollama"


@pytest.fixture(autouse=True)
def reset_pull_state():
    """The pull singleton is module state — every test starts and ends idle."""
    ollama_io._PULL = None
    ollama_io._PULL_TASK = None
    yield
    ollama_io._PULL = None
    ollama_io._PULL_TASK = None


# ── helpers ──────────────────────────────────────────────────────────────────

def _get_transport(routes: dict):
    """Sync MockTransport: path → httpx.Response (or raising callable)."""
    def handler(request):
        target = routes.get(request.url.path)
        if callable(target):
            return target(request)
        if target is None:
            return httpx.Response(404)
        return target
    return httpx.MockTransport(handler)


def _ndjson_transport(lines, request_log=None):
    """Async MockTransport streaming the given dicts as NDJSON pull lines.
    A fresh generator per request — async generators are single-use."""
    def handler(request):
        if request_log is not None:
            request_log.append(json.loads(request.read()))

        async def gen():
            for line in lines:
                yield (json.dumps(line) + "\n").encode()

        return httpx.Response(200, content=gen())
    return httpx.MockTransport(handler)


async def _pull_and_wait(model_id: str) -> dict:
    state = await ollama_io.start_pull(model_id)
    assert state["state"] == "running"  # claim snapshot, pre-stream
    await ollama_io._PULL_TASK
    return ollama_io.pull_status()


class _VM:
    def __init__(self, available):
        self.available = available


def _mock_ollama(monkeypatch, installed=False, running=False, models=(),
                 available_ram=None):
    """Patch the ollama_io seams the routes consume."""
    monkeypatch.setattr(ollama_io, "binary_installed", lambda: installed)
    monkeypatch.setattr(
        ollama_io, "daemon_version", lambda: "0.5.0" if running else None
    )
    monkeypatch.setattr(ollama_io, "local_models", lambda: list(models))
    if available_ram is None:
        monkeypatch.setattr(ollama_io, "ram_preflight", lambda ram_gb: None)
    else:
        monkeypatch.setattr(
            ollama_io.psutil, "virtual_memory", lambda: _VM(available_ram)
        )


# ── daemon probes (sync GETs) ────────────────────────────────────────────────

def test_daemon_version_happy(monkeypatch):
    monkeypatch.setattr(ollama_io, "_transport", _get_transport({
        "/api/version": httpx.Response(200, json={"version": "0.5.1"}),
    }))
    assert ollama_io.daemon_version() == "0.5.1"


def test_daemon_version_unreachable_is_none(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(ollama_io, "_transport", _get_transport({
        "/api/version": refuse,
    }))
    assert ollama_io.daemon_version() is None


def test_daemon_version_http_error_is_none(monkeypatch):
    monkeypatch.setattr(ollama_io, "_transport", _get_transport({
        "/api/version": httpx.Response(500, text="boom"),
    }))
    assert ollama_io.daemon_version() is None


def test_local_models_happy(monkeypatch):
    monkeypatch.setattr(ollama_io, "_transport", _get_transport({
        "/api/tags": httpx.Response(200, json={"models": [
            {"name": "qwen3-embedding:0.6b", "size": 639},
            {"name": "llama3.2:latest", "size": 2048},
        ]}),
    }))
    assert ollama_io.local_models() == ["qwen3-embedding:0.6b", "llama3.2:latest"]


def test_local_models_unreachable_is_empty(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(ollama_io, "_transport", _get_transport({
        "/api/tags": refuse,
    }))
    assert ollama_io.local_models() == []


def test_binary_installed_both_ways(monkeypatch):
    monkeypatch.setattr(ollama_io.shutil, "which", lambda name: "/usr/bin/ollama")
    assert ollama_io.binary_installed() is True
    monkeypatch.setattr(ollama_io.shutil, "which", lambda name: None)
    assert ollama_io.binary_installed() is False


# ── NDJSON pull stream parsing ───────────────────────────────────────────────

def test_pull_stream_happy_largest_layer_progress(monkeypatch):
    request_log = []
    monkeypatch.setattr(ollama_io, "_async_transport", _ndjson_transport([
        {"status": "pulling manifest"},
        {"status": "pulling sha256:big", "total": 1000, "completed": 100},
        {"status": "pulling sha256:big", "total": 1000, "completed": 1000},
        # a smaller layer must NOT clobber the big layer's numbers
        {"status": "pulling sha256:small", "total": 10, "completed": 10},
        {"status": "verifying sha256 digest"},
        {"status": "success"},
    ], request_log))

    final = asyncio.run(_pull_and_wait(LIGHT_ID))

    assert final["state"] == "done"
    assert final["status"] == "success"
    assert final["model"] == LIGHT_ID
    assert final["total"] == 1000
    assert final["completed"] == 1000
    assert final["error"] is None
    assert request_log == [{"name": LIGHT_ID, "stream": True}]


def test_pull_progress_updates_mid_stream(monkeypatch):
    """completed/total advance line-by-line while the stream runs (the wizard
    polls this through /embeddings/status)."""
    seen = []

    def handler(request):
        async def gen():
            yield b'{"status": "pulling manifest"}\n'
            yield b'{"status": "pulling sha256:big", "total": 1000, "completed": 100}\n'
            # runs after the line above has been parsed (single-task ordering)
            seen.append(ollama_io.pull_status())
            yield b'{"status": "success"}\n'
        return httpx.Response(200, content=gen())

    monkeypatch.setattr(
        ollama_io, "_async_transport", httpx.MockTransport(handler)
    )
    final = asyncio.run(_pull_and_wait(LIGHT_ID))

    assert seen[0]["state"] == "running"
    assert seen[0]["completed"] == 100
    assert seen[0]["total"] == 1000
    assert final["state"] == "done"


def test_pull_error_line_sets_error_state(monkeypatch):
    monkeypatch.setattr(ollama_io, "_async_transport", _ndjson_transport([
        {"status": "pulling manifest"},
        {"error": "pull model manifest: file does not exist"},
    ]))
    final = asyncio.run(_pull_and_wait(LIGHT_ID))
    assert final["state"] == "error"
    assert "file does not exist" in final["error"]


def test_pull_connection_drop_mid_stream_sets_error(monkeypatch):
    def handler(request):
        async def gen():
            yield b'{"status": "pulling manifest"}\n'
            yield b'{"status": "pulling sha256:big", "total": 1000, "completed": 50}\n'
            raise RuntimeError("connection dropped mid-stream")
        return httpx.Response(200, content=gen())

    monkeypatch.setattr(
        ollama_io, "_async_transport", httpx.MockTransport(handler)
    )
    final = asyncio.run(_pull_and_wait(LIGHT_ID))
    assert final["state"] == "error"
    assert "dropped" in final["error"]


def test_pull_daemon_unreachable_sets_error(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(
        ollama_io, "_async_transport", httpx.MockTransport(refuse)
    )
    final = asyncio.run(_pull_and_wait(LIGHT_ID))
    assert final["state"] == "error"
    assert "refused" in final["error"]


def test_pull_stream_ends_without_success_is_error(monkeypatch):
    monkeypatch.setattr(ollama_io, "_async_transport", _ndjson_transport([
        {"status": "pulling manifest"},
        {"status": "pulling sha256:big", "total": 1000, "completed": 500},
    ]))
    final = asyncio.run(_pull_and_wait(LIGHT_ID))
    assert final["state"] == "error"
    assert "without a success line" in final["error"]


def test_second_pull_while_running_raises(monkeypatch):
    ollama_io._PULL = {
        "model": LIGHT_ID, "status": "pulling sha256:big", "completed": 1,
        "total": 10, "state": "running", "error": None,
    }
    with pytest.raises(RuntimeError, match="already running"):
        asyncio.run(ollama_io.start_pull("anything:else"))


def test_new_pull_allowed_after_done(monkeypatch):
    ollama_io._PULL = {
        "model": LIGHT_ID, "status": "success", "completed": 10,
        "total": 10, "state": "done", "error": None,
    }
    monkeypatch.setattr(ollama_io, "_async_transport", _ndjson_transport([
        {"status": "success"},
    ]))
    final = asyncio.run(_pull_and_wait(LIGHT_ID))
    assert final["state"] == "done"


def test_pull_status_none_when_idle():
    assert ollama_io.pull_status() is None


# ── RAM preflight ────────────────────────────────────────────────────────────

def test_ram_preflight_short(monkeypatch):
    monkeypatch.setattr(
        ollama_io.psutil, "virtual_memory", lambda: _VM(int(0.5e9))
    )
    assert ollama_io.ram_preflight(6.0) == (
        "Needs ~6GB free RAM; close apps or pick the lighter model"
    )
    assert ollama_io.ram_preflight(1.0) == (
        "Needs ~1GB free RAM; close apps or pick the lighter model"
    )


def test_ram_preflight_enough(monkeypatch):
    monkeypatch.setattr(
        ollama_io.psutil, "virtual_memory", lambda: _VM(int(8e9))
    )
    assert ollama_io.ram_preflight(6.0) is None


def test_ram_preflight_boundary(monkeypatch):
    # threshold is ram_gb * RAM_BYTES_PER_GB: just under blocks, exact passes
    threshold = int(1.0 * ollama_io.RAM_BYTES_PER_GB)
    monkeypatch.setattr(
        ollama_io.psutil, "virtual_memory", lambda: _VM(threshold - 1)
    )
    assert ollama_io.ram_preflight(1.0) is not None
    monkeypatch.setattr(
        ollama_io.psutil, "virtual_memory", lambda: _VM(threshold)
    )
    assert ollama_io.ram_preflight(1.0) is None


def test_ram_preflight_zero_is_none_for_cloud():
    assert ollama_io.ram_preflight(0.0) is None


# ── route fixtures (same isolation recipe as test_embeddings_routes) ─────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    index_path = tmp_path / "snapshot_index.json"
    stores_dir = tmp_path / "embeddings"
    monkeypatch.setattr(fossils, "SNAPSHOT_INDEX", index_path)
    monkeypatch.setattr(fossils, "_index_cache", None)
    monkeypatch.setattr(fossils, "_index_cache_mtime", 0.0)
    monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(stores_dir))
    set_active_slug("gemini-embedding-001", base_dir=stores_dir)
    return index_path, stores_dir


@pytest.fixture
def client(env):
    app = FastAPI()
    app.include_router(router)
    # Context-managed: one persistent portal/event loop across requests, so a
    # background pull task started by one request keeps streaming while the
    # test polls /embeddings/status with subsequent requests.
    with TestClient(app) as c:
        yield c


def _local_models_by_slug(client):
    models = client.get("/embeddings/status").json()["models"]
    return {
        m["slug"]: m for m in models
        if EMBEDDING_MODELS[m["slug"]]["provider"] == "ollama"
    }


# ── preflight blocker matrix (via /embeddings/status models[]) ───────────────

def test_preflight_not_installed_daemon_down(env, client, monkeypatch):
    _mock_ollama(monkeypatch, installed=False, running=False)
    for m in _local_models_by_slug(client).values():
        assert m["ready"] is False
        assert m["blockers"] == [INSTALL_BLOCKER]


def test_preflight_installed_daemon_down(env, client, monkeypatch):
    _mock_ollama(monkeypatch, installed=True, running=False)
    for m in _local_models_by_slug(client).values():
        assert m["ready"] is False
        assert m["blockers"] == [START_BLOCKER]


def test_preflight_up_model_missing(env, client, monkeypatch):
    _mock_ollama(monkeypatch, installed=True, running=True, models=[])
    by_slug = _local_models_by_slug(client)
    # ram_gb doubles as the download-size estimate (no size field in registry)
    assert by_slug[LIGHT]["blockers"] == [
        "Pull the model from the setup wizard (≈1 GB download)"
    ]
    assert by_slug[LIGHT]["ready"] is False
    assert by_slug[HEAVY]["blockers"] == [
        "Pull the model from the setup wizard (≈6 GB download)"
    ]
    assert by_slug[HEAVY]["ready"] is False


def test_preflight_up_model_present_ram_short(env, client, monkeypatch):
    _mock_ollama(
        monkeypatch, installed=True, running=True,
        models=[LIGHT_ID, EMBEDDING_MODELS[HEAVY]["model_id"]],
        available_ram=int(0.5e9),  # < 1GB needed for the light model
    )
    by_slug = _local_models_by_slug(client)
    assert by_slug[LIGHT]["ready"] is False
    assert by_slug[LIGHT]["blockers"] == [
        "Needs ~1GB free RAM; close apps or pick the lighter model"
    ]


def test_preflight_up_model_missing_and_ram_short_reports_both(
    env, client, monkeypatch
):
    """All applicable blockers are reported as a list, not just the first."""
    _mock_ollama(
        monkeypatch, installed=True, running=True, models=[],
        available_ram=int(0.5e9),
    )
    light = _local_models_by_slug(client)[LIGHT]
    assert light["ready"] is False
    assert light["blockers"] == [
        "Pull the model from the setup wizard (≈1 GB download)",
        "Needs ~1GB free RAM; close apps or pick the lighter model",
    ]


def test_preflight_all_green(env, client, monkeypatch):
    _mock_ollama(
        monkeypatch, installed=True, running=True,
        models=[LIGHT_ID, EMBEDDING_MODELS[HEAVY]["model_id"]],
        available_ram=int(64e9),
    )
    for m in _local_models_by_slug(client).values():
        assert m["ready"] is True
        assert m["blockers"] == []


# ── status `ollama` block ────────────────────────────────────────────────────

def test_status_ollama_block_shape(env, client, monkeypatch):
    _mock_ollama(monkeypatch, installed=True, running=True, models=[LIGHT_ID])
    ollama = client.get("/embeddings/status").json()["ollama"]
    assert {"installed", "running", "models", "pull"} <= set(ollama.keys())
    assert ollama["installed"] is True
    assert ollama["running"] is True
    assert ollama["models"] == [LIGHT_ID]
    assert ollama["pull"] is None  # idle


def test_status_ollama_models_empty_when_daemon_down(env, client, monkeypatch):
    """tags are never fetched (and never partial-stale) with the daemon down."""
    _mock_ollama(monkeypatch, installed=True, running=False, models=[LIGHT_ID])
    ollama = client.get("/embeddings/status").json()["ollama"]
    assert ollama["running"] is False
    assert ollama["models"] == []


# ── POST /embeddings/ollama/pull ─────────────────────────────────────────────

def test_pull_route_unknown_slug_404(env, client):
    resp = client.post("/embeddings/ollama/pull", json={"model": "no-such-model"})
    assert resp.status_code == 404


def test_pull_route_non_ollama_slug_400(env, client):
    resp = client.post(
        "/embeddings/ollama/pull", json={"model": "gemini-embedding-001"}
    )
    assert resp.status_code == 400
    assert "not an Ollama model" in resp.json()["detail"]


def test_pull_route_409_while_running(env, client):
    ollama_io._PULL = {
        "model": LIGHT_ID, "status": "pulling sha256:big", "completed": 1,
        "total": 10, "state": "running", "error": None,
    }
    resp = client.post("/embeddings/ollama/pull", json={"model": LIGHT})
    assert resp.status_code == 409
    assert "already running" in resp.json()["detail"]


def test_pull_route_starts_and_status_surfaces_progress(env, client, monkeypatch):
    """Slug in → raw model id resolved → 200 with the claimed state; the
    background stream then completes and shows up in status as ollama.pull."""
    request_log = []
    monkeypatch.setattr(ollama_io, "_async_transport", _ndjson_transport([
        {"status": "pulling manifest"},
        {"status": "pulling sha256:big", "total": 1000, "completed": 1000},
        {"status": "success"},
    ], request_log))
    _mock_ollama(monkeypatch, installed=True, running=True, models=[])

    resp = client.post("/embeddings/ollama/pull", json={"model": LIGHT})
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == LIGHT_ID  # registry slug resolved to raw id
    assert body["state"] == "running"

    pull = None
    for _ in range(200):
        pull = client.get("/embeddings/status").json()["ollama"]["pull"]
        if pull is not None and pull["state"] != "running":
            break
        time.sleep(0.02)
    assert pull["state"] == "done"
    assert pull["total"] == 1000
    assert pull["completed"] == 1000
    assert request_log == [{"name": LIGHT_ID, "stream": True}]
