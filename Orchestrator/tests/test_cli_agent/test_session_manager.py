import shutil
import subprocess
import tempfile
from pathlib import Path
import pytest

from Orchestrator.cli_agent.session_manager import (
    TmuxSessionManager, session_name, parse_session_name
)
from Orchestrator.cli_agent.path_validator import PathValidator, WorkspaceViolation
from Orchestrator.cli_agent.operator_config import OperatorConfig


# The tests that start a real tmux server (audit O21-python-tests-10).
needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


@pytest.fixture
def tmux_sock():
    """A private tmux server socket per test: a fixed shared /tmp socket let two
    concurrent runs kill each other's server. A short mkdtemp path, since a unix
    socket path is capped near 108 bytes and tmp_path can exceed it."""
    sock_dir = Path(tempfile.mkdtemp(prefix="bbx-tmux-"))
    sock = sock_dir / "tmux.sock"
    yield sock
    subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True)
    shutil.rmtree(sock_dir, ignore_errors=True)


@pytest.fixture
def workspace(tmp_path):
    apps = tmp_path / "Apps"
    (apps / "grocery-store").mkdir(parents=True)
    return apps


@pytest.fixture
def manager(workspace, tmp_path, tmux_sock):
    pv = PathValidator(apps_root=workspace)
    oc = OperatorConfig(root=tmp_path / ".claude-bbox")
    return TmuxSessionManager(
        path_validator=pv, operator_config=oc, tmux_socket=tmux_sock
    )


def test_session_name_deterministic():
    n1 = session_name("Brandon", "claude", "grocery-store")
    n2 = session_name("Brandon", "claude", "grocery-store")
    assert n1 == n2 == "cli-agent-Brandon__claude__grocery-store"


def test_session_name_apps_root_uses_underscore_root():
    assert session_name("Brandon", "claude", "") == "cli-agent-Brandon__claude___root"


def test_parse_session_name_round_trips():
    name = "cli-agent-Brandon__claude__grocery-store"
    op, prov, app = parse_session_name(name)
    assert (op, prov, app) == ("Brandon", "claude", "grocery-store")


@needs_tmux
def test_attach_or_create_creates_new(manager, workspace):
    info = manager.attach_or_create(
        operator="Brandon", provider="claude", app="grocery-store",
        command=["bash", "-c", "echo hello; sleep 600"]
    )
    assert info.created is True
    assert info.name == "cli-agent-Brandon__claude__grocery-store"
    out = subprocess.run(
        ["tmux", "-S", str(manager.socket), "has-session", "-t", info.name],
        capture_output=True
    )
    assert out.returncode == 0


@needs_tmux
def test_attach_or_create_idempotent(manager, workspace):
    a = manager.attach_or_create(
        operator="Brandon", provider="claude", app="grocery-store",
        command=["bash", "-c", "sleep 600"]
    )
    b = manager.attach_or_create(
        operator="Brandon", provider="claude", app="grocery-store",
        command=["bash", "-c", "sleep 600"]
    )
    assert a.name == b.name
    assert a.created is True
    assert b.created is False


@needs_tmux
def test_kill_session_works(manager, workspace):
    manager.attach_or_create(
        operator="Brandon", provider="claude", app="grocery-store",
        command=["bash", "-c", "sleep 600"]
    )
    name = "cli-agent-Brandon__claude__grocery-store"
    manager.kill(name)
    out = subprocess.run(
        ["tmux", "-S", str(manager.socket), "has-session", "-t", name],
        capture_output=True
    )
    assert out.returncode != 0


@needs_tmux
def test_list_sessions_for_operator(manager, workspace):
    manager.attach_or_create(operator="Brandon", provider="claude",
                              app="grocery-store",
                              command=["bash", "-c", "sleep 600"])
    sessions = manager.list_for_operator("Brandon")
    names = [s.name for s in sessions]
    assert "cli-agent-Brandon__claude__grocery-store" in names


def test_path_violation_rejects_before_tmux(manager):
    with pytest.raises(WorkspaceViolation):
        manager.attach_or_create(
            operator="Brandon", provider="claude", app="../../etc",
            command=["bash"]
        )


def test_session_name_handles_hyphenated_operator():
    # Brandon-DEV is a real BlackBox operator; the field separator must
    # not collide with hyphens inside operator names.
    n = session_name("Brandon-DEV", "claude", "grocery-store")
    assert n == "cli-agent-Brandon-DEV__claude__grocery-store"
    op, prov, app = parse_session_name(n)
    assert (op, prov, app) == ("Brandon-DEV", "claude", "grocery-store")


def test_session_name_apps_root_round_trips():
    # Apps-root pick (empty app slug) must round-trip cleanly.
    name = session_name("Brandon", "claude", "")
    op, prov, app = parse_session_name(name)
    assert (op, prov, app) == ("Brandon", "claude", "")


def test_session_name_rejects_field_separator_in_operator():
    # Operator names containing the field separator break the parse.
    # Validate at name-construction time.
    with pytest.raises(ValueError):
        session_name("Bran__don", "claude", "grocery-store")
