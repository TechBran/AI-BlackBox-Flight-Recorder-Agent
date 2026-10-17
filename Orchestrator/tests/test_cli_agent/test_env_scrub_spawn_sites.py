"""Every CLI-agent / terminal spawn site builds its child env through the shared
scrub (audit O14-cli-agents-devices-02 / -03).

Each test plants fake server credentials in the orchestrator's own environment
(exactly what config.py's load_dotenv() does with the real .env), drives ONE
spawn site, and asserts the child it launched never receives them. The tmux
tests run a private tmux server on a throwaway socket and read the env the
pane actually got, so they prove the leak is closed, not just that a helper ran.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

# Fake values; the names are the real ones the BlackBox .env carries.
_PLANTED = {
    "ANTHROPIC_API_KEY": "fake-anthropic-planted",
    "OPENAI_API_KEY": "fake-openai-planted",
    "GOOGLE_API_KEY": "fake-google-planted",
    "XAI_API_KEY": "fake-xai-planted",
    "TWILIO_AUTH_TOKEN": "fake-twilio-planted",
    "ASTERISK_AMI_SECRET": "fake-ami-planted",
    "ASTERISK_ARI_PASSWORD": "fake-ari-planted",
    "TELEPHONY_SECRET_KEY": "fake-telephony-planted",
}


@pytest.fixture
def planted(monkeypatch):
    for k, v in _PLANTED.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "someones-live-session")
    return _PLANTED


def _assert_clean(env: dict) -> None:
    assert env is not None, "spawn site passed no env (would inherit os.environ)"
    leaked = sorted(k for k in _PLANTED if k in env)
    assert leaked == [], f"server secrets reached the child env: {leaked}"
    for v in _PLANTED.values():
        assert v not in env.values()
    assert not any(k.startswith("ZELLIJ") for k in env)
    assert env.get("PATH"), "the scrub must keep PATH"


def _assert_clean_text(text: str) -> None:
    for name, v in _PLANTED.items():
        assert v not in text, f"{name}'s value reached the child"


# --- headless runner (claude_code_task / gemini_cli_task / codex_cli_task) ---


@pytest.mark.parametrize("provider", ["claude", "gemini", "codex"])
def test_headless_build_child_env_scrubs_every_secret(planted, provider):
    from Orchestrator.cli_agent import headless
    _assert_clean(headless.build_child_env(provider))


# --- zellij ------------------------------------------------------------------


def test_zellij_launch_session_env_is_scrubbed(planted, monkeypatch):
    from Orchestrator.cli_agent import zellij_client
    captured = {}

    class _Proc:
        stderr = None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("zellij", timeout)

        def terminate(self):
            pass

        def kill(self):
            pass

    def fake_popen(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return _Proc()

    monkeypatch.setattr(zellij_client.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(zellij_client.pty, "openpty", lambda: (40, 41))
    monkeypatch.setattr(zellij_client.os, "close", lambda fd: None)
    zellij_client.launch_session("op__terminal__1", binary=None)
    _assert_clean(captured["env"])


def test_zellij_cli_calls_are_scrubbed(planted, monkeypatch):
    """Any zellij CLI call can START a session server (an `action paste` against
    an exited session resurrects it), so _run's env must be scrubbed too."""
    from Orchestrator.cli_agent import zellij_client
    captured = {}

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(zellij_client.subprocess, "run", fake_run)
    zellij_client.paste_into_pane("op__claude__1", "hello")
    _assert_clean(captured["env"])


# --- tmux backend (real tmux on a private socket) -----------------------------

_needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


@pytest.fixture
def tmux_mgr(tmp_path):
    from Orchestrator.cli_agent.operator_config import OperatorConfig
    from Orchestrator.cli_agent.path_validator import PathValidator
    from Orchestrator.cli_agent.session_manager import TmuxSessionManager

    sockdir = tempfile.mkdtemp(prefix="bbx-scrub-")  # short path: AF_UNIX limit
    sock = Path(sockdir) / "s"
    apps = tmp_path / "Apps"
    (apps / "demo").mkdir(parents=True)
    mgr = TmuxSessionManager(
        path_validator=PathValidator(apps_root=apps),
        operator_config=OperatorConfig(root=tmp_path / "cfg"),
        tmux_socket=sock,
    )
    yield mgr, sock
    subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True)
    shutil.rmtree(sockdir, ignore_errors=True)


