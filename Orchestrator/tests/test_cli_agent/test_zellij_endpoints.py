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


def test_launch_with_zellij_backend_returns_201_no_token_in_response(
    monkeypatch, tmp_path,
):
    """Phase 5 master-token model (2026-05-26): the launch endpoint no
    longer mints per-session tokens. The orchestrator's app-proxy
    injects the master token cookie on every upstream forward; clients
    never see tokens. Verify:
      - response shape: session_name + session_url + token (None) +
        expires_at (None)
      - session_url has NO `?token=` query param
      - state row has token_name="master" placeholder
      - audit-I7-style check still holds: no UUID-shaped strings in
        the state file (the master token value is stored separately
        in ~/.local/share/blackbox/zellij-master.token, NOT in
        zellij_sessions.json)
    """
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    import Orchestrator.cli_agent as cli_agent
    from Orchestrator.cli_agent import zellij_client, zellij_state

    # Redirect state file to tmp_path so we can inspect it.
    state_dir = tmp_path / "state"
    state_path = state_dir / "zellij_sessions.json"
    monkeypatch.setattr(zellij_state, "_STATE_DIR", state_dir)
    monkeypatch.setattr(zellij_state, "_STATE_PATH", state_path)

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "session_exists", return_value=False), \
         patch.object(zellij_client, "list_sessions", return_value=[]), \
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
    # Phase 2 resume model: a non-fork launch uses the DETERMINISTIC name
    # {op}__{provider}__{app_or_root} (no timestamp), so "open the terminal"
    # always maps to the same resume identity.
    assert body["session_name"] == "Brandon__terminal__root", body["session_name"]
    assert body["resumed"] is False  # session_exists=False -> created, not resumed
    minted_name = body["session_name"]
    assert "session_url" in body
    # Same-origin proxy URL — must NOT be a raw localhost URL.
    assert body["session_url"].startswith("/app-proxy/")
    # Session name lives in the URL PATH (Zellij reads via pathname.split('/').pop()).
    assert f"/{minted_name}" in body["session_url"]
    # Phase 5 master-token model: NO `?token=` query param in session_url.
    assert "?token=" not in body["session_url"], (
        f"master-token model violation — `?token=` leaked into session_url: {body['session_url']!r}"
    )
    # token field is now always None (clients never see tokens).
    assert body["token"] is None
    # Terminal-mode token is long-lived; expires_at is null.
    assert body["expires_at"] is None

    # State file invariant (renamed from audit-I7 since the per-session-
    # token rationale is gone, but the test still has value as a sanity
    # check that we don't accidentally start persisting auth values here).
    # The master token value lives in ~/.local/share/blackbox/zellij-master.token,
    # NEVER in zellij_sessions.json.
    assert state_path.exists(), "launch handler did not persist state"
    state_raw = state_path.read_text(encoding="utf-8")
    assert not _UUID_RE.search(state_raw), (
        f"UUID found in state file (master token should never be persisted here):\n{state_raw}"
    )
    rows = json.loads(state_raw)
    assert len(rows) == 1
    row = rows[0]
    assert row["operator"] == "Brandon"
    assert row["provider"] == "terminal"
    assert row["session_name"] == minted_name
    # Phase 5: token_name is the literal "master" placeholder.
    assert row["token_name"] == "master"
    assert row["expires_at"] is None

    # launch_session was invoked with binary=None for terminal mode.
    mock_launch.assert_called_once()
    args, kwargs = mock_launch.call_args
    # Positional: (session_name, binary)
    assert args[0] == minted_name
    assert args[1] is None  # terminal → no binary


# --- launch accepts provider="grok" ------------------------------------


