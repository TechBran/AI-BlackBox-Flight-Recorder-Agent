"""Scripts/update.sh, the SSH twin of the in-app updater, follows the same rules.

Audit 2026-10-09 rank 43 (O16-01, O22-09):
  * a tracked file with local edits blocks the reset: --yes exits 2 unless
    --stash is passed, an interactive run asks, and a stash is named so it can
    be found again. Untracked runtime files never block;
  * when install.sh fails, the rollback restores the venvs from the freezes
    the script saved (before, only the code went back), stashing anything the
    failed run left in tracked files first.

The real script runs in a real git box (_gitbox) whose Scripts/install.sh is a
stub. `sudo` is a fake on PATH that records its arguments and never escalates:
install.sh runs as the stub, `systemctl` is recorded and never run. The test
proves the fake is the `sudo` the script will find before it starts it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from ._gitbox import git, head, make_box, push_upstream, stash_entries, tags

REPO = Path(__file__).resolve().parents[3]

INSTALL_STUB = textwrap.dedent("""\
    #!/bin/sh
    # Stand-in for Scripts/install.sh. STUB_TOUCH edits a tracked file first,
    # as a half-finished install could.
    echo "[install-stub] ran"
    if [ -n "$STUB_TOUCH" ]; then echo "touched by install" >> app.py; fi
    exit "${STUB_INSTALL_RC:-0}"
""")

FAKE_SUDO = textwrap.dedent("""\
    #!/bin/sh
    # Test double: records the call and never escalates. systemctl is only
    # recorded, everything else (the install.sh stub) runs as this user.
    echo "$*" >> "$FAKE_SUDO_LOG"
    [ "$1" = "-n" ] && shift
    case "$1" in systemctl) exit 0 ;; esac
    exec "$@"
""")

FAKE_PIP = textwrap.dedent("""\
    #!/bin/sh
    echo "$*" >> "$FAKE_PIP_LOG"
    if [ "$1" = "freeze" ]; then echo "aiohttp==3.13.2"; echo "torch==2.11.0+cpu"; fi
    exit 0
