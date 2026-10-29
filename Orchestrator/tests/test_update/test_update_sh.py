"""Scripts/update.sh, the SSH twin of the in-app updater, follows the same rules.

Audit 2026-10-09 rank 43 (O16-01, O22-09):
  * a tracked file with local edits blocks the reset: --yes exits 2 unless
    --stash is passed, an interactive run asks, and a stash is named so it can
    be found again. Untracked runtime files never block;
  * when install.sh fails, the rollback restores the venvs from the freezes
    the script saved (before, only the code went back), stashing anything the
    failed run left in tracked files first.

Audit 2026-10-09 rank 79 (X04-security-clients-infra-11/-12):
  * the origin must be one the root-owned /etc/blackbox/update-origin pins,
    checked before the fetch, and once /etc/blackbox/update-allowed-signers
    holds a key the commit installed must be SSH-signed by it (exit 4, nothing
    changed). The reset installs the verified SHA, not whatever origin/main
    names by then;
  * a failed install.sh is rolled back further: the stash taken for the update
    is put back and the old code republishes the root-owned template store;
  * on a box whose checkout belongs to a dedicated service account, git and
    pip run as that account (only install.sh and the restart run as root).

The real script runs in a real git box (_gitbox) whose Scripts/install.sh is a
stub. `sudo` is a fake on PATH that records its arguments and never escalates:
install.sh runs as the stub, `systemctl` is recorded and never run, `-u <user>`
runs the command as this user. The test proves the fake is the `sudo` the
script will find before it starts it. The script's copy reads its trust pins
from the per-test trust directory (conftest.py), never /etc/blackbox.
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

from ._gitbox import (git, head, make_box, make_signing_key, make_zip_box,
                      overwrite_loose_object, plant_commit, push_signed, push_upstream,
                      smudge_filter, stash_entries, tags, tree_with, write_pins)

REPO = Path(__file__).resolve().parents[3]

INSTALL_STUB = textwrap.dedent("""\
    #!/bin/sh
    # Stand-in for Scripts/install.sh. STUB_TOUCH edits a tracked file first,
    # as a half-finished install could.
    case "$1" in
        --publish-store-only) echo "[install-stub] store republished"; exit 0 ;;
    esac
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
    # recorded, `-u <user> --` runs the rest as this user, and everything else
    # (the install.sh stub) runs as this user.
    echo "$*" >> "$FAKE_SUDO_LOG"
    [ "$1" = "-n" ] && shift
    if [ "$1" = "-u" ]; then shift 2; [ "$1" = "--" ] && shift; exec "$@"; fi
    case "$1" in systemctl) exit 0 ;; esac
    exec "$@"