def test_launch_with_grok_provider_returns_201(monkeypatch, tmp_path):
    """grok is a supported zellij launch provider: the endpoint must NOT
    400 on it, and must resolve + pass the grok binary to launch_session."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client, zellij_state
    from Orchestrator.routes import cli_agent_routes

    state_dir = tmp_path / "state"
    state_path = state_dir / "zellij_sessions.json"
    monkeypatch.setattr(zellij_state, "_STATE_DIR", state_dir)
    monkeypatch.setattr(zellij_state, "_STATE_PATH", state_path)
    # Don't depend on a real grok install on the box running the suite.
    monkeypatch.setattr(
        cli_agent_routes, "provider_bin",
        lambda name: "/fake/.local/bin/grok" if name == "grok" else None,
    )

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "session_exists", return_value=False), \
         patch.object(zellij_client, "list_sessions", return_value=[]), \
         patch.object(zellij_client, "launch_session") as mock_launch:
        c = _client()
        r = c.post(
            "/cli-agent/zellij/launch",
            json={"provider": "grok"},
            params={"op": "Brandon"},
        )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["session_name"] == "Brandon__grok__root", body["session_name"]
    mock_launch.assert_called_once()
    args, kwargs = mock_launch.call_args
    assert args[0] == "Brandon__grok__root"
    assert args[1] == "/fake/.local/bin/grok"  # resolved binary, not None

    rows = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert rows[0]["provider"] == "grok"


# --- spawn allowlist includes grok --------------------------------------


def test_spawn_accepts_grok_binary(monkeypatch):
    """grok must be in _SPAWN_ALLOWED_BINARIES: POST /zellij/spawn with
    binary="grok" into the operator's own session → 204, and the RESOLVED
    grok path is what gets spawned."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client
    from Orchestrator.routes import cli_agent_routes

    # Don't depend on a real grok install on the box running the suite.
    monkeypatch.setattr(
        cli_agent_routes, "provider_bin",
        lambda name: "/fake/.local/bin/grok" if name == "grok" else None,
    )

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "spawn_in_place") as mock_spawn:
        c = _client()
        r = c.post(
            "/cli-agent/zellij/spawn",
            json={"session_name": "Brandon__terminal__root", "binary": "grok"},
            params={"op": "Brandon"},
        )

    assert r.status_code == 204, r.text
    mock_spawn.assert_called_once_with(
        "Brandon__terminal__root", "/fake/.local/bin/grok",
    )


def test_spawn_rejects_unknown_binary(monkeypatch):
    """The spawn allowlist stays tight: a binary outside
    _SPAWN_ALLOWED_BINARIES → 400, and nothing is spawned."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "spawn_in_place") as mock_spawn:
        c = _client()
        r = c.post(
            "/cli-agent/zellij/spawn",
            json={"session_name": "Brandon__terminal__root", "binary": "rm"},
            params={"op": "Brandon"},
        )

    assert r.status_code == 400, r.text
    assert "not in allowlist" in r.json()["detail"]
    mock_spawn.assert_not_called()


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


# --- Phase 2: attach-if-exists (resume) -------------------------------


def test_launch_attaches_if_session_exists_no_relaunch(monkeypatch, tmp_path):
    """Launching twice with the same (op, provider, app) and NO fork: the
    second call must ATTACH the existing session (resumed=True) and NOT
    call launch_session again (no duplicate)."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client, zellij_state

    state_dir = tmp_path / "state"
    monkeypatch.setattr(zellij_state, "_STATE_DIR", state_dir)
    monkeypatch.setattr(zellij_state, "_STATE_PATH", state_dir / "zellij_sessions.json")

    # First launch: session does NOT exist -> create.
    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "session_exists", return_value=False), \
         patch.object(zellij_client, "list_sessions", return_value=[]), \
         patch.object(zellij_client, "launch_session") as first_launch:
        c = _client()
        r1 = c.post("/cli-agent/zellij/launch",
                    json={"provider": "claude", "app": "grocery-store"},
                    params={"op": "Brandon"})
    assert r1.status_code == 201, r1.text
    name1 = r1.json()["session_name"]
    assert name1 == "Brandon__claude__grocery-store"
    assert r1.json()["resumed"] is False
    first_launch.assert_called_once()

    # Second launch (same triple): session EXISTS -> attach, no relaunch.
    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "session_exists", return_value=True), \
         patch.object(zellij_client, "launch_session") as second_launch:
        r2 = c.post("/cli-agent/zellij/launch",
                    json={"provider": "claude", "app": "grocery-store"},
                    params={"op": "Brandon"})
    assert r2.status_code == 201, r2.text
    assert r2.json()["session_name"] == name1, "same deterministic name on resume"
    assert r2.json()["resumed"] is True
    second_launch.assert_not_called()  # NO duplicate launch on resume

    # Exactly one state row for the triple (idempotent upsert).
    rows = zellij_state.load()
    assert [row["session_name"] for row in rows] == [name1]


