# Orchestrator/tests/test_voice_agent_routes.py
"""/voice-agents CRUD via TestClient over a minimal app (test_custom_servers_routes pattern)."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator.voice_agents import registry as va
from Orchestrator.routes import voice_agent_routes as var


@pytest.fixture
def tmp_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(va, "REGISTRY_PATH", str(tmp_path / "voice_agents.json"))


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(var.router)
    return TestClient(app)


def test_empty_registry_returns_empty_list(client, tmp_registry):
    r = client.get("/voice-agents")
    assert r.status_code == 200
    assert r.json() == {"agents": []}          # fresh-box gate


def test_crud_roundtrip(client, tmp_registry):
    r = client.post("/voice-agents", json={
        "name": "Pizza Bot", "provider": "grok-live", "voice": "Rex",
        "instructions": "You order pizzas.", "greeting": "Hi!",
        "created_by": "Brandon"})
    assert r.status_code == 200
    agent = r.json()["agent"]
    aid = agent["id"]
    assert agent["provider"] == "grok-live"

    listing = client.get("/voice-agents").json()["agents"]
    assert [a["id"] for a in listing] == [aid]

    # provider filter
    assert client.get("/voice-agents?provider=realtime").json()["agents"] == []
    assert len(client.get("/voice-agents?provider=grok-live").json()["agents"]) == 1

    r = client.patch(f"/voice-agents/{aid}", json={"greeting": "Yo!"})
    assert r.status_code == 200
    assert r.json()["agent"]["greeting"] == "Yo!"

    assert client.delete(f"/voice-agents/{aid}").status_code == 200
    assert client.get("/voice-agents").json()["agents"] == []


def test_post_unknown_provider_400(client, tmp_registry):
    r = client.post("/voice-agents", json={"name": "x", "provider": "alexa"})
    assert r.status_code == 400


def test_post_model_validated_against_catalog(client, tmp_registry):
    # realtime + gemini-live have config catalogs — a junk model must 400.
    r = client.post("/voice-agents", json={
        "name": "x", "provider": "realtime", "model": "gpt-6-realtime-fake"})
    assert r.status_code == 400
    assert "model" in r.json()["detail"].lower()
    # a real catalog id is accepted
    from Orchestrator.config import OPENAI_REALTIME_MODELS
    good = OPENAI_REALTIME_MODELS[0]["id"]
    assert client.post("/voice-agents", json={
        "name": "y", "provider": "realtime", "model": good}).status_code == 200


def test_post_oversized_instructions_400(client, tmp_registry):
    r = client.post("/voice-agents", json={
        "name": "big", "provider": "realtime",
        "instructions": "x" * (va.INSTRUCTIONS_MAX_CHARS + 1)})
    assert r.status_code == 400


def test_patch_unknown_id_404_and_delete_unknown_404(client, tmp_registry):
    assert client.patch("/voice-agents/va-nope", json={"name": "x"}).status_code == 404
    assert client.delete("/voice-agents/va-nope").status_code == 404


def test_patch_model_revalidated_against_stored_provider(client, tmp_registry):
    aid = client.post("/voice-agents", json={
        "name": "x", "provider": "realtime"}).json()["agent"]["id"]
    r = client.patch(f"/voice-agents/{aid}", json={"model": "not-a-model"})
    assert r.status_code == 400
