import time
from unittest.mock import MagicMock
from Orchestrator.cli_agent.reaper import reap_idle_sessions


def test_reaps_sessions_idle_beyond_threshold():
    mgr = MagicMock()
    now = int(time.time())
    mgr._tmux.return_value.stdout = (
        f"cli-agent-Brandon__claude__grocery-store {now - 8 * 86400}\n"
        f"cli-agent-Brandon__claude__fresh {now - 3600}\n"
    )
    mgr._tmux.return_value.returncode = 0
    killed = reap_idle_sessions(mgr, idle_seconds=7 * 86400)
    assert killed == ["cli-agent-Brandon__claude__grocery-store"]
    mgr.kill.assert_called_once_with("cli-agent-Brandon__claude__grocery-store")


def test_skips_non_cli_agent_sessions():
    mgr = MagicMock()
    now = int(time.time())
    mgr._tmux.return_value.stdout = (
        f"some-other-session {now - 99 * 86400}\n"
    )
    mgr._tmux.return_value.returncode = 0
    killed = reap_idle_sessions(mgr, idle_seconds=7 * 86400)
    assert killed == []
    mgr.kill.assert_not_called()


def test_returns_empty_when_no_server():
    """If tmux server isn't running, list-sessions returns nonzero
    and we should bail out gracefully with an empty list — NOT raise."""
    mgr = MagicMock()
    mgr._tmux.return_value.returncode = 1
    mgr._tmux.return_value.stdout = ""
    killed = reap_idle_sessions(mgr, idle_seconds=7 * 86400)
    assert killed == []
    mgr.kill.assert_not_called()


def test_skips_malformed_lines():
    """Lines without a parseable activity timestamp should be skipped
    silently rather than crashing the reaper."""
    mgr = MagicMock()
    now = int(time.time())
    mgr._tmux.return_value.stdout = (
        "cli-agent-Brandon__claude__broken\n"  # no activity timestamp
        f"cli-agent-Brandon__claude__valid {now - 99 * 86400}\n"
        "garbage-line-no-spaces\n"
    )
    mgr._tmux.return_value.returncode = 0
    killed = reap_idle_sessions(mgr, idle_seconds=7 * 86400)
    assert killed == ["cli-agent-Brandon__claude__valid"]


# --- Phase 2: zellij-aware reaper --------------------------------------
from unittest.mock import patch
from Orchestrator.cli_agent import reaper as _reaper


def test_parse_age_seconds_formats():
    assert _reaper._parse_age_seconds("26days 15h 17m 30s ago") == 26*86400 + 15*3600 + 17*60 + 30
    assert _reaper._parse_age_seconds("4m 56s ago") == 4*60 + 56
    assert _reaper._parse_age_seconds("3h 59m 23s ago") == 3*3600 + 59*60 + 23
    assert _reaper._parse_age_seconds("8days 7h 50m 10s ago") == 8*86400 + 7*3600 + 50*60 + 10
    # Unparseable -> None (caller leaves the session alone).
    assert _reaper._parse_age_seconds("whoknows") is None
    assert _reaper._parse_age_seconds("") is None


def test_reap_idle_zellij_reaps_old_exited_keeps_recent():
    """Old sessions (>7d, EXITED or running) reaped; recent ones preserved."""
    sessions = [
        {"name": "Brandon__terminal__1779878290", "created_at": "26days 15h ago", "exited": True},
        {"name": "Brandon__claude__root", "created_at": "9days ago", "exited": True},
        {"name": "Brandon__terminal__root", "created_at": "3h 59m ago", "exited": False},  # recent -> keep
        {"name": "bbx test", "created_at": "27days ago", "exited": True},  # not BBX-named -> never touch
    ]
    with patch("Orchestrator.cli_agent.zellij_client.list_sessions", return_value=sessions), \
         patch("Orchestrator.cli_agent.zellij_client.kill_session") as mock_kill, \
         patch("Orchestrator.cli_agent.zellij_state.remove_session"):
        killed = _reaper.reap_idle_zellij_sessions(idle_seconds=7*86400)

    assert set(killed) == {"Brandon__terminal__1779878290", "Brandon__claude__root"}
    killed_args = {c.args[0] for c in mock_kill.call_args_list}
    assert killed_args == {"Brandon__terminal__1779878290", "Brandon__claude__root"}
    # The recent terminal and the non-BBX "bbx test" are NOT reaped.
    assert "Brandon__terminal__root" not in killed
    assert "bbx test" not in killed


def test_reap_idle_zellij_skips_unparseable_age():
    """A session with an unparseable age is never reaped (fail-safe)."""
    sessions = [{"name": "Brandon__terminal__root", "created_at": "???", "exited": True}]
    with patch("Orchestrator.cli_agent.zellij_client.list_sessions", return_value=sessions), \
         patch("Orchestrator.cli_agent.zellij_client.kill_session") as mock_kill:
        killed = _reaper.reap_idle_zellij_sessions(idle_seconds=7*86400)
    assert killed == []
    mock_kill.assert_not_called()


def test_reap_idle_zellij_empty_on_cli_failure():
    with patch("Orchestrator.cli_agent.zellij_client.list_sessions", side_effect=RuntimeError("down")):
        assert _reaper.reap_idle_zellij_sessions() == []
