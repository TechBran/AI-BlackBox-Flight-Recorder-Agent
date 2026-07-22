"""M3 (correction [21]/[26]): localstack preflight + status wiring."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator import config, fossils, local_stack
from Orchestrator.embeddings import ollama_io
from Orchestrator.embeddings.store import set_active_slug
from Orchestrator.routes.embeddings_routes import router

LOCALSTACK_SLUG = "qwen3-embedding-8b-local"


@pytest.fixture
def client(tmp_path, monkeypatch):
    index_path = tmp_path / "snapshot_index.json"
    index_path.write_text("{}", encoding="utf-8")
    stores_dir = tmp_path / "embeddings"
    monkeypatch.setattr(fossils, "SNAPSHOT_INDEX", index_path)
    monkeypatch.setattr(fossils, "_index_cache", None)
    monkeypatch.setattr(fossils, "_index_cache_mtime", 0.0)
    monkeypatch.setattr(config, "EMBEDDINGS_STORES_DIR", str(stores_dir))
    set_active_slug("gemini-embedding-001", base_dir=stores_dir)
    monkeypatch.setattr(ollama_io, "binary_installed", lambda: False)
    monkeypatch.setattr(ollama_io, "daemon_version", lambda: None)
    monkeypatch.setattr(ollama_io, "local_models", lambda: [])
    monkeypatch.setattr(ollama_io, "ram_preflight", lambda ram_gb: None)
    # localstack seams default to installed+healthy+downloaded; tests override
    monkeypatch.setattr(local_stack, "is_installed", lambda: True)
    monkeypatch.setattr(local_stack, "is_healthy", lambda: True)
    monkeypatch.setattr(local_stack, "model_downloaded", lambda mid: True)
    monkeypatch.setattr(local_stack, "get_member_ttl", lambda mid: 600)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), monkeypatch


def _model(body, slug):
    return next(m for m in body["models"] if m["slug"] == slug)


def test_localstack_ready_when_installed_healthy_downloaded(client):
    tc, _ = client
    m = _model(tc.get("/embeddings/status").json(), LOCALSTACK_SLUG)
    assert m["ready"] is True and m["blockers"] == []


def test_localstack_blocker_not_installed(client):
    tc, mp = client
    mp.setattr(local_stack, "is_installed", lambda: False)
    m = _model(tc.get("/embeddings/status").json(), LOCALSTACK_SLUG)
    assert m["ready"] is False
    assert any("local stack not installed" in b for b in m["blockers"])


def test_localstack_blocker_service_down(client):
    tc, mp = client
    mp.setattr(local_stack, "is_healthy", lambda: False)
    m = _model(tc.get("/embeddings/status").json(), LOCALSTACK_SLUG)
    assert any("blackbox-models.service" in b for b in m["blockers"])


def test_localstack_blocker_model_not_downloaded(client):
    tc, mp = client
    mp.setattr(local_stack, "model_downloaded", lambda mid: False)
    m = _model(tc.get("/embeddings/status").json(), LOCALSTACK_SLUG)
    # A2: softened copy pointing at the real Download button.
    assert any("not downloaded yet" in b for b in m["blockers"])
    # A2: enrichment — a real Download button needs member_id + downloadable.
    assert m["member_id"] == "embed-qwen3-8b"
    assert m["downloadable"] is True


def test_localstack_active_model_never_not_downloaded(client):
    # A2: the ACTIVE model can NEVER be "not downloaded" — it's serving searches.
    from pathlib import Path as _Path
    tc, mp = client
    # Make the localstack model the ACTIVE slug and report weights absent —
    # the active-implies-downloaded guard must suppress the not-downloaded blocker.
    set_active_slug(LOCALSTACK_SLUG, base_dir=_Path(config.EMBEDDINGS_STORES_DIR))
    mp.setattr(local_stack, "model_downloaded", lambda mid: False)
    body = tc.get("/embeddings/status").json()
    assert body["active"] == LOCALSTACK_SLUG
    m = _model(body, LOCALSTACK_SLUG)
    assert not any("not downloaded" in b for b in m["blockers"])
    assert m["ready"] is True



def test_localstack_status_shows_keep_alive_and_no_placement(client):
    tc, mp = client
    mp.setattr(local_stack, "get_member_ttl", lambda mid: 0)  # warm
    m = _model(tc.get("/embeddings/status").json(), LOCALSTACK_SLUG)
    assert m["warm"] is True                 # is_local now privacy-based
    assert m["placement"] is None            # no runtime placement for on-box
    # on-box devices are install-fixed by tier; no advisory placement hint
    # either (set_placement raises for localstack, so a "cpu"/"gpu" recommendation
    # would point at a toggle the backend rejects).
    assert m["recommended_placement"] is None