def test_launch_fork_creates_distinct_session(monkeypatch, tmp_path):
    """fork=true mints a UNIQUE timestamped name distinct from the
    deterministic resume name, and always CREATES (never attaches)."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client, zellij_state

    state_dir = tmp_path / "state"
    monkeypatch.setattr(zellij_state, "_STATE_DIR", state_dir)
    monkeypatch.setattr(zellij_state, "_STATE_PATH", state_dir / "zellij_sessions.json")

    resume_name = "Brandon__claude__grocery-store"

    # Even though a session with the resume name "exists", a fork must NOT
    # attach to it — it forks a brand-new uniquely-named session.
    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "session_exists", return_value=True) as exists_probe, \
         patch.object(zellij_client, "list_sessions", return_value=[]), \
         patch.object(zellij_client, "launch_session") as fork_launch:
        c = _client()
        r = c.post("/cli-agent/zellij/launch",
                   json={"provider": "claude", "app": "grocery-store", "fork": True},
                   params={"op": "Brandon"})

    assert r.status_code == 201, r.text
    forked = r.json()["session_name"]
    assert forked != resume_name, "fork must be distinct from the resume name"
    assert forked.startswith(resume_name + "__"), forked  # {resume}__{ts}
    assert r.json()["resumed"] is False
    # Fork never probes existence + always launches.
    exists_probe.assert_not_called()
    fork_launch.assert_called_once()


# --- YOLO (skip-permissions) launch ------------------------------------


def test_launch_yolo_claude_passes_skip_permissions_flag(monkeypatch, tmp_path):
    """yolo=true with provider=claude: launch_session receives the claude
    skip-permissions flag in args, the session name carries the _yolo
    suffix, and the state row persists yolo=True."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client, zellij_state
    from Orchestrator.routes import cli_agent_routes

    state_dir = tmp_path / "state"
    state_path = state_dir / "zellij_sessions.json"
    monkeypatch.setattr(zellij_state, "_STATE_DIR", state_dir)
    monkeypatch.setattr(zellij_state, "_STATE_PATH", state_path)
    monkeypatch.setattr(
        cli_agent_routes, "provider_bin",
        lambda name: "/fake/.local/bin/claude" if name == "claude" else None,
    )

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "session_exists", return_value=False), \
         patch.object(zellij_client, "list_sessions", return_value=[]), \
         patch.object(zellij_client, "launch_session") as mock_launch:
        c = _client()
        r = c.post(
            "/cli-agent/zellij/launch",
            json={"provider": "claude", "yolo": True},
            params={"op": "Brandon"},
        )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["session_name"] == "Brandon__claude__root__yolo", body["session_name"]

    mock_launch.assert_called_once()
    args, kwargs = mock_launch.call_args
    assert args[0] == "Brandon__claude__root__yolo"
    assert args[1] == "/fake/.local/bin/claude"
    passed_args = args[2] if len(args) > 2 else kwargs.get("args")
    assert passed_args == ["--dangerously-skip-permissions"], passed_args

    rows = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert rows[0]["yolo"] is True


