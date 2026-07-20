import os
import time
from unittest.mock import MagicMock

import pytest

from Orchestrator.cli_agent.reaper import reap_idle_sessions


@pytest.fixture(autouse=True)
def _redirect_terminal_uploads(monkeypatch, tmp_path):
    """Plan Task 5: the zellij reaper removes terminal upload folders.
    Redirect the module default away from the real Portal/uploads/terminal
    for EVERY test in this file so no test can ever touch live upload
    data. Yields the redirected base dir (not created — tests mkdir it)."""
    from Orchestrator.cli_agent import terminal_uploads
    base = tmp_path / "terminal-uploads"
    monkeypatch.setattr(terminal_uploads, "TERMINAL_UPLOADS_DIR", base)
    yield base


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


# --- Task 5: terminal upload folders die with their session ------------


def _patched_zellij(sessions):
    """Context managers for a reap pass over a fake session list."""
    return (
        patch("Orchestrator.cli_agent.zellij_client.list_sessions",
              return_value=sessions),
        patch("Orchestrator.cli_agent.zellij_client.kill_session"),
        patch("Orchestrator.cli_agent.zellij_state.remove_session"),
    )


def test_reap_removes_upload_folder_of_reaped_session(_redirect_terminal_uploads):
    """A session the reaper just killed gets its upload folder removed
    DIRECTLY (its mtime is young — the age-gated orphan sweep would have
    given it grace, so the reap path must delete it explicitly)."""
    base = _redirect_terminal_uploads
    base.mkdir(parents=True)
    reaped_folder = base / "Brandon__claude__old"
    reaped_folder.mkdir()
    (reaped_folder / "shot.png").write_bytes(b"x")
    kept_folder = base / "Brandon__terminal__root"
    kept_folder.mkdir()

    sessions = [
        {"name": "Brandon__claude__old", "created_at": "9days ago", "exited": True},
        {"name": "Brandon__terminal__root", "created_at": "3h 59m ago", "exited": False},
    ]
    p1, p2, p3 = _patched_zellij(sessions)
    with p1, p2, p3:
        killed = _reaper.reap_idle_zellij_sessions(idle_seconds=7 * 86400)

    assert killed == ["Brandon__claude__old"]
    assert not reaped_folder.exists()
    assert kept_folder.exists()


def test_reap_sweeps_old_orphan_folders_keeps_live_and_young(_redirect_terminal_uploads):
    """Out-of-band deaths (session killed from zellij's own session
    manager) leave orphan folders — the reap pass sweeps those older
    than the idle window, keeps live sessions' folders (however old)
    and young orphans (EXITED-but-resurrectable / mid-upload grace)."""
    base = _redirect_terminal_uploads
    base.mkdir(parents=True)
    week = 7 * 86400
    now = time.time()

    live_folder = base / "Brandon__terminal__root"
    live_folder.mkdir()
    os.utime(live_folder, (now - 2 * week, now - 2 * week))  # ancient but live

    old_orphan = base / "Brandon__claude__gone"
    old_orphan.mkdir()
    os.utime(old_orphan, (now - week - 3600, now - week - 3600))

    young_orphan = base / "Brandon__codex__gone"
    young_orphan.mkdir()  # fresh mtime -> grace

    sessions = [
        {"name": "Brandon__terminal__root", "created_at": "3h 59m ago", "exited": False},
    ]
    p1, p2, p3 = _patched_zellij(sessions)
    with p1, p2, p3:
        killed = _reaper.reap_idle_zellij_sessions(idle_seconds=week)

    assert killed == []
    assert live_folder.exists()
    assert young_orphan.exists()
    assert not old_orphan.exists()


def test_orphan_sweep_receives_post_reap_live_set():
    """The sweep's live set is the sessions still alive AFTER reaping —
    a just-reaped session must NOT shield its folder, while every
    surviving session (even non-BBX-named) must."""
    week = 7 * 86400
    sessions = [
        {"name": "Brandon__claude__old", "created_at": "9days ago", "exited": True},
        {"name": "Brandon__terminal__root", "created_at": "3h 59m ago", "exited": False},
        {"name": "bbx test", "created_at": "27days ago", "exited": True},
    ]
    p1, p2, p3 = _patched_zellij(sessions)
    with p1, p2, p3, \
         patch("Orchestrator.cli_agent.terminal_uploads.sweep_orphans",
               return_value=[]) as mock_sweep:
        _reaper.reap_idle_zellij_sessions(idle_seconds=week)

    mock_sweep.assert_called_once()
    live_arg, age_arg = mock_sweep.call_args.args[:2]
    assert live_arg == {"Brandon__terminal__root", "bbx test"}
    assert age_arg == week


def test_reap_survives_upload_cleanup_failures():
    """Error isolation: a broken uploads layer (both the per-kill removal
    AND the sweep raising) must not kill the reap pass — sessions still
    get reaped and the killed list is still returned."""
    sessions = [
        {"name": "Brandon__claude__old", "created_at": "9days ago", "exited": True},
    ]
    p1, p2, p3 = _patched_zellij(sessions)
    with p1, p2, p3, \
         patch("Orchestrator.cli_agent.terminal_uploads.remove_for_session",
               side_effect=RuntimeError("disk on fire")), \
         patch("Orchestrator.cli_agent.terminal_uploads.sweep_orphans",
               side_effect=RuntimeError("disk still on fire")):
        killed = _reaper.reap_idle_zellij_sessions(idle_seconds=7 * 86400)

    assert killed == ["Brandon__claude__old"]
