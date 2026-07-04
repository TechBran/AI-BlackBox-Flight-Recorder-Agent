"""POST /rerank/select — the tiered selector endpoint (M8, the backend keystone).

The wizard/Portal drive the reranker through this route: persist a
provider+model+enabled selection (+ an optional pasted api_key) so a choice
takes effect with no restart or config.ini edit. Covers the validation ladder
(provider/model/tier/provider-match), the live key write+os.environ mirror (with
the CRITICAL guarantee that the key is NEVER echoed in the response), the sidecar
write + is_enabled() flip, and the preflight reset. Everything is fixture/mock
based — NO real .env write (update_env spied), NO real API calls (requests
stubbed), NO real hardware probe (tier pinned).
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator import config as _config
from Orchestrator import hardware, rerank
from Orchestrator.embeddings import store
from Orchestrator.onboarding import secrets_writer
from Orchestrator.routes.rerank_routes import router


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point the sidecar at an empty tmp stores dir, reset the preflight caches,
    and hard-disable the network so no test can hit a real reranker API even if a
    key is present in the box's env (the controller keeps a real VOYAGE key)."""
    monkeypatch.setattr(_config, "EMBEDDINGS_STORES_DIR", str(tmp_path / "stores"))
    rerank.reset_preflight()

    def _no_net(*a, **k):
        raise ConnectionError("test: network disabled")

    monkeypatch.setattr(rerank.requests, "get", _no_net)
    monkeypatch.setattr(rerank.requests, "post", _no_net)
    yield
    rerank.reset_preflight()


def _pin_tier(monkeypatch, tier):
    """Pin the hardware tier (both rerank + rerank_routes share the hardware
    module object, so one patch covers the endpoint AND status())."""
    monkeypatch.setattr(
        hardware, "probe",
        lambda *a, **k: {"gpu": tier == "HIGH", "gpu_name": None,
                         "vram_mb": None, "ram_mb": 16000, "source": "test",
                         "tier": tier})


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ── happy path: sidecar written + enabled without a config edit ───────────────

def test_select_persists_sidecar_and_enables(client, monkeypatch):
    """Select voyage enabled=true on an all-tier-eligible box → the rerank.json
    sidecar is written AND rerank.is_enabled() is True with NO config.ini edit."""
    _pin_tier(monkeypatch, "LOW")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)  # keep preflight off-net
    r = client.post("/rerank/select", json={
        "provider": "voyage", "model": "voyage-rerank-2.5", "enabled": True})
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "voyage"
    assert body["model"] == "voyage-rerank-2.5"
    sel = store.get_rerank_selection()
    assert sel == {"enabled": True, "provider": "voyage",
                   "model": "voyage-rerank-2.5"}
    assert rerank.is_enabled() is True


# ── key write → .env + os.environ mirror; NEVER echoed ────────────────────────

def test_select_writes_key_to_env_and_mirrors_os_environ(client, monkeypatch):
    """A pasted api_key is written via secrets_writer.update_env AND mirrored into
    os.environ (live, no restart) — and the response body must NOT contain it."""
    _pin_tier(monkeypatch, "LOW")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(secrets_writer, "update_env",
                        lambda updates: calls.append(updates) or {})
    secret = "sk-SECRET-DO-NOT-ECHO-abc123"
    r = client.post("/rerank/select", json={
        "provider": "voyage", "model": "voyage-rerank-2.5",
        "enabled": True, "api_key": secret})
    assert r.status_code == 200
    # update_env called with EXACTLY {key_env: key}
    assert calls == [{"VOYAGE_API_KEY": secret}]
    # os.environ live-mirrored
    import os
    assert os.environ.get("VOYAGE_API_KEY") == secret
    # CRITICAL: the key is nowhere in the response
    assert secret not in r.text
    assert "api_key" not in r.json()


def test_select_response_omits_api_key(client, monkeypatch):
    """Belt-and-suspenders: even the status payload keys never include api_key and
    the pasted secret never appears in the serialized response."""
    _pin_tier(monkeypatch, "LOW")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setattr(secrets_writer, "update_env", lambda updates: {})
    secret = "sk-NEVER-ECHO-zzz"
    r = client.post("/rerank/select", json={
        "provider": "voyage", "model": "voyage-rerank-2.5",
        "enabled": True, "api_key": secret})
    assert r.status_code == 200
    assert secret not in r.text
    assert not any("key" in k and "present" not in k for k in r.json())


# ── validation ladder ─────────────────────────────────────────────────────────

def test_select_tier_forbidden_model_returns_400(client, monkeypatch):
    """A HIGH-only model (qwen3-reranker-0.6b) on a LOW box → 400 with a clear
    tier message; no sidecar written."""
    _pin_tier(monkeypatch, "LOW")
    r = client.post("/rerank/select", json={
        "provider": "vllm", "model": "qwen3-reranker-0.6b", "enabled": True})
    assert r.status_code == 400
    assert "tier" in r.json()["detail"].lower()
    assert store.get_rerank_selection() is None


def test_select_mid_tier_forbidden_for_high_only(client, monkeypatch):
    """The same HIGH-only model is also forbidden on MID (only HIGH qualifies)."""
    _pin_tier(monkeypatch, "MID")
    r = client.post("/rerank/select", json={
        "provider": "vllm", "model": "qwen3-reranker-0.6b", "enabled": True})
    assert r.status_code == 400


def test_select_provider_model_mismatch_400(client, monkeypatch):
    """provider=cohere with a voyage model → 400 (the entry's provider must match
    the requested provider), preventing a cross-provider selection."""
    _pin_tier(monkeypatch, "HIGH")
    r = client.post("/rerank/select", json={
        "provider": "cohere", "model": "voyage-rerank-2.5", "enabled": True})
    assert r.status_code == 400
    assert store.get_rerank_selection() is None


def test_select_unknown_provider_400(client, monkeypatch):
    _pin_tier(monkeypatch, "HIGH")
    r = client.post("/rerank/select", json={
        "provider": "banana", "model": "voyage-rerank-2.5", "enabled": True})
    assert r.status_code == 400
    assert store.get_rerank_selection() is None


def test_select_unknown_model_404(client, monkeypatch):
    _pin_tier(monkeypatch, "HIGH")
    r = client.post("/rerank/select", json={
        "provider": "voyage", "model": "no-such-reranker", "enabled": True})
    assert r.status_code == 404
    assert store.get_rerank_selection() is None


# ── preflight reset on selection ──────────────────────────────────────────────

def test_select_resets_preflight(client, monkeypatch):
    """The selector resets the preflight caches so the new provider/model
    re-probes (and the CPU model cache is cleared)."""
    _pin_tier(monkeypatch, "LOW")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(rerank, "reset_preflight",
                        lambda: called.append(True))
    r = client.post("/rerank/select", json={
        "provider": "voyage", "model": "voyage-rerank-2.5", "enabled": False})
    assert r.status_code == 200
    assert called == [True]


def test_select_no_key_does_not_write_env(client, monkeypatch):
    """No api_key in the request → update_env is NEVER called (a selection change
    must not touch .env unless a key was actually pasted)."""
    _pin_tier(monkeypatch, "LOW")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(secrets_writer, "update_env",
                        lambda updates: calls.append(updates) or {})
    r = client.post("/rerank/select", json={
        "provider": "voyage", "model": "voyage-rerank-2.5", "enabled": True})
    assert r.status_code == 200
    assert calls == []