""")

TREE_CHECK_REL = "Orchestrator/update/tree_check.py"
# The global options every update.sh git call carries (tree_check.GIT_SAFE_OPTIONS).
SAFE = "--no-replace-objects --no-pager -c core.hooksPath=/dev/null -c core.fsmonitor=false"

# The constants update.sh reads its trust pins and the checkout's owner from.
TRUST_DIR_LINE = "UPDATE_TRUST_DIR=/etc/blackbox\n"
TRUST_UID_LINE = "UPDATE_TRUST_UID=0\n"
TREE_OWNER_LINE = 'TREE_OWNER="$(stat -c %U "$BLACKBOX_ROOT")"\n'


def sandboxed_update_sh(trust_dir, tree_owner=None) -> str:
    """Scripts/update.sh, reading its pins from `trust_dir` (owned by the test
    user) instead of /etc/blackbox, and, when asked, believing the checkout
    belongs to `tree_owner`."""
    script = (REPO / "Scripts" / "update.sh").read_text()
    script = script.replace(TRUST_DIR_LINE, f"UPDATE_TRUST_DIR={trust_dir}\n", 1)
    script = script.replace(TRUST_UID_LINE, f"UPDATE_TRUST_UID={os.getuid()}\n", 1)
    if tree_owner:
        assert TREE_OWNER_LINE in script
        script = script.replace(TREE_OWNER_LINE, f"TREE_OWNER={tree_owner}\n", 1)
    return script

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
def sh(tmp_path, trust):
    def build(*, requirements=False, install_stub=INSTALL_STUB, tree_owner=None):
        box = make_box(tmp_path, {
            "Scripts/update.sh": sandboxed_update_sh(trust, tree_owner),
            "Scripts/install.sh": install_stub,
            # The checker update.sh copies before the reset (the real one).
            TREE_CHECK_REL: (REPO / TREE_CHECK_REL).read_text(),
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


def test_a_file_where_the_update_needs_a_directory_is_moved_aside(sh):
    box = sh()
    (box.seed / "docs").mkdir()
    (box.seed / "docs" / "a.md").write_text("upstream a\n")
    (box.seed / "docs" / "b.md").write_text("upstream b\n")
    git(box.seed, "add", "-A")
    git(box.seed, "commit", "-q", "-m", "v3 adds docs/")
    git(box.seed, "push", "-q", "origin", "main")
    (box.root / "docs").write_text("operator's notes in a file called docs\n")
    r = run_update(box, "--yes")

    assert r.returncode == 0, r.stdout + r.stderr
    assert (box.root / "docs" / "a.md").read_text() == "upstream a\n"
    kept = list((box.root / "Manifest" / "displaced").glob("pre-update-*/docs"))
    assert len(kept) == 1 and kept[0].read_text() == "operator's notes in a file called docs\n"


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


def _zip_box_behind(box):
    """A ZIP-installed box at v2 (see make_zip_box) with v3 upstream."""
    root = make_zip_box(box, box.v2)
    (box.seed / "app.py").write_text("VERSION = 3\n")
    git(box.seed, "commit", "-q", "-am", "v3")
    git(box.seed, "push", "-q", "origin", "main")
    return root


def run_in(root, box, *args):
    return subprocess.run(["bash", str(root / "Scripts" / "update.sh"), *args], cwd=str(root),
                          env=box.env, capture_output=True, text=True, timeout=120)


def test_a_zip_install_box_is_not_refused_for_files_it_never_changed(sh):
    """install.sh Step 0a never fills the index, so `git status` called every
    file a staged deletion and --yes refused every first update."""
    box = sh()
    root = _zip_box_behind(box)
    r = run_in(root, box, "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (root / "app.py").read_text() == "VERSION = 3\n"
    assert stash_entries(root) == []


def test_a_zip_install_box_lists_only_its_real_edits(sh):
    box = sh()
    root = _zip_box_behind(box)
    (root / "notes.txt").write_text("customer edit\n")
    r = run_in(root, box, "--yes")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "notes.txt" in r.stdout and "app.py" not in r.stdout
    assert (root / "notes.txt").read_text() == "customer edit\n"
    assert git(root, "ls-files") == "", "the box's own index stays as install.sh left it"
    assert not list((root / ".git").glob("blackbox-status-*"))


# ── pinned origin + signed releases (audit rank 79, X04-security-clients-infra-11) ──

def test_the_trust_pins_are_root_owned_constants_not_environment(sh):
    """The copy under test can only be pointed at a sandbox by rewriting these
    two lines: nothing in the operator's environment moves them."""
    script = (REPO / "Scripts" / "update.sh").read_text()
    assert script.count(TRUST_DIR_LINE) == 1 and script.count(TRUST_UID_LINE) == 1
    assert "${UPDATE_TRUST_DIR:-" not in script and "${UPDATE_TRUST_UID:-" not in script


@pytest.mark.parametrize("url", [
    "https://github.com/TechBran/AI-BlackBox-Flight-Recorder-Agent.git",
    "  https://github.com/TechBran/ai-blackbox-flight-recorder-agent.GIT/  ",
    "https://github.com/TechBran/ai-blackbox-flight-recorder-agent",
    "https://github.com/TechBran/blackbox-poc.git",
    "git@github.com:TechBran/ai-blackbox-flight-recorder-agent.git",
    "/srv/mirrors/blackbox.git//",
])
def test_update_sh_compares_origins_exactly_like_the_in_app_runner(url):
    import re
    from Orchestrator.update import verify
    script = (REPO / "Scripts" / "update.sh").read_text()
    fn = re.search(r"^normalize_origin\(\) \{.*?^\}", script, re.S | re.M).group(0)
    out = subprocess.run(["bash", "-c", fn + '\nnormalize_origin "$1"', "_", url],
                         capture_output=True, text=True, check=True).stdout
    assert out == verify.normalize_origin(url) + "\n"


