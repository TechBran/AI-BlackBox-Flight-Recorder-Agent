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
import pty
import select
import shutil
import subprocess
import textwrap
import time
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
    # An intent-to-add entry: `git stash` refuses it ("Cannot save the
    # current worktree state"), so the rollback cannot keep it.
    if [ -n "$STUB_ITA" ]; then echo "half-added" > ita.txt; git add -N ita.txt; fi
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
    if [ "$1" = "freeze" ]; then
        # FREEZE_TOUCH: a tracked file is edited while the venvs are frozen.
        if [ -n "$FREEZE_TOUCH" ]; then echo "edit saved during the freeze" > notes.txt; fi
        echo "aiohttp==3.13.2"; echo "torch==2.11.0+cpu"
    fi
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


# ── review follow-ups ────────────────────────────────────────────────────

def run_on_a_tty(box, steps, *args, **env):
    """Run update.sh on a pseudo-terminal, so its `read -p` prompts show.
    `steps` is a list of (prompt text, action): wait for the prompt, run
    `action()` when it is callable, then type `answer`. Returns (rc, output)."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        ["bash", str(box.root / "Scripts" / "update.sh"), *args], cwd=str(box.root),
        env={**box.env, "GIT_PAGER": "cat", "PAGER": "cat", "TERM": "dumb", **env},
        stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    out, seen = "", 0

    def read_more(deadline):
        nonlocal out
        ready, _, _ = select.select([master], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            return False
        try:
            chunk = os.read(master, 4096)
        except OSError:  # EIO: the script exited and closed the terminal
            chunk = b""
        out += chunk.decode("utf-8", "replace")
        return bool(chunk)

    try:
        for prompt, action, answer in steps:
            deadline = time.monotonic() + 30
            while prompt not in out[seen:]:
                assert read_more(deadline), f"no {prompt!r} prompt; got:\n{out}"
            seen = out.index(prompt, seen) + len(prompt)
            if action:
                action()
            os.write(master, answer.encode())
        deadline = time.monotonic() + 60
        while proc.poll() is None or read_more(time.monotonic() + 0.2):
            if time.monotonic() > deadline:
                raise AssertionError(f"update.sh did not finish; got:\n{out}")
            read_more(time.monotonic() + 0.2)
        return proc.wait(timeout=5), out
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)


APPLY = "Apply this update? (y/N)"
STASH = "Stash these edits and continue? (y/N)"


def test_an_edit_made_while_the_apply_prompt_is_open_is_asked_about(sh):
    """The tree was checked before the operator read the changelog at the
    prompt; an edit saved meanwhile was reset over with no stash (the base
    script checked after the prompt). Declining keeps it, and nothing moved."""
    box = sh()
    edit = lambda: (box.root / "notes.txt").write_text("edit saved while the prompt was open\n")
    rc, out = run_on_a_tty(box, [(APPLY, edit, "y"), (STASH, None, "n")])

    assert rc == 2, out
    assert "notes.txt" in out
    assert (box.root / "notes.txt").read_text() == "edit saved while the prompt was open\n"
    assert head(box.root) == box.v1
    assert stash_entries(box.root) == [] and tags(box.root) == []
    assert sudo_calls(box) == []


def test_agreeing_after_the_apply_prompt_stashes_that_edit(sh):
    box = sh()
    edit = lambda: (box.root / "notes.txt").write_text("edit saved while the prompt was open\n")
    rc, out = run_on_a_tty(box, [(APPLY, edit, "y"), (STASH, None, "y")])

    assert rc == 0, out
    assert head(box.root) == box.v2
    assert "edit saved while the prompt was open" in git(box.root, "stash", "show", "-p", "stash@{0}")


def test_an_edit_made_during_the_freeze_is_stashed_not_reset_over(sh):
    box = sh()
    _write_exec(box.root / "Orchestrator" / "venv" / "bin" / "pip", FAKE_PIP)
    r = run_update(box, "--yes", FREEZE_TOUCH="1")

    assert r.returncode == 0, r.stdout + r.stderr
    assert head(box.root) == box.v2
    entries = stash_entries(box.root)
    assert len(entries) == 1 and "auto-stash-pre-update-" in entries[0]
    assert "edit saved during the freeze" in git(box.root, "stash", "show", "-p", "stash@{0}")
    assert git(box.root, "rev-parse", "stash@{0}") in r.stdout


def test_an_untracked_file_where_the_update_adds_one_is_moved_aside(sh):
    box = sh()
    (box.seed / "docs").mkdir()
    (box.seed / "docs" / "audit.md").write_text("upstream version\n")
    git(box.seed, "add", "-A")
    git(box.seed, "commit", "-q", "-m", "v3 adds docs/audit.md")
    git(box.seed, "push", "-q", "origin", "main")
    (box.root / "docs").mkdir()
    (box.root / "docs" / "audit.md").write_text("operator's only copy\n")
    r = run_update(box, "--yes")

    assert r.returncode == 0, r.stdout + r.stderr
    assert (box.root / "docs" / "audit.md").read_text() == "upstream version\n"
    kept = list((box.root / "Manifest" / "displaced").glob("pre-update-*/docs/audit.md"))
    assert len(kept) == 1 and kept[0].read_text() == "operator's only copy\n"
    assert "docs/audit.md" in r.stdout


def test_a_rollback_that_cannot_stash_leaves_the_code_and_says_so(sh):
    box = sh()
    r = run_update(box, "--yes", STUB_INSTALL_RC="1", STUB_ITA="1")
    assert r.returncode == 1, r.stdout + r.stderr
    assert head(box.root) == box.v2, "no reset may run once the stash failed"
    assert (box.root / "ita.txt").read_text() == "half-added\n"
    assert "NOT rolled back" in r.stderr
    assert "systemctl restart blackbox.service" not in sudo_calls(box)


def test_the_freeze_names_the_run_it_belongs_to(sh):
    """The in-app Rollback reinstalls a freeze only for the tag it rolls back
    to; this script writes the same file for its own tag."""
    box = sh()
    _write_exec(box.root / "Orchestrator" / "venv" / "bin" / "pip", FAKE_PIP)
    r = run_update(box, "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    (tag,) = tags(box.root)
    freeze = (box.root / "Manifest" / "pre_update_pip_freeze.txt").read_text()
    assert freeze.splitlines()[0] == f"# blackbox-freeze-for: {tag}"
    assert "aiohttp==3.13.2" in freeze.splitlines()
