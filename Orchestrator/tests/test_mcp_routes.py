"""M7.1 + M7.2 tests for the MCP onboarding routes (tokens, public-url, status)."""
import json
import stat

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator.routes import mcp_routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = tmp_path / "mcp_tokens.json"
    monkeypatch.setattr(mcp_routes, "MCP_TOKENS_FILE", store)
    monkeypatch.setattr(mcp_routes, "MCP_RUNTIME_FILE", tmp_path / "mcp_runtime.json")
    monkeypatch.setattr(mcp_routes, "USERS_LIST", ["Alice", "Bob"])
    monkeypatch.setattr(mcp_routes, "_trigger_reload", lambda: False)
    monkeypatch.delenv("BLACKBOX_MCP_PUBLIC_URL", raising=False)
    app = FastAPI()
    app.include_router(mcp_routes.router)
    return TestClient(app), store


# ---- M7.1: tokens + connection ----
def test_mint_list_revoke(client):
    c, store = client
    body = c.post("/mcp/tokens", json={"operator": "Alice"}).json()
    assert body["token"].startswith("bbmcp_") and body["operator"] == "Alice"
    tid = body["token_id"]
    assert tid.startswith("sha256:")
    assert oct(stat.S_IMODE(store.stat().st_mode)) == "0o600"
    lst = c.get("/mcp/tokens").json()["tokens"]
    assert lst == [{"token_id": tid, "operator": "Alice"}]
    assert body["token"] not in json.dumps(lst)
    assert c.delete("/mcp/tokens", params={"token_id": tid}).json()["revoked"] == 1
    assert c.get("/mcp/tokens").json()["tokens"] == []


def test_non_roster_operator_rejected(client):
    c, _ = client
    r = c.post("/mcp/tokens", json={"operator": "Brandon"})
    assert r.status_code == 400 and "not a live operator" in r.text


def test_system_operator_rejected(client):
    c, _ = client
    assert c.post("/mcp/tokens", json={"operator": "system"}).status_code == 400


def test_mint_preserves_other_operators(client):
    c, store = client
    c.post("/mcp/tokens", json={"operator": "Alice"})
    c.post("/mcp/tokens", json={"operator": "Bob"})
    data = json.loads(store.read_text())
    assert sorted(data.values()) == ["Alice", "Bob"] and len(data) == 2


def test_connection_scoped(client):
    c, _ = client
    c.post("/mcp/tokens", json={"operator": "Alice"})
    assert c.get("/mcp/connection", params={"operator": "Alice"}).json()["has_token"] is True
    assert c.get("/mcp/connection", params={"operator": "Bob"}).json()["has_token"] is False


# ---- M7.2: public-url + status ----
def test_set_public_url_explicit(client):
    c, _ = client
    r = c.post("/mcp/public-url", json={"url": "https://box.ts.net:8443/"})
    assert r.status_code == 200 and r.json()["public_url"] == "https://box.ts.net:8443"
    assert c.get("/mcp/connection").json()["public_url"] == "https://box.ts.net:8443"


def test_set_public_url_derived(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(mcp_routes, "_derive_public_url", lambda: "https://derived.ts.net:8443")
    r = c.post("/mcp/public-url", json={})
    assert r.status_code == 200 and r.json()["public_url"] == "https://derived.ts.net:8443"


def test_set_public_url_no_derive(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(mcp_routes, "_derive_public_url", lambda: "")
    assert c.post("/mcp/public-url", json={}).status_code == 400


def test_status_signals(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(mcp_routes, "_mcp_up", lambda: True)
    monkeypatch.setattr(mcp_routes, "_funnel_up", lambda: False)
    monkeypatch.setattr(mcp_routes, "_oauth_ready", lambda: False)
    monkeypatch.setattr(mcp_routes, "_derive_public_url", lambda: "https://d.ts.net:8443")
    c.post("/mcp/tokens", json={"operator": "Alice"})
    s = c.get("/mcp/status").json()
    assert s["mcp_up"] is True and s["funnel_up"] is False
    assert s["tokens_present"] is True and s["oauth_ready"] is False
    assert s["derived_public_url"] == "https://d.ts.net:8443"