def test_launch_non_yolo_claude_passes_no_args(monkeypatch, tmp_path):
    """Backward compat: omitted yolo behaves like today — claude has no
    PROVIDER_ARGS entry, so launch_session receives args=None (NOT [])."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client, zellij_state
    from Orchestrator.routes import cli_agent_routes

    state_dir = tmp_path / "state"
    monkeypatch.setattr(zellij_state, "_STATE_DIR", state_dir)
    monkeypatch.setattr(zellij_state, "_STATE_PATH", state_dir / "zellij_sessions.json")
    monkeypatch.setattr(
        cli_agent_routes, "provider_bin",
        lambda name: "/fake/.local/bin/claude" if name == "claude" else None,
    )

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "session_exists", return_value=False), \
         patch.object(zellij_client, "list_sessions", return_value=[]), \
         patch.object(zellij_client, "launch_session") as mock_launch:
        c = _client()
        r = c.post(
            "/cli-agent/zellij/launch",
            json={"provider": "claude"},
            params={"op": "Brandon"},
        )

    assert r.status_code == 201, r.text
    assert r.json()["session_name"] == "Brandon__claude__root"  # no _yolo suffix
    mock_launch.assert_called_once()
    args, kwargs = mock_launch.call_args
    passed_args = args[2] if len(args) > 2 else kwargs.get("args")
    assert passed_args is None, passed_args


def test_launch_codex_carries_no_alt_screen_onto_zellij_path(monkeypatch, tmp_path):
    """codex (non-yolo) now carries PROVIDER_ARGS' --no-alt-screen onto
    the zellij path — the documented fix for codex scrollback under
    zellij (previously PROVIDER_ARGS was tmux-only)."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client, zellij_state
    from Orchestrator.routes import cli_agent_routes

    state_dir = tmp_path / "state"
    monkeypatch.setattr(zellij_state, "_STATE_DIR", state_dir)
    monkeypatch.setattr(zellij_state, "_STATE_PATH", state_dir / "zellij_sessions.json")
    monkeypatch.setattr(
        cli_agent_routes, "provider_bin",
        lambda name: "/fake/.local/bin/codex" if name == "codex" else None,
    )

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "session_exists", return_value=False), \
         patch.object(zellij_client, "list_sessions", return_value=[]), \
         patch.object(zellij_client, "launch_session") as mock_launch:
        c = _client()
        r = c.post(
            "/cli-agent/zellij/launch",
            json={"provider": "codex"},
            params={"op": "Brandon"},
        )

    assert r.status_code == 201, r.text
    mock_launch.assert_called_once()
    args, kwargs = mock_launch.call_args
    passed_args = args[2] if len(args) > 2 else kwargs.get("args")
    assert passed_args == ["--no-alt-screen"], passed_args


def test_launch_yolo_codex_appends_flag_after_provider_args(monkeypatch, tmp_path):
    """yolo codex: PROVIDER_ARGS come first, YOLO flag appended after."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client, zellij_state
    from Orchestrator.routes import cli_agent_routes

    state_dir = tmp_path / "state"
    monkeypatch.setattr(zellij_state, "_STATE_DIR", state_dir)
    monkeypatch.setattr(zellij_state, "_STATE_PATH", state_dir / "zellij_sessions.json")
    monkeypatch.setattr(
        cli_agent_routes, "provider_bin",
        lambda name: "/fake/.local/bin/codex" if name == "codex" else None,
    )

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "session_exists", return_value=False), \
         patch.object(zellij_client, "list_sessions", return_value=[]), \
         patch.object(zellij_client, "launch_session") as mock_launch:
        c = _client()
        r = c.post(
            "/cli-agent/zellij/launch",
            json={"provider": "codex", "yolo": True},
            params={"op": "Brandon"},
        )

    assert r.status_code == 201, r.text
    assert r.json()["session_name"] == "Brandon__codex__root__yolo"
    mock_launch.assert_called_once()
    args, kwargs = mock_launch.call_args
    passed_args = args[2] if len(args) > 2 else kwargs.get("args")
    assert passed_args == [
        "--no-alt-screen",
        "--dangerously-bypass-approvals-and-sandbox",
    ], passed_args


def test_launch_yolo_terminal_returns_400(monkeypatch):
    """yolo=true with provider='terminal' is a client error: a bare shell
    has no permission prompts to skip. 400 + nothing launched."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "launch_session") as mock_launch:
        c = _client()
        r = c.post(
            "/cli-agent/zellij/launch",
            json={"provider": "terminal", "yolo": True},
            params={"op": "Brandon"},
        )

    assert r.status_code == 400, r.text
    mock_launch.assert_not_called()


def test_launch_rejects_app_containing_name_delimiter(monkeypatch):
    """`__` is the session-name delimiter — an app containing it can
    forge name collisions (e.g. app="root__yolo" with yolo=false collides
    with the legitimate YOLO session name Brandon__claude__root__yolo,
    attaching to the live YOLO session and falsifying its state row's
    yolo badge via the upsert). Must 400, nothing launched."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "launch_session") as mock_launch:
        c = _client()
        r = c.post(
            "/cli-agent/zellij/launch",
            json={"provider": "claude", "app": "root__yolo"},
            params={"op": "Brandon"},
        )

    assert r.status_code == 400, r.text
    assert "__" in r.json()["detail"]
    mock_launch.assert_not_called()


def test_launch_rejects_non_boolean_yolo(monkeypatch):
    """yolo is a permission-bypass toggle — coercion must be strict. A
    client sending the STRING "false" (truthy under bool()) must get a
    400, not a silent YOLO launch."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "launch_session") as mock_launch:
        c = _client()
        r = c.post(
            "/cli-agent/zellij/launch",
            json={"provider": "claude", "yolo": "false"},
            params={"op": "Brandon"},
        )

    assert r.status_code == 400, r.text
    assert "boolean" in r.json()["detail"].lower()
    mock_launch.assert_not_called()


