"""Unit tests for Orchestrator.cli_agent.zellij_client.

Mocks subprocess + urllib so the suite runs without a live zellij-web
daemon. Covers the audit-acceptance criteria:

- M8 mint_token preamble + last-match robustness
- C2/polish-4 web_server_healthy identity check
- M15/polish-9 ensure_config regeneration backup
- launch_session argv shape (terminal vs CLI provider modes)
- kill_session / revoke_token idempotency
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from Orchestrator.cli_agent import zellij_client


# --- mint_token ---------------------------------------------------------


def _mk_completed_process(stdout: str = "", stderr: str = "", returncode: int = 0):
    cp = subprocess.CompletedProcess(args=["zellij"], returncode=returncode)
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


def test_mint_token_parses_spike_validated_stdout():
    out = (
        "Created token successfully\n\n"
        "token_3: 6dac3716-1a65-4ea6-95f8-c54af9bdebb0\n"
    )
    with patch.object(zellij_client, "_run", return_value=_mk_completed_process(stdout=out)):
        name, value = zellij_client.mint_token()
    assert name == "token_3"
    assert value == "6dac3716-1a65-4ea6-95f8-c54af9bdebb0"


def test_mint_token_rejects_missing_preamble():
    # Output without "Created token successfully" preamble — audit M8 robustness.
    out = "token_3: 6dac3716-1a65-4ea6-95f8-c54af9bdebb0\n"
    with patch.object(zellij_client, "_run", return_value=_mk_completed_process(stdout=out)):
        with pytest.raises(RuntimeError, match="preamble missing"):
            zellij_client.mint_token()


def test_mint_token_picks_last_token_line():
    # Defensive: future Zellij versions might dump the full token list with
    # the just-minted entry appended. Take the LAST match.
    out = (
        "Created token successfully\n\n"
        "token_1: aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n"
        "token_2: bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\n"
        "token_5: cccccccc-cccc-cccc-cccc-cccccccccccc\n"
    )
    with patch.object(zellij_client, "_run", return_value=_mk_completed_process(stdout=out)):
        name, value = zellij_client.mint_token()
    assert name == "token_5"
    assert value == "cccccccc-cccc-cccc-cccc-cccccccccccc"


def test_mint_token_raises_when_no_token_line_at_all():
    out = "Created token successfully\n\nbut nothing useful here\n"
    with patch.object(zellij_client, "_run", return_value=_mk_completed_process(stdout=out)):
        with pytest.raises(RuntimeError, match="Failed to parse"):
            zellij_client.mint_token()


# --- launch_session -----------------------------------------------------


class _FakeProc:
    """Minimal Popen stand-in so launch_session can exercise its detach path."""

    def __init__(self, *, wait_timeouts=True, returncode=0):
        self._wait_timeouts = wait_timeouts
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.stderr = None

    def wait(self, timeout=None):
        if self._wait_timeouts:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_launch_session_terminal_mode_argv_has_no_layout_flags():
    """Terminal mode (binary=None): argv must NOT include `-n` or `--layout`."""
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakeProc(wait_timeouts=True)

    with patch.object(zellij_client.subprocess, "Popen", side_effect=fake_popen):
        with patch.object(zellij_client.pty, "openpty", return_value=(10, 11)):
            with patch.object(zellij_client.os, "close"):
                zellij_client.launch_session("Brandon__terminal", binary=None)

    argv = captured["argv"]
    assert "--session" in argv
    assert "Brandon__terminal" in argv
    assert "-n" not in argv
    assert "--layout" not in argv
    assert "--layout-string" not in argv


def test_launch_session_cli_provider_mode_writes_layout_kdl_and_cleans_up(tmp_path, monkeypatch):
    """CLI mode (binary='claude'): argv contains -n <path>, file has
    `pane command="claude"`, and the temp file is unlinked after."""
    captured = {"argv": None, "layout_text": None, "layout_path": None, "unlinked": False}

    real_unlink = os.unlink

    def fake_popen(argv, **kwargs):
        # Find the layout file path from argv.
        captured["argv"] = argv
        try:
            n_idx = argv.index("-n")
        except ValueError:
            n_idx = -1
        if n_idx >= 0 and n_idx + 1 < len(argv):
            layout_path = argv[n_idx + 1]
            captured["layout_path"] = layout_path
            # Capture file contents BEFORE launch_session cleans it up.
            captured["layout_text"] = Path(layout_path).read_text(encoding="utf-8")
        return _FakeProc(wait_timeouts=True)

    def fake_unlink(path):
        # Record unlink and actually remove if it exists.
        if path == captured["layout_path"]:
            captured["unlinked"] = True
        try:
            real_unlink(path)
        except OSError:
            pass

    with patch.object(zellij_client.subprocess, "Popen", side_effect=fake_popen):
        with patch.object(zellij_client.pty, "openpty", return_value=(20, 21)):
            with patch.object(zellij_client.os, "close"):
                with patch.object(zellij_client.os, "unlink", side_effect=fake_unlink):
                    zellij_client.launch_session(
                        "Brandon__claude__test__1234",
                        binary="claude",
                    )

    argv = captured["argv"]
    assert argv is not None
    assert "--session" in argv
    assert "Brandon__claude__test__1234" in argv
    assert "-n" in argv
    n_idx = argv.index("-n")
    layout_arg = argv[n_idx + 1]
    assert layout_arg.endswith(".kdl"), f"expected .kdl suffix, got {layout_arg}"

    assert captured["layout_text"] is not None
    assert 'pane command="claude"' in captured["layout_text"]

    # Cleanup ran (the os.unlink interception saw the path).
    assert captured["unlinked"] is True


def test_launch_session_kdl_includes_args_block_when_args_supplied():
    captured = {"layout_text": None}

    def fake_popen(argv, **kwargs):
        n_idx = argv.index("-n")
        path = argv[n_idx + 1]
        captured["layout_text"] = Path(path).read_text(encoding="utf-8")
        return _FakeProc(wait_timeouts=True)

    with patch.object(zellij_client.subprocess, "Popen", side_effect=fake_popen):
        with patch.object(zellij_client.pty, "openpty", return_value=(30, 31)):
            with patch.object(zellij_client.os, "close"):
                zellij_client.launch_session(
                    "Brandon__claude__test", binary="claude", args=["--print", "hi"]
                )

    assert captured["layout_text"] is not None
    assert 'pane command="claude"' in captured["layout_text"]
    # args block present, with both args quoted.
    assert 'args "--print" "hi"' in captured["layout_text"]


def test_launch_session_child_env_scrubs_zellij_and_denylist(monkeypatch):
    """The session-backend child env must drop ZELLIJ* vars (nested-pane
    leak — same rationale as _run's scrub) AND the pane denylist keys
    (ANTHROPIC_API_KEY), while keeping ordinary vars like PATH."""
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeProc(wait_timeouts=True)

    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "someones-live-session")
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-server-side-secret")

    with patch.object(zellij_client.subprocess, "Popen", side_effect=fake_popen):
        with patch.object(zellij_client.pty, "openpty", return_value=(40, 41)):
            with patch.object(zellij_client.os, "close"):
                zellij_client.launch_session("Brandon__terminal", binary=None)

    env = captured["env"]
    assert env is not None
    assert not any(k.startswith("ZELLIJ") for k in env)
    assert "ANTHROPIC_API_KEY" not in env
    assert "PATH" in env


# --- kill_session idempotency -------------------------------------------


def test_kill_session_idempotent_on_missing_session():
    """Zellij prints "No session named X found." to stdout (rc=1) when
    asked to kill a missing session — kill_session must NOT raise."""
    err = subprocess.CalledProcessError(
        returncode=1,
        cmd=["zellij", "kill-session", "missing"],
        output="No session named missing found.\n",
        stderr="",
    )
    with patch.object(zellij_client, "_run", side_effect=err):
        # Should not raise.
        zellij_client.kill_session("missing")


def test_kill_session_reraises_unrelated_errors():
    err = subprocess.CalledProcessError(
        returncode=1,
        cmd=["zellij", "kill-session", "x"],
        output="",
        stderr="some unrelated catastrophic failure\n",
    )
    with patch.object(zellij_client, "_run", side_effect=err):
        with pytest.raises(subprocess.CalledProcessError):
            zellij_client.kill_session("x")


# --- revoke_token idempotency -------------------------------------------


def test_revoke_token_idempotent_on_missing_token():
    err = subprocess.CalledProcessError(
        returncode=2,
        cmd=["zellij", "web", "--revoke-token", "token_99"],
        output="Token by that name does not exist.\n",
        stderr="",
    )
    with patch.object(zellij_client, "_run", side_effect=err):
        # Should not raise.
        zellij_client.revoke_token("token_99")


def test_revoke_token_reraises_unrelated_errors():
    err = subprocess.CalledProcessError(
        returncode=1,
        cmd=["zellij", "web", "--revoke-token", "token_99"],
        output="",
        stderr="permission denied\n",
    )
    with patch.object(zellij_client, "_run", side_effect=err):
        with pytest.raises(subprocess.CalledProcessError):
            zellij_client.revoke_token("token_99")


# --- web_server_healthy identity check (audit polish #4) ----------------


class _FakeResp:
    """urllib.request response stand-in with .status, .read(), context-mgr."""

    def __init__(self, *, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self, n=None):
        if n is None:
            return self._body
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_web_server_healthy_returns_true_when_zellij_signature_present():
    body = b"<html><head><title>Zellij Web Client</title></head><body/></html>"
    with patch.object(zellij_client.urllib.request, "urlopen",
                      return_value=_FakeResp(status=200, body=body)):
        assert zellij_client.web_server_healthy(retries=1) is True


def test_web_server_healthy_returns_false_when_200_but_not_zellij():
    """A 200 from a hijacked-port service must NOT pass the identity check."""
    body = b"<html><head><title>SomeOtherService</title></head><body>not zellij</body></html>"
    with patch.object(zellij_client.urllib.request, "urlopen",
                      return_value=_FakeResp(status=200, body=body)):
        assert zellij_client.web_server_healthy(retries=1) is False


def test_web_server_healthy_returns_false_on_http_500():
    body = b""
    with patch.object(zellij_client.urllib.request, "urlopen",
                      return_value=_FakeResp(status=500, body=body)):
        assert zellij_client.web_server_healthy(retries=1, backoff_seconds=0.0) is False


def test_web_server_healthy_returns_false_on_url_error():
    import urllib.error
    with patch.object(zellij_client.urllib.request, "urlopen",
                      side_effect=urllib.error.URLError("connection refused")):
        assert zellij_client.web_server_healthy(retries=1, backoff_seconds=0.0) is False


# --- ensure_config regeneration backup (audit polish #9) ----------------


def test_ensure_config_backs_up_operator_edits_before_regenerating(tmp_path, monkeypatch):
    """When the existing config is missing a required line, regenerate
    the file AND back up the prior content so operator hand-edits are
    not silently lost."""
    cfg_path = tmp_path / "config.kdl"
    # Operator-edited file: has SOME lines but missing
    # `enforce_https_for_localhost false`.
    operator_edited = (
        "// operator notes\n"
        'web_server true\n'
        'web_server_ip "127.0.0.1"\n'
        f'web_server_port 9097\n'
        'web_sharing "on"\n'
        '// my hand-edit: log_file "/tmp/zellij.log"\n'
    )
    cfg_path.write_text(operator_edited, encoding="utf-8")

    monkeypatch.setattr(zellij_client, "_CONFIG_PATH", cfg_path)

    zellij_client.ensure_config()

    # File regenerated with all required lines.
    new_text = cfg_path.read_text(encoding="utf-8")
    for required in zellij_client._REQUIRED_CONFIG_LINES:
        assert required in new_text, f"required line missing: {required!r}"

    # A backup file exists with the original operator content.
    backups = list(tmp_path.glob("config.kdl.bak.*"))
    assert backups, "no backup file created"
    backup_text = backups[0].read_text(encoding="utf-8")
    assert backup_text == operator_edited


# --- list_sessions EXITED-suffix parsing (live parser bug fix) ----------


def test_list_sessions_parses_exited_resurrectable_suffix():
    """The original regex anchored at `]` matched ZERO rows on a box with
    accumulated `(EXITED - attach to resurrect)` sessions. Confirm both
    running and exited rows parse, with the `exited` flag set correctly."""
    out = (
        "Brandon__terminal__root [Created 3h 59m 23s ago]\n"
        "Brandon__claude__root__1779878318 [Created 26days 15h 14m 55s ago] (EXITED - attach to resurrect)\n"
    )
    with patch.object(zellij_client, "_run", return_value=_mk_completed_process(stdout=out)):
        rows = zellij_client.list_sessions()
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"Brandon__terminal__root", "Brandon__claude__root__1779878318"}
    assert by_name["Brandon__terminal__root"]["exited"] is False
    assert by_name["Brandon__claude__root__1779878318"]["exited"] is True


def test_session_exists_true_for_running_and_exited():
    out = (
        "Brandon__terminal__root [Created 3h ago]\n"
        "Brandon__claude__root [Created 9days ago] (EXITED - attach to resurrect)\n"
    )
    with patch.object(zellij_client, "_run", return_value=_mk_completed_process(stdout=out)):
        assert zellij_client.session_exists("Brandon__terminal__root") is True
        assert zellij_client.session_exists("Brandon__claude__root") is True  # exited still counts
        assert zellij_client.session_exists("Brandon__gemini__root") is False


def test_session_exists_false_on_cli_failure():
    """If list_sessions raises, session_exists must return False (treat
    'can't tell' as absent -> caller falls through to a normal launch)."""
    with patch.object(zellij_client, "list_sessions", side_effect=RuntimeError("daemon down")):
        assert zellij_client.session_exists("Brandon__terminal__root") is False


# --- _run env scrub (ZELLIJ* leak — probe 2026-07-20) --------------------


def test_run_scrubs_zellij_env(monkeypatch):
    """_run must never let the orchestrator's own ZELLIJ_* env leak into
    zellij CLI subprocesses — an inherited ZELLIJ_SESSION_NAME makes a bare
    action target the USER'S live session (probe 2026-07-20)."""
    captured = {}

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "someones-live-session")
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setattr(zellij_client.subprocess, "run", fake_run)
    zellij_client._run([zellij_client._ZELLIJ_BIN, "--session", "x", "action", "write", "13"])
    assert captured["env"] is not None
    assert not any(k.startswith("ZELLIJ") for k in captured["env"])
    assert "PATH" in captured["env"]


# --- paste_into_pane (detached-safe bracketed paste — probe 2026-07-20) --


def test_paste_into_pane_argv(monkeypatch):
    """Argv carries `--` before the payload so leading-dash text can't be
    parsed as a flag (probe 2026-07-20: without it zellij rejects e.g.
    '--verbose ...' with rc=2)."""
    calls = []
    monkeypatch.setattr(zellij_client, "_run", lambda argv, **kw: calls.append(argv))
    zellij_client.paste_into_pane("Brandon__claude__root", 'Read this file: "/a/b c.png" ')
    assert calls[0] == [
        zellij_client._ZELLIJ_BIN, "--session", "Brandon__claude__root",
        "action", "paste", "--pane-id", "terminal_0", "--",
        'Read this file: "/a/b c.png" ',
    ]


def test_paste_into_pane_rejects_bad_session():
    with pytest.raises(ValueError):
        zellij_client.paste_into_pane("bad;name", "x")


def test_paste_into_pane_rejects_empty_text():
    with pytest.raises(ValueError):
        zellij_client.paste_into_pane("Brandon__claude__root", "")


def test_is_valid_session_name():
    assert zellij_client.is_valid_session_name("Brandon__claude__root") is True
    assert zellij_client.is_valid_session_name("bad;name") is False
    assert zellij_client.is_valid_session_name("") is False


# --- master token LOADS (not re-mints) across a simulated restart -------


def test_ensure_master_zellij_token_loads_from_disk_no_remint(tmp_path, monkeypatch):
    """Simulated restart: the master auth token persisted on disk must be
    LOADED (not re-minted) so reattach keeps working across an
    orchestrator restart. mint_token must NOT be called when the file
    already holds a value."""
    token_file = tmp_path / "zellij-master.token"
    token_file.write_text("11111111-2222-3333-4444-555555555555", encoding="utf-8")
    monkeypatch.setattr(zellij_client, "_MASTER_TOKEN_FILE", token_file)
    # Clear the in-process cache so we exercise the disk-load path (the
    # "fresh process after restart" condition).
    monkeypatch.setattr(zellij_client, "_master_token", None)

    with patch.object(zellij_client, "mint_token") as mock_mint:
        value = zellij_client.ensure_master_zellij_token()

    assert value == "11111111-2222-3333-4444-555555555555"
    mock_mint.assert_not_called(), "master token must be loaded, never re-minted, across restart"


def test_ensure_master_zellij_token_mints_only_when_absent(tmp_path, monkeypatch):
    """Fresh install (no token file): mint once + persist."""
    token_file = tmp_path / "sub" / "zellij-master.token"
    monkeypatch.setattr(zellij_client, "_MASTER_TOKEN_FILE", token_file)
    monkeypatch.setattr(zellij_client, "_master_token", None)

    with patch.object(zellij_client, "mint_token",
                      return_value=("master-blackbox", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")) as mock_mint:
        value = zellij_client.ensure_master_zellij_token()

    assert value == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    mock_mint.assert_called_once()
    # Persisted for the NEXT restart to load.
    assert token_file.read_text(encoding="utf-8").strip() == value
