"""Unit tests for the Zellij endpoints in cli_agent_routes.py.

Acceptance criteria covered:
- I8 operator-prefix gate: cross-operator DELETE returns 403.
- I7 zero-UUID in state file post-launch.
- 503 when CLI_AGENT_BACKEND unset (effective backend = tmux).
- 201 launch success when backend=zellij + healthy.

Mocks zellij_client + zellij_state so the suite runs without zellij-web.
"""
from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _client():
    from Orchestrator.app import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_health_cache():
    """The cli_agent TTL cache leaks across tests — reset before each
    so backend-resolution is deterministic."""
    import Orchestrator.cli_agent as cli_agent
    cli_agent._reset_health_cache_for_tests()
    yield
    cli_agent._reset_health_cache_for_tests()


# --- I8: cross-operator delete returns 403 ----------------------------


def test_delete_cross_operator_returns_403(monkeypatch):
    """Audit I8 acceptance: an operator must NOT be able to delete a
    session whose name starts with another operator's prefix.

    The handler validates the prefix BEFORE any backend gating, so we
    don't even need zellij to be healthy for this test."""
    c = _client()
    r = c.delete(
        "/cli-agent/zellij/sessions/other-op__claude__abc__1234",
        params={"op": "Brandon"},
    )
    assert r.status_code == 403, r.text
    body = r.json()
    assert "another operator" in body["detail"].lower()


# --- DELETE own session returns 204 -----------------------------------


def test_delete_own_session_returns_204(monkeypatch):
    """Operator deleting their own session: backend=zellij + healthy +
    state lookup + kill + remove all mocked → 204 No Content."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    import Orchestrator.cli_agent as cli_agent
    from Orchestrator.cli_agent import zellij_client, zellij_state

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_state, "list_for_operator", return_value=[
             {"session_name": "Brandon__terminal", "token_name": "token_5"}
         ]), \
         patch.object(zellij_client, "revoke_token") as mock_revoke, \
         patch.object(zellij_client, "kill_session") as mock_kill, \
         patch.object(zellij_state, "remove_session") as mock_remove:
        c = _client()
        r = c.delete(
            "/cli-agent/zellij/sessions/Brandon__terminal",
            params={"op": "Brandon"},
        )

    assert r.status_code == 204, r.text
    mock_revoke.assert_called_once_with("token_5")
    mock_kill.assert_called_once_with("Brandon__terminal")
    mock_remove.assert_called_once_with("Brandon__terminal")


# --- launch with default tmux backend returns 503 ---------------------


def test_launch_with_default_tmux_backend_returns_503(monkeypatch):
    """When CLI_AGENT_BACKEND is unset (default tmux), the Zellij launch
    endpoint must 503 — never silently invoke a different backend."""
    monkeypatch.delenv("CLI_AGENT_BACKEND", raising=False)

    c = _client()
    r = c.post(
        "/cli-agent/zellij/launch",
        json={"provider": "terminal"},
        params={"op": "Brandon"},
    )
    assert r.status_code == 503, r.text
    assert "not active" in r.json()["detail"].lower()


# --- launch with backend=zellij + healthy returns 201 ------------------


def test_launch_with_zellij_backend_returns_201_and_no_uuid_persisted(
    monkeypatch, tmp_path,
):
    """Full happy-path: mock the entire Zellij side. Verify response
    shape (session_name + session_url + token + expires_at) AND that the
    persisted state file contains zero UUID-shaped strings (audit I7)."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    import Orchestrator.cli_agent as cli_agent
    from Orchestrator.cli_agent import zellij_client, zellij_state

    # Redirect state file to tmp_path so we can inspect it.
    state_dir = tmp_path / "state"
    state_path = state_dir / "zellij_sessions.json"
    monkeypatch.setattr(zellij_state, "_STATE_DIR", state_dir)
    monkeypatch.setattr(zellij_state, "_STATE_PATH", state_path)

    fake_token_value = "6dac3716-1a65-4ea6-95f8-c54af9bdebb0"

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "mint_token",
                      return_value=("token_3", fake_token_value)), \
         patch.object(zellij_client, "launch_session") as mock_launch:
        c = _client()
        r = c.post(
            "/cli-agent/zellij/launch",
            json={"provider": "terminal"},
            params={"op": "Brandon"},
        )

    assert r.status_code == 201, r.text
    body = r.json()
    assert "session_name" in body
    assert body["session_name"] == "Brandon__terminal"
    assert "session_url" in body
    # Same-origin proxy URL — must NOT be a raw localhost URL.
    assert body["session_url"].startswith("/app-proxy/")
    assert "session=Brandon__terminal" in body["session_url"]
    assert f"token={fake_token_value}" in body["session_url"]
    assert body["token"] == fake_token_value
    # Terminal-mode token is long-lived; expires_at is null.
    assert body["expires_at"] is None

    # Audit I7 acceptance: the persisted state file MUST NOT contain a
    # UUID-shaped string. The raw token value lives only in the response.
    assert state_path.exists(), "launch handler did not persist state"
    state_raw = state_path.read_text(encoding="utf-8")
    assert not _UUID_RE.search(state_raw), (
        f"audit I7 violation — UUID found in state file:\n{state_raw}"
    )
    rows = json.loads(state_raw)
    assert len(rows) == 1
    row = rows[0]
    assert row["operator"] == "Brandon"
    assert row["provider"] == "terminal"
    assert row["session_name"] == "Brandon__terminal"
    assert row["token_name"] == "token_3"
    assert row["expires_at"] is None
    # Defense in depth: token_name is the safe handle, NEVER the UUID value.
    assert row["token_name"] != fake_token_value

    # launch_session was invoked with binary=None for terminal mode.
    mock_launch.assert_called_once()
    args, kwargs = mock_launch.call_args
    # Positional: (session_name, binary)
    assert args[0] == "Brandon__terminal"
    assert args[1] is None  # terminal → no binary


# --- backend-status endpoint reports configured + effective -----------


def test_backend_status_reports_configured_and_effective(monkeypatch):
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "list_sessions", return_value=[
             {"name": "Brandon__terminal", "created_at": "..."},
             {"name": "Other__terminal", "created_at": "..."},
         ]):
        c = _client()
        r = c.get("/cli-agent/zellij/backend-status", params={"op": "Brandon"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured_backend"] == "zellij"
    assert body["effective_backend"] == "zellij"
    assert body["web_daemon_running"] is True
    assert body["session_count_total"] == 2
    assert body["my_session_count"] == 1  # only Brandon__ prefix


def test_backend_status_when_default_tmux(monkeypatch):
    monkeypatch.delenv("CLI_AGENT_BACKEND", raising=False)
    c = _client()
    r = c.get("/cli-agent/zellij/backend-status", params={"op": "Brandon"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured_backend"] == "tmux"
    assert body["effective_backend"] == "tmux"
    assert body["web_daemon_running"] is False