def test_list_sessions_includes_yolo_field(monkeypatch):
    """Sessions list response carries yolo per session: True for a yolo
    row, False for a legacy row that predates the field (no crash)."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client, zellij_state

    state_rows = [
        {
            "operator": "Brandon",
            "provider": "claude",
            "app": None,
            "session_name": "Brandon__claude__root__yolo",
            "token_name": "master",
            "created_at": "2026-07-03T00:00:00+00:00",
            "expires_at": None,
            "yolo": True,
        },
        {
            # Legacy row minted before the yolo field existed — no key.
            "operator": "Brandon",
            "provider": "terminal",
            "app": None,
            "session_name": "Brandon__terminal__root",
            "token_name": "master",
            "created_at": "2026-07-01T00:00:00+00:00",
            "expires_at": None,
        },
    ]
    live = [
        {"name": "Brandon__claude__root__yolo", "created_at": "1h ago"},
        {"name": "Brandon__terminal__root", "created_at": "2days ago"},
    ]

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "list_sessions", return_value=live), \
         patch.object(zellij_state, "list_for_operator", return_value=state_rows):
        c = _client()
        r = c.get("/cli-agent/zellij/sessions", params={"op": "Brandon"})

    assert r.status_code == 200, r.text
    sessions = {s["name"]: s for s in r.json()["sessions"]}
    assert sessions["Brandon__claude__root__yolo"]["yolo"] is True
    assert sessions["Brandon__terminal__root"]["yolo"] is False


# --- name helpers: _yolo suffix (pure-function unit tests) ---------------


def test_zellij_resume_name_yolo_suffix():
    from Orchestrator.routes.cli_agent_routes import _zellij_resume_name

    assert _zellij_resume_name("Brandon", "claude", None) == "Brandon__claude__root"
    assert _zellij_resume_name("Brandon", "claude", None, yolo=True) == (
        "Brandon__claude__root__yolo"
    )
    assert _zellij_resume_name("Brandon", "gemini", "grocery-store", yolo=True) == (
        "Brandon__gemini__grocery-store__yolo"
    )


def test_zellij_fork_name_yolo_suffix():
    from Orchestrator.routes.cli_agent_routes import _zellij_fork_name

    plain = _zellij_fork_name("Brandon", "claude", None)
    assert re.fullmatch(r"Brandon__claude__root__\d+", plain), plain

    yolo = _zellij_fork_name("Brandon", "claude", None, yolo=True)
    assert re.fullmatch(r"Brandon__claude__root__\d+_yolo", yolo), yolo

    yolo_app = _zellij_fork_name("Brandon", "codex", "grocery-store", yolo=True)
    assert re.fullmatch(
        r"Brandon__codex__grocery-store__\d+_yolo", yolo_app
    ), yolo_app


def test_launch_collision_race_falls_back_to_resume(monkeypatch, tmp_path):
    """If the existence probe missed but launch_session hits an existing
    name (rc=1 race), the handler re-checks existence and treats it as a
    successful RESUME rather than a 500."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")
    import subprocess
    from Orchestrator.cli_agent import zellij_client, zellij_state

    state_dir = tmp_path / "state"
    monkeypatch.setattr(zellij_state, "_STATE_DIR", state_dir)
    monkeypatch.setattr(zellij_state, "_STATE_PATH", state_dir / "zellij_sessions.json")

    # First probe says "absent" (race window), launch fails rc=1, second
    # probe confirms the session now exists -> resume.
    exists_results = iter([False, True])
    err = subprocess.CalledProcessError(1, ["zellij"], output="", stderr="session exists")

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "session_exists", side_effect=lambda n: next(exists_results)), \
         patch.object(zellij_client, "list_sessions", return_value=[]), \
         patch.object(zellij_client, "launch_session", side_effect=err):
        c = _client()
        r = c.post("/cli-agent/zellij/launch",
                   json={"provider": "terminal"},
                   params={"op": "Brandon"})

    assert r.status_code == 201, r.text
    assert r.json()["resumed"] is True
    assert r.json()["session_name"] == "Brandon__terminal__root"