def test_an_origin_the_pin_does_not_list_is_refused_before_the_fetch(sh, trust):
    box = sh()
    write_pins(trust, origins=["https://github.com/example/the-real-repo.git"])
    r = run_update(box, "--yes")

    assert r.returncode == 4, r.stdout + r.stderr
    assert "the-real-repo" in r.stderr and str(box.origin) in r.stderr
    assert git(box.root, "rev-parse", "origin/main") == box.v1, "nothing may be fetched"
    assert head(box.root) == box.v1 and tags(box.root) == [] and stash_entries(box.root) == []
    assert sudo_calls(box) == []


def test_status_reports_a_refused_origin(sh, trust):
    box = sh()
    write_pins(trust, origins=["https://github.com/example/the-real-repo.git"])
    r = run_update(box, "--status")
    assert r.returncode == 4, r.stdout + r.stderr
    assert head(box.root) == box.v1


def test_a_pinned_origin_updates_whatever_its_case(sh, trust):
    box = sh()
    write_pins(trust, origins=[str(box.origin).upper()])
    r = run_update(box, "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert head(box.root) == box.v2


def test_an_unpinned_box_updates_with_a_warning(sh):
    box = sh()
    r = run_update(box, "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert head(box.root) == box.v2
    assert "no update origin is pinned" in r.stderr
    assert "no release signing key is pinned" in r.stderr


def test_with_a_pinned_key_an_unsigned_commit_is_refused(sh, trust, tmp_path):
    box = sh()
    key = make_signing_key(tmp_path / "keys")
    write_pins(trust, origins=[str(box.origin)], signers=[key.line])
    r = run_update(box, "--yes")

    assert r.returncode == 4, r.stdout + r.stderr
    assert "not signed with a release key" in r.stderr
    assert head(box.root) == box.v1 and tags(box.root) == [] and sudo_calls(box) == []


def test_a_commit_signed_by_another_key_is_refused(sh, trust, tmp_path):
    box = sh()
    key = make_signing_key(tmp_path / "keys")
    push_signed(box, make_signing_key(tmp_path / "stranger", "stranger"))
    write_pins(trust, signers=[key.line])
    r = run_update(box, "--yes")
    assert r.returncode == 4, r.stdout + r.stderr
    assert "not from a pinned release key" in r.stderr
    assert head(box.root) == box.v1 and sudo_calls(box) == []


def test_a_commit_signed_by_the_pinned_key_is_installed(sh, trust, tmp_path):
    box = sh()
    key = make_signing_key(tmp_path / "keys")
    signed = push_signed(box, key)
    write_pins(trust, origins=[str(box.origin)], signers=[key.line])
    r = run_update(box, "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert head(box.root) == signed
    assert "WARNING: no" not in r.stderr


def test_a_planted_allowed_signers_file_in_git_config_is_ignored(sh, trust, tmp_path):
    box = sh()
    key = make_signing_key(tmp_path / "keys")
    attacker = make_signing_key(tmp_path / "attacker", "attacker")
    push_signed(box, attacker)
    planted = tmp_path / "attacker_allowed_signers"
    planted.write_text(attacker.line + "\n")
    git(box.root, "config", "gpg.ssh.allowedSignersFile", str(planted))
    write_pins(trust, signers=[key.line])
    r = run_update(box, "--yes")
    assert r.returncode == 4, r.stdout + r.stderr
    assert head(box.root) == box.v1 and sudo_calls(box) == []


def test_a_pin_the_service_could_write_is_not_a_pin(sh, trust):
    box = sh()
    write_pins(trust, origins=[str(box.origin)])
    (trust / "update-origin").chmod(0o666)
    r = run_update(box, "--yes")
    assert r.returncode == 4, r.stdout + r.stderr
    assert "not a trusted pin" in r.stderr
    assert head(box.root) == box.v1


MOVING_PIP = textwrap.dedent("""\
    #!/bin/sh
    # pip freeze that re-points origin/main while the venvs are frozen: after
    # the version check, before the reset.
    if [ "$1" = "freeze" ] && [ -n "$MOVE_ORIGIN_TO" ]; then
        git update-ref refs/remotes/origin/main "$MOVE_ORIGIN_TO"
        echo "aiohttp==3.13.2"
    fi
    exit 0
""")


def test_the_reset_installs_the_checked_commit_even_if_origin_main_moves(sh):
    box = sh()
    (box.seed / "app.py").write_text("VERSION = 666\n")
    git(box.seed, "commit", "-q", "-am", "kept off main")
    evil = git(box.seed, "rev-parse", "HEAD")
    git(box.seed, "push", "-q", "origin", "HEAD:refs/heads/evil")
    git(box.root, "fetch", "-q", "origin", "evil")
    _write_exec(box.root / "Orchestrator" / "venv" / "bin" / "pip", MOVING_PIP)
    r = run_update(box, "--yes", MOVE_ORIGIN_TO=evil)

    assert r.returncode == 0, r.stdout + r.stderr
    assert git(box.root, "rev-parse", "origin/main") == evil, "precondition: origin/main moved"
    assert head(box.root) == box.v2


# ── what .git says cannot choose or change what root runs (review of rank 79) ──
#
# Each attacker can write the box's .git and nothing else. Before the fix every
# one of them got its own Scripts/install.sh run through `sudo install.sh`.

EVIL_INSTALL = textwrap.dedent("""\
    #!/bin/sh
    echo "[evil] the planted install.sh ran"
    touch "$PWD/PWNED"
    exit 0
""")


def evil_ran(box, r) -> bool:
    return (box.root / "PWNED").exists() or "[evil]" in r.stdout


def test_update_sh_and_the_runner_pass_git_the_same_safe_options():
    import re
    from Orchestrator.update import tree_check
    script = (REPO / "Scripts" / "update.sh").read_text()
    line = re.search(r"^GIT_SAFE=\((.*)\)$", script, re.M).group(1)
    assert line.split() == list(tree_check.GIT_SAFE_OPTIONS)
    assert SAFE.split() == list(tree_check.GIT_SAFE_OPTIONS)
    # Every git call goes through the wrapper that adds them, or names them.
    assert re.findall(r"^git\(\) \{ command \"\$\{GIT_CMD\[@\]\}\" \"\$\{GIT_SAFE\[@\]\}\" \"\$@\"; \}$",
                      script, re.M)
    assert "rev-parse origin/main" not in script and "LATEST_SHA=$(git" not in script


def test_a_tag_named_origin_main_does_not_choose_what_is_installed(sh, trust, tmp_path):
    box = sh()
    write_pins(trust, origins=[str(box.origin)])
    evil = plant_commit(box, tmp_path, {"Scripts/install.sh": EVIL_INSTALL})
    git(box.root, "update-ref", "refs/tags/origin/main", evil)   # data only
    r = run_update(box, "--yes")

    assert r.returncode == 0, r.stdout + r.stderr
    assert head(box.root) == box.v2
    assert not evil_ran(box, r)


def test_a_remote_fetch_refspec_does_not_choose_what_is_installed(sh, trust, tmp_path):
    box = sh()
    write_pins(trust, origins=[str(box.origin)])
    evil = plant_commit(box, tmp_path, {"Scripts/install.sh": EVIL_INSTALL})
    git(box.root, "update-ref", "refs/remotes/origin/main", evil)
    git(box.root, "config", "--replace-all", "remote.origin.fetch",
        "+refs/heads/main:refs/remotes/origin/upstream")
    r = run_update(box, "--yes")

    assert r.returncode == 0, r.stdout + r.stderr
    assert head(box.root) == box.v2
    assert not evil_ran(box, r)


def test_a_replace_ref_cannot_swap_install_sh_under_a_signed_commit(sh, trust, tmp_path):
    box = sh()
    key = make_signing_key(tmp_path / "keys")
    signed = push_signed(box, key)                  # changes app.py only
    write_pins(trust, origins=[str(box.origin)], signers=[key.line])
    git(box.root, "fetch", "-q", "origin", "main")
    evil_tree = tree_with(box.root, signed, "Scripts/install.sh", EVIL_INSTALL, tmp_path / "idx")
    git(box.root, "replace", git(box.root, "rev-parse", f"{signed}^{{tree}}"), evil_tree)
    r = run_update(box, "--yes")

    assert r.returncode == 0, r.stdout + r.stderr
    assert head(box.root) == signed
    assert (box.root / "Scripts" / "install.sh").read_text() == INSTALL_STUB
    assert not evil_ran(box, r)


def test_a_rewritten_object_is_refused_before_anything_changes(sh, trust, tmp_path):
    box = sh()
    key = make_signing_key(tmp_path / "keys")
    signed = push_signed(box, key)
    write_pins(trust, origins=[str(box.origin)], signers=[key.line])
    git(box.root, "-c", "fetch.unpackLimit=100000", "fetch", "-q", "origin", "main")
    tree = git(box.root, "rev-parse", f"{signed}^{{tree}}")
    evil_tree = tree_with(box.root, signed, "Scripts/install.sh", EVIL_INSTALL, tmp_path / "idx")
    raw = subprocess.run(["git", "cat-file", "tree", evil_tree], cwd=str(box.root),
                         capture_output=True, check=True).stdout
    overwrite_loose_object(box.root, tree, "tree", raw)
    r = run_update(box, "--yes")

    assert r.returncode == 4, r.stdout + r.stderr
    assert "does not hash to its id" in r.stderr
    assert head(box.root) == box.v1 and tags(box.root) == [] and sudo_calls(box) == []
    assert not evil_ran(box, r)


def test_a_hook_cannot_rewrite_install_sh_after_the_verified_reset(sh, trust, tmp_path):
    box = sh()
    key = make_signing_key(tmp_path / "keys")
    signed = push_signed(box, key)
    write_pins(trust, origins=[str(box.origin)], signers=[key.line])
    planted = tmp_path / "planted-install.sh"
    planted.write_text(EVIL_INSTALL)
    for name in ("reference-transaction", "post-checkout"):
        hook = box.root / ".git" / "hooks" / name
        hook.write_text(f"#!/bin/sh\ncp {planted} Scripts/install.sh\n")
        hook.chmod(0o755)
    r = run_update(box, "--yes")

    assert r.returncode == 0, r.stdout + r.stderr
    assert head(box.root) == signed
    assert (box.root / "Scripts" / "install.sh").read_text() == INSTALL_STUB
    assert not evil_ran(box, r)


def test_a_filter_that_rewrites_files_during_the_reset_stops_install_sh(sh, trust, tmp_path):
    """Hooks are off, but a smudge filter (.git/config + .git/info/attributes)
    still runs during the reset. What it wrote is found before root runs
    anything from the tree, and the code goes back."""
    box = sh()
    write_pins(trust, origins=[str(box.origin)])
    smudge_filter(box.root, tmp_path, "app.py", "EVIL = True\n")
    r = run_update(box, "--yes")

    assert r.returncode == 4, r.stdout + r.stderr
    assert "app.py: differs from the release" in r.stderr
    assert "install.sh was not run" in r.stderr
    assert head(box.root) == box.v1
    assert not [c for c in sudo_calls(box) if "install.sh" in c], "root ran nothing from that tree"
    assert "systemctl restart blackbox.service" not in sudo_calls(box)


# ── a failed install.sh: the stash and the trusted store go back too ──────

def test_a_failed_install_puts_the_stashed_edit_back(sh):
    box = sh()
    (box.root / "app.py").write_text("VERSION = 1  # hand fix\n")
    r = run_update(box, "--yes", "--stash", STUB_INSTALL_RC="1")

    assert r.returncode == 1, r.stdout + r.stderr
    assert head(box.root) == box.v1
    assert (box.root / "app.py").read_text() == "VERSION = 1  # hand fix\n"
    assert stash_entries(box.root) == [], "the stash that went back is dropped"


def test_a_failed_install_republishes_the_trusted_store_from_the_old_code(sh):
    box = sh()
    r = run_update(box, "--yes", STUB_INSTALL_RC="1")
    assert r.returncode == 1, r.stdout + r.stderr
    calls = sudo_calls(box)
    assert calls[-1] == f"{box.root}/Scripts/install.sh --publish-store-only", calls
    assert "[install-stub] store republished" in r.stdout
    assert "systemctl restart blackbox.service" not in calls


LEGACY_INSTALL_STUB = textwrap.dedent("""\
    #!/bin/sh
    # An install.sh from before the trusted-store-only mode: every run is a
    # full install, whatever its arguments (it mentions --publish-store-only only
    # in this comment).
    echo "[install-stub] full install"
    exit "${STUB_INSTALL_RC:-0}"
""")


def test_old_code_without_the_store_mode_gets_the_remedy_not_a_full_install(sh):
    box = sh(install_stub=LEGACY_INSTALL_STUB)
    r = run_update(box, "--yes", STUB_INSTALL_RC="1")
    assert r.returncode == 1, r.stdout + r.stderr
    assert [c for c in sudo_calls(box) if "install.sh" in c] == [f"{box.root}/Scripts/install.sh"]
    assert f"sudo bash {box.root}/Scripts/install.sh" in r.stderr


# ── a checkout owned by a dedicated service account (X04-...-12) ─────────

def test_git_and_pip_run_as_the_checkouts_owner(sh):
    box = sh(tree_owner="svc-owner")
    _write_exec(box.root / "Orchestrator" / "venv" / "bin" / "pip", FAKE_PIP)
    r = run_update(box, "--yes")

    assert r.returncode == 0, r.stdout + r.stderr
    assert head(box.root) == box.v2
    calls = sudo_calls(box)
    assert f"-u svc-owner -- git {SAFE} fetch origin +refs/heads/main:refs/remotes/origin/main" in calls
    assert f"-u svc-owner -- git {SAFE} reset --hard {box.v2}" in calls
    # The checker's git runs as the owner too; it hashes what git hands it.
    assert f"-u svc-owner -- git {SAFE} cat-file --batch" in calls
    assert any(c.startswith("-u svc-owner -- ") and c.endswith("/pip freeze") for c in calls)
    assert "-u svc-owner -- tee Manifest/pre_update_pip_freeze.txt" in calls
    # Only install.sh and the restart run as root.
    root_calls = [c for c in calls if not c.startswith("-u svc-owner -- ")]
    assert root_calls == [f"{box.root}/Scripts/install.sh",
                          "systemctl restart blackbox.service"], root_calls


def test_a_failed_install_rolls_back_as_the_checkouts_owner(sh):
    box = sh(requirements=True, tree_owner="svc-owner")
    _write_exec(box.root / "Orchestrator" / "venv" / "bin" / "pip", FAKE_PIP)
    (box.root / "app.py").write_text("VERSION = 1  # hand fix\n")
    r = run_update(box, "--yes", "--stash", STUB_INSTALL_RC="1")

    assert r.returncode == 1, r.stdout + r.stderr
    assert head(box.root) == box.v1
    assert (box.root / "app.py").read_text() == "VERSION = 1  # hand fix\n"
    calls = sudo_calls(box)
    assert any(c.startswith("-u svc-owner -- ") and "/pip install -r " in c for c in calls)
    assert any(c.startswith(f"-u svc-owner -- git {SAFE} stash apply") for c in calls)
    assert [c for c in calls if not c.startswith("-u svc-owner -- ")] == [
        f"{box.root}/Scripts/install.sh", f"{box.root}/Scripts/install.sh --publish-store-only"]