""")


def _write_exec(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def sh(tmp_path):
    def build(*, requirements=False):
        box = make_box(tmp_path, {
            "Scripts/update.sh": (REPO / "Scripts" / "update.sh").read_text(),
            "Scripts/install.sh": INSTALL_STUB,
            "requirements.txt": "aiohttp==3.13.2\n",
        })
        box.v2 = push_upstream(box, requirements=requirements)
        fakebin = tmp_path / "fakebin"
        _write_exec(fakebin / "sudo", FAKE_SUDO)
        box.sudo_log = tmp_path / "sudo.log"
        box.pip_log = tmp_path / "pip.log"
        box.env = {
            "PATH": f"{fakebin}:/usr/bin:/bin",
            "HOME": os.environ.get("HOME", str(tmp_path)),
            "LANG": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
            "FAKE_SUDO_LOG": str(box.sudo_log),
            "FAKE_PIP_LOG": str(box.pip_log),
        }
        # The guard that makes running the real script safe: the only `sudo`
        # it can reach is the recording fake.
        assert shutil.which("sudo", path=box.env["PATH"]) == str(fakebin / "sudo")
        return box
    return build


def run_update(box, *args, stdin="", **env):
    return subprocess.run(
        ["bash", str(box.root / "Scripts" / "update.sh"), *args],
        cwd=str(box.root), env={**box.env, **env}, input=stdin,
        capture_output=True, text=True, timeout=120)


def sudo_calls(box) -> list[str]:
    return box.sudo_log.read_text().splitlines() if box.sudo_log.exists() else []


def test_yes_refuses_a_dirty_tree(sh):
    box = sh()
    (box.root / "app.py").write_text("VERSION = 1  # hand fix\n")
    r = run_update(box, "--yes")

    assert r.returncode == 2, r.stdout + r.stderr
    assert "app.py" in r.stdout and "--stash" in r.stderr
    assert (box.root / "app.py").read_text() == "VERSION = 1  # hand fix\n"
    assert head(box.root) == box.v1
    assert stash_entries(box.root) == [] and tags(box.root) == []
    assert sudo_calls(box) == [], "nothing may run once the tree is refused"


def test_interactive_no_keeps_the_edit(sh):
    box = sh()
    (box.root / "app.py").write_text("VERSION = 1  # hand fix\n")
    r = run_update(box, stdin="n")
    assert r.returncode == 2, r.stdout + r.stderr
    assert (box.root / "app.py").read_text() == "VERSION = 1  # hand fix\n"
    assert head(box.root) == box.v1 and sudo_calls(box) == []


def test_status_lists_local_edits_and_changes_nothing(sh):
    box = sh()
    (box.root / "app.py").write_text("VERSION = 1  # hand fix\n")
    r = run_update(box, "--status")
    assert r.returncode == 0
    assert "app.py" in r.stdout
    assert head(box.root) == box.v1 and stash_entries(box.root) == []


def test_stash_flag_stashes_names_the_stash_and_updates(sh):
    box = sh()
    (box.root / "app.py").write_text("VERSION = 1  # hand fix\n")
    r = run_update(box, "--yes", "--stash")

    assert r.returncode == 0, r.stdout + r.stderr
    assert head(box.root) == box.v2
    entries = stash_entries(box.root)
    assert len(entries) == 1 and "auto-stash-pre-update-" in entries[0]
    assert "hand fix" in git(box.root, "stash", "show", "-p", "stash@{0}")
    assert git(box.root, "rev-parse", "stash@{0}") in r.stdout
    assert "systemctl restart blackbox.service" in sudo_calls(box)


def test_untracked_files_never_block(sh):
    box = sh()
    (box.root / "scratch.log").write_text("runtime\n")
    r = run_update(box, "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert head(box.root) == box.v2
    assert stash_entries(box.root) == []
    assert (box.root / "scratch.log").read_text() == "runtime\n"


def test_failed_install_restores_the_venv_freeze_then_the_code(sh):
    box = sh(requirements=True)
    _write_exec(box.root / "Orchestrator" / "venv" / "bin" / "pip", FAKE_PIP)
    r = run_update(box, "--yes", STUB_INSTALL_RC="1")

    assert r.returncode == 1, r.stdout + r.stderr
    assert head(box.root) == box.v1, "code must be rolled back"
    pip_calls = box.pip_log.read_text().splitlines()
    assert pip_calls[0] == "freeze"
    restores = [c for c in pip_calls if c.startswith("install -r ")]
    assert len(restores) == 1 and "--force-reinstall" in restores[0]
    restore_file = Path(restores[0].split()[2])
    if not restore_file.is_absolute():
        restore_file = box.root / restore_file
    # The same rule as the in-app runner: a local-version pin (+cpu) comes
    # from another index and would make pip refuse the whole file.
    assert restore_file.read_text() == "aiohttp==3.13.2\n"
    assert "systemctl restart blackbox.service" not in sudo_calls(box)


def test_failed_install_without_requirement_changes_skips_the_venv_restore(sh):
    box = sh()
    _write_exec(box.root / "Orchestrator" / "venv" / "bin" / "pip", FAKE_PIP)
    r = run_update(box, "--yes", STUB_INSTALL_RC="1")
    assert r.returncode == 1
    assert head(box.root) == box.v1
    assert [c for c in box.pip_log.read_text().splitlines()
            if c.startswith("install")] == []


def test_failed_install_stashes_what_it_left_before_the_rollback_reset(sh):
    box = sh()
    r = run_update(box, "--yes", STUB_INSTALL_RC="1", STUB_TOUCH="1")
    assert r.returncode == 1, r.stdout + r.stderr
    assert head(box.root) == box.v1
    entries = stash_entries(box.root)
    assert entries and "auto-stash-rollback-pre-update-" in entries[0]
    assert "touched by install" in git(box.root, "stash", "show", "-p", "stash@{0}")