# --- per-operator 12-session soft cap ------------------------------------


def _cap_fixture_rows(op: str, n: int, provider: str = "terminal"):
    """Build n LIVE sessions for ``op``: a matching (state_rows, live)
    pair — every state row's name also appears in the zellij list, so
    the state∩live intersection counts exactly n."""
    state_rows: list[dict] = []
    live: list[dict] = []
    for i in range(n):
        name = f"{op}__{provider}__app{i}__{1000 + i}"
        state_rows.append({
            "operator": op,
            "provider": provider,
            "app": f"app{i}",
            "session_name": name,
            "token_name": "master",
            "created_at": "2026-07-03T00:00:00+00:00",
            "expires_at": None,
        })
        live.append({"name": name, "created_at": "1h ago"})
    return state_rows, live


def test_launch_fork_at_cap_returns_409(monkeypatch):
    """12 live sessions for the operator → a fork launch (always a
    CREATE) must 409 with the exact toast message and never reach
    launch_session."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client, zellij_state

    state_rows, live = _cap_fixture_rows("Brandon", 12)

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "list_sessions", return_value=live), \
         patch.object(zellij_state, "list_for_operator", return_value=state_rows), \
         patch.object(zellij_client, "launch_session") as mock_launch:
        c = _client()
        r = c.post(
            "/cli-agent/zellij/launch",
            json={"provider": "terminal", "fork": True},
            params={"op": "Brandon"},
        )

    assert r.status_code == 409, r.text
    # "(X)" is LITERAL — the X kill button in the Android session UI
    # (the client toasts this string verbatim), NOT the live count.
    assert r.json()["detail"] == (
        "Session limit reached (12). Close a session (X) first."
    )
    mock_launch.assert_not_called()


def test_launch_resume_at_cap_still_succeeds(monkeypatch, tmp_path):
    """Resuming an EXISTING session at the cap must still succeed — an
    attach doesn't add a session. session_exists=True routes to the
    ATTACH path, so the cap check must not fire even though the
    operator already has 12 live sessions."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client, zellij_state

    state_dir = tmp_path / "state"
    monkeypatch.setattr(zellij_state, "_STATE_DIR", state_dir)
    monkeypatch.setattr(zellij_state, "_STATE_PATH", state_dir / "zellij_sessions.json")

    # 11 unrelated live sessions + the resume target itself = 12 (at cap).
    state_rows, live = _cap_fixture_rows("Brandon", 11)
    state_rows.append({
        "operator": "Brandon",
        "provider": "terminal",
        "app": None,
        "session_name": "Brandon__terminal__root",
        "token_name": "master",
        "created_at": "2026-07-03T00:00:00+00:00",
        "expires_at": None,
    })
    live.append({"name": "Brandon__terminal__root", "created_at": "1h ago"})

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "session_exists", return_value=True), \
         patch.object(zellij_client, "list_sessions", return_value=live), \
         patch.object(zellij_state, "list_for_operator", return_value=state_rows), \
         patch.object(zellij_client, "launch_session") as mock_launch:
        c = _client()
        r = c.post(
            "/cli-agent/zellij/launch",
            json={"provider": "terminal"},
            params={"op": "Brandon"},
        )

    assert r.status_code == 201, r.text
    assert r.json()["resumed"] is True
    assert r.json()["session_name"] == "Brandon__terminal__root"
    mock_launch.assert_not_called()


