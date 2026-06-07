"""Tests for the ToolVault v2 admin endpoints (Task 7.2).

Uses FastAPI ``TestClient`` against the real app. The embedding API is NEVER
hit: ``embeddings.sync_embeddings`` is mocked everywhere it's reachable (the
``/toolvault/reload`` handler AND the startup background-sync hook), so no
network/credentials are required.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


EXPECTED_TOOL_COUNT = 48


@pytest.fixture
def client():
    """TestClient with sync_embeddings mocked before app construction triggers
    the startup hook (which spawns a daemon thread calling sync_embeddings)."""
    # Patch at both the source module and the names the route/startup imported.
    with patch("Orchestrator.toolvault.embeddings.sync_embeddings") as m_src:
        m_src.return_value = {"x": {"vector": [0.1]}}
        from Orchestrator.app import app
        with TestClient(app) as c:
            yield c, m_src


def test_health_ok(client):
    """GET /toolvault/health → 200 with the shipping tool count, never fails."""
    c, _ = client
    resp = c.get("/toolvault/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tool_count"] == EXPECTED_TOOL_COUNT
    assert "schema_only" in body
    assert "load_errors" in body
    assert body["embedding_coverage"]["total"] == EXPECTED_TOOL_COUNT


def test_validate_ok_true(client):
    """GET /toolvault/validate → 200 and ok=True on the clean real tree."""
    c, _ = client
    resp = c.get("/toolvault/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["tool_count"] == EXPECTED_TOOL_COUNT
    assert body["errors"] == {}


def test_reload_calls_sync_and_returns_shape(client):
    """POST /toolvault/reload → 200, calls sync_embeddings (mocked), right shape."""
    c, m_sync = client
    # Reset the call counter — the startup hook may have already invoked it.
    m_sync.reset_mock()

    resp = c.post("/toolvault/reload")
    assert resp.status_code == 200

    # The reload handler must have driven the (mocked) embedding sync.
    assert m_sync.called

    body = resp.json()
    assert body["reloaded"] is True
    assert body["tool_count"] == EXPECTED_TOOL_COUNT
    # embedded == len(mocked store) == 1
    assert body["embedded"] == 1
    assert body["errors"] == {}
