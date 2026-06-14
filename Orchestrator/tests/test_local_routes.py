"""Tests for the on-device (local Gemma) tool-bridge endpoints (Task 0.2).

These exercise the HTTP contract of:
  POST /local/tools/search   — semantic tool discovery (≤ k schemas)
  POST /local/tools/execute  — execute a ToolVault tool

Hermetic: the live embedding backend (Gemini key is IP-restricted) is NEVER
hit. ``execute_tool`` and the meta-tool search are monkeypatched at module
level on ``local_routes`` so no network/credentials are required.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from Orchestrator.routes import local_routes


@pytest.fixture
def client():
    """TestClient with the startup embedding-sync hook mocked (it spawns a
    daemon thread calling sync_embeddings, which would hit the network)."""
    with patch("Orchestrator.toolvault.embeddings.sync_embeddings") as m_src:
        m_src.return_value = {"x": {"vector": [0.1]}}
        from Orchestrator.app import app
        with TestClient(app) as c:
            yield c


# ---------------------------------------------------------------------------
# /local/tools/execute
# ---------------------------------------------------------------------------

def test_tools_execute_routes_through_execute_tool(client, monkeypatch):
    """POST /local/tools/execute hands (tool, params+operator, operator) to
    execute_tool and surfaces {success, result}."""

    class _FakeResult:
        success = True

        def __init__(self, tool, operator):
            self.result = {"echo": tool, "op": operator}

    async def fake_execute_tool(tool, params, operator):
        # operator must be threaded both into params AND passed positionally
        assert params.get("operator") == operator
        return _FakeResult(tool, operator)

    monkeypatch.setattr(local_routes, "execute_tool", fake_execute_tool)

    resp = client.post(
        "/local/tools/execute",
        json={"tool": "search_snapshots", "params": {"query": "x"}, "operator": "Brandon"},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "success": True,
        "result": {"echo": "search_snapshots", "op": "Brandon"},
    }


def test_tools_execute_requires_tool(client):
    """Missing/blank tool → 400 with success False."""
    resp = client.post("/local/tools/execute", json={"params": {"query": "x"}})
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


# ---------------------------------------------------------------------------
# /local/tools/search
# ---------------------------------------------------------------------------

def test_tools_search_returns_schemas(client, monkeypatch):
    """POST /local/tools/search returns ≤ k {name, description, parameters}
    dicts. The underlying meta_tool call is mocked (endpoint-contract test,
    not a semantic-search-quality test)."""

    class _FakeMetaResult:
        def __init__(self, success, result, data=None):
            self.success = success
            self.result = result
            self.data = data

    def fake_execute(action, **params):
        if action == "search":
            return _FakeMetaResult(
                True,
                "found",
                data={"matches": [
                    {"name": "tool_a", "score": 0.9},
                    {"name": "tool_b", "score": 0.8},
                    {"name": "tool_c", "score": 0.7},
                ]},
            )
        if action == "read":
            name = params.get("tool_name")
            return _FakeMetaResult(
                True,
                f"=== Tool: {name} ===",
                data={
                    "name": name,
                    "schema": {"type": "object", "properties": {"q": {"type": "string"}}},
                    "description": f"desc for {name}",
                },
            )
        return _FakeMetaResult(False, "unknown action")

    monkeypatch.setattr(local_routes.meta_tool, "execute", fake_execute)

    resp = client.post("/local/tools/search", json={"query": "search my memory", "k": 3})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["tools"]) == 3
    for t in body["tools"]:
        assert "name" in t
        assert "parameters" in t
        assert "description" in t


def test_tools_search_requires_query(client):
    """Empty query → 400 with success False."""
    resp = client.post("/local/tools/search", json={"query": "  "})
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"] == "query required"