def test_launch_below_cap_creates(monkeypatch, tmp_path):
    """11 live sessions (one below the cap) → a create still succeeds."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client, zellij_state

    state_dir = tmp_path / "state"
    monkeypatch.setattr(zellij_state, "_STATE_DIR", state_dir)
    monkeypatch.setattr(zellij_state, "_STATE_PATH", state_dir / "zellij_sessions.json")

    state_rows, live = _cap_fixture_rows("Brandon", 11)

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "session_exists", return_value=False), \
         patch.object(zellij_client, "list_sessions", return_value=live), \
         patch.object(zellij_state, "list_for_operator", return_value=state_rows), \
         patch.object(zellij_client, "launch_session") as mock_launch:
        c = _client()
        r = c.post(
            "/cli-agent/zellij/launch",
            json={"provider": "terminal", "fork": True},
            params={"op": "Brandon"},
        )

    assert r.status_code == 201, r.text
    mock_launch.assert_called_once()


def test_launch_cap_ignores_other_operators_sessions(monkeypatch, tmp_path):
    """Another operator's 12 live sessions must NOT count toward this
    operator's cap — the intersection is per-operator (state rows for
    THIS op, {op}__ prefix)."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client, zellij_state

    state_dir = tmp_path / "state"
    monkeypatch.setattr(zellij_state, "_STATE_DIR", state_dir)
    monkeypatch.setattr(zellij_state, "_STATE_PATH", state_dir / "zellij_sessions.json")

    other_rows, live = _cap_fixture_rows("Other", 12)

    def fake_list_for_operator(op):
        return other_rows if op == "Other" else []

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "session_exists", return_value=False), \
         patch.object(zellij_client, "list_sessions", return_value=live), \
         patch.object(zellij_state, "list_for_operator", side_effect=fake_list_for_operator), \
         patch.object(zellij_client, "launch_session") as mock_launch:
        c = _client()
        r = c.post(
            "/cli-agent/zellij/launch",
            json={"provider": "terminal"},
            params={"op": "Brandon"},
        )

    assert r.status_code == 201, r.text
    mock_launch.assert_called_once()


def test_launch_cap_count_failure_fails_open(monkeypatch, tmp_path):
    """If the live-session count can't be computed (zellij list fails),
    the launch proceeds — soft cap fails open, mirroring the attach
    probe's 'can't tell -> proceed' philosophy."""
    monkeypatch.setenv("CLI_AGENT_BACKEND", "zellij")

    from Orchestrator.cli_agent import zellij_client, zellij_state

    state_dir = tmp_path / "state"
    monkeypatch.setattr(zellij_state, "_STATE_DIR", state_dir)
    monkeypatch.setattr(zellij_state, "_STATE_PATH", state_dir / "zellij_sessions.json")

    with patch.object(zellij_client, "web_server_healthy", return_value=True), \
         patch.object(zellij_client, "session_exists", return_value=False), \
         patch.object(zellij_client, "list_sessions", side_effect=RuntimeError("boom")), \
         patch.object(zellij_client, "launch_session") as mock_launch:
        c = _client()
        r = c.post(
            "/cli-agent/zellij/launch",
            json={"provider": "terminal"},
            params={"op": "Brandon"},
        )

    assert r.status_code == 201, r.text
    mock_launch.assert_called_once()


# --- attach-file: provider-aware attach text (pure-function unit tests) --


def test_provider_parse():
    from Orchestrator.routes.cli_agent_routes import _provider_from_session_name

    assert _provider_from_session_name("Brandon__claude__root", "Brandon") == "claude"
    assert _provider_from_session_name(
        "Brandon__gemini__myapp__1784542317224_yolo", "Brandon"
    ) == "gemini"
    assert _provider_from_session_name("weird", "Brandon") == "unknown"


def test_attach_text_terminal():
    from Orchestrator.routes.cli_agent_routes import _build_attach_text

    assert _build_attach_text("terminal", "/a/b c.png") == '"/a/b c.png" '


def test_attach_text_gemini_escapes_spaces():
    from Orchestrator.routes.cli_agent_routes import _build_attach_text

    assert _build_attach_text("gemini", "/a/b c.pdf") == "@/a/b\\ c.pdf "


def test_attach_text_codex_image_bare_path():
    from Orchestrator.routes.cli_agent_routes import _build_attach_text

    assert _build_attach_text("codex", "/a/shot.png") == "/a/shot.png "


def test_attach_text_codex_textfile_sentence():
    from Orchestrator.routes.cli_agent_routes import _build_attach_text

    assert _build_attach_text("codex", "/a/notes.txt") == 'Read this file: "/a/notes.txt" '


def test_attach_text_claude_sentence_no_newline():
    from Orchestrator.routes.cli_agent_routes import _build_attach_text

    out = _build_attach_text("claude", "/a/b.png")
    assert out == 'Read this file: "/a/b.png" '
    assert "\n" not in out