def _wait_for(path: Path, timeout: float = 8.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            time.sleep(0.1)  # let the writer finish
            return path.read_text()
        time.sleep(0.05)
    raise AssertionError(f"pane never wrote {path}")


def _global_env(sock: Path) -> str:
    return subprocess.run(["tmux", "-S", str(sock), "show-environment", "-g"],
                          capture_output=True, text=True).stdout


@_needs_tmux
def test_tmux_pane_and_server_env_are_scrubbed(planted, tmux_mgr, tmp_path):
    mgr, sock = tmux_mgr
    out = tmp_path / "pane-env.txt"
    info = mgr.attach_or_create(operator="op", provider="codex", app="demo",
                                command=["bash", "-c", f"env > {out}; sleep 600"])
    assert info.created
    _assert_clean_text(_wait_for(out))
    _assert_clean_text(_global_env(sock))


@_needs_tmux
def test_tmux_new_session_on_a_stale_server_is_scrubbed(planted, tmux_mgr, tmp_path):
    """A tmux server started by the OLD code holds every secret in its global
    environment and hands it to every new window. Creating a session through
    the manager must purge those names first, so new panes are clean even
    before the operator kills the stale server."""
    mgr, sock = tmux_mgr
    stale_env = {**os.environ}  # what the pre-fix code launched tmux with
    subprocess.run(["tmux", "-S", str(sock), "new-session", "-d", "-s", "stale", "sleep 600"],
                   env=stale_env, check=True, capture_output=True)
    assert "fake-twilio-planted" in _global_env(sock)  # control: really stale

    out = tmp_path / "pane-env.txt"
    mgr.attach_or_create(operator="op", provider="codex", app="demo",
                         command=["bash", "-c", f"env > {out}; sleep 600"])
    _assert_clean_text(_wait_for(out))
    _assert_clean_text(_global_env(sock))


def test_tmux_client_commands_are_scrubbed(planted, monkeypatch, tmp_path):
    from Orchestrator.cli_agent import session_manager as sm
    from Orchestrator.cli_agent.operator_config import OperatorConfig
    from Orchestrator.cli_agent.path_validator import PathValidator
    seen = []

    def fake_run(argv, **kwargs):
        seen.append(kwargs.get("env"))
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    monkeypatch.setattr(sm.subprocess, "run", fake_run)
    mgr = sm.TmuxSessionManager(path_validator=PathValidator(apps_root=tmp_path),
                                operator_config=OperatorConfig(root=tmp_path / "c"))
    mgr.has_session("x")
    mgr.kill("x")
    mgr.list_for_operator("op")
    assert seen
    for env in seen:
        _assert_clean(env)


def test_tmux_attach_pty_env_is_scrubbed(planted):
    """The WS bridge's `tmux attach` client: tmux copies selected client vars
    into the session on attach, so it must not carry secrets either."""
    from Orchestrator.routes import cli_agent_routes
    env = cli_agent_routes._attach_pty_env("op")
    _assert_clean(env)
    assert env.get("TERM")


@pytest.mark.asyncio
async def test_pty_bridge_default_env_is_scrubbed(planted):
    from Orchestrator.cli_agent.pty_bridge import PtyBridge
    bridge = PtyBridge.spawn(["env"])
    try:
        out = b""
        for _ in range(40):
            chunk = await bridge.read(timeout=0.25)
            if chunk:
                out += chunk
            elif out and not bridge.isalive():
                break
        text = out.decode(errors="replace")
        assert "PATH=" in text  # control: the child printed its environment
        _assert_clean_text(text)
    finally:
        bridge.close()


# --- legacy agent-mode CLIs (Portal agent WS, phone bridge, Gemini agent) -----


class _FakeChild:
    def __init__(self):
        r, w = os.pipe()
        os.close(w)
        self.stdout = os.fdopen(r, "rb")
        self.pid = 4242


def test_agent_mode_claude_streaming_env_is_scrubbed(planted, monkeypatch, tmp_path):
    """claude must use its subscription login here, not bill the server key."""
    from Orchestrator.routes import agent_routes
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeChild()

    monkeypatch.setattr(agent_routes.subprocess, "Popen", fake_popen)
    proc = agent_routes.create_claude_streaming(str(tmp_path), "hi")
    proc.stdout.close()
    _assert_clean(captured["env"])
    assert os.path.expanduser("~/.local/bin") in captured["env"]["PATH"]


def test_gemini_agent_mode_keeps_only_its_one_key(planted, monkeypatch, tmp_path):
    """The legacy Gemini agent route has always authenticated the gemini CLI
    with the server's configured key. It keeps exactly that ONE variable and
    loses everything else."""
    from Orchestrator.routes import gemini_agent_routes
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeChild()

    monkeypatch.setattr(gemini_agent_routes, "GEMINI_API_KEY", "fake-configured-gemini")
    monkeypatch.setattr(gemini_agent_routes.subprocess, "Popen", fake_popen)
    proc = gemini_agent_routes.create_gemini_streaming(str(tmp_path), "hi")
    proc.stdout.close()
    env = captured["env"]
    _assert_clean(env)
    assert env["GEMINI_API_KEY"] == "fake-configured-gemini"


# --- onboarding wizard terminals / diagnostics --------------------------------


def test_wizard_terminal_env_is_scrubbed(planted):
    """/onboarding/cli-agent/spawn-terminal runs the CLI install/login command
    in a desktop terminal built from this env."""
    from Orchestrator.routes import onboarding_routes
    env = onboarding_routes._build_user_env()
    _assert_clean(env)
    assert env["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:path=")


def test_claude_doctor_runs_claude_with_a_scrubbed_env(planted, monkeypatch, tmp_path):
    """The doctor's `claude --print` must not bill the server key either."""
    from Orchestrator.routes import cli_agent_routes, onboarding_routes
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli_agent_routes, "provider_bin", lambda name: "/bin/true")
    envs = []

    def fake_run(cmd, **kwargs):
        envs.append(kwargs.get("env"))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    onboarding_routes.cli_agent_claude_doctor()
    assert envs
    for env in envs:
        _assert_clean(env)
