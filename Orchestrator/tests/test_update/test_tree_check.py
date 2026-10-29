"""tree_check.py: is the checkout, byte for byte, the commit the updater
verified? (2026-10 audit rank 79, X04-security-clients-infra-11, review
follow-up.) Real git over a tmp_path repo; every tamper below is one the
service account (which owns .git and the tree) could make."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from Orchestrator.update import tree_check

from ._gitbox import git, make_box, overwrite_loose_object, tree_with

CHECKER = Path(tree_check.__file__)


@pytest.fixture
def box(tmp_path):
    b = make_box(tmp_path, {"Scripts/install.sh": "#!/bin/sh\necho install\n",
                            "Orchestrator/app.py": "APP = 1\n"})
    os.symlink("Scripts/install.sh", b.root / "install-link")
    git(b.root, "add", "install-link")
    git(b.root, "commit", "-q", "-m", "a tracked symlink")
    b.head = git(b.root, "rev-parse", "HEAD")
    return b


def test_a_clean_checkout_matches_in_both_modes(box):
    assert tree_check.verify_objects(str(box.root), box.head) == []
    assert tree_check.verify_worktree(str(box.root), box.head) == []


def test_untracked_files_are_not_looked_at(box):
    (box.root / "Manifest" / "state.json").write_text("{}")
    (box.root / "Orchestrator" / "venv").mkdir()
    assert tree_check.verify_worktree(str(box.root), box.head) == []


def test_an_edited_tracked_file_is_reported(box):
    (box.root / "Scripts" / "install.sh").write_text("#!/bin/sh\necho planted\n")
    assert tree_check.verify_worktree(str(box.root), box.head) == [
        "Scripts/install.sh: differs from the release"]


def test_a_missing_tracked_file_is_reported(box):
    (box.root / "notes.txt").unlink()
    assert tree_check.verify_worktree(str(box.root), box.head) == ["notes.txt: missing"]


def test_a_file_swapped_for_a_symlink_to_the_same_bytes_is_reported(box, tmp_path):
    """Same content today, whatever the target says tomorrow."""
    copy = tmp_path / "elsewhere-install.sh"
    shutil.copy(box.root / "Scripts" / "install.sh", copy)
    (box.root / "Scripts" / "install.sh").unlink()
    os.symlink(copy, box.root / "Scripts" / "install.sh")
    assert tree_check.verify_worktree(str(box.root), box.head) == [
        "Scripts/install.sh: is not a regular file"]


def test_a_directory_swapped_for_a_symlink_to_a_copy_is_reported(box, tmp_path):
    copy = tmp_path / "elsewhere-Scripts"
    shutil.copytree(box.root / "Scripts", copy)
    shutil.rmtree(box.root / "Scripts")
    os.symlink(copy, box.root / "Scripts")
    problems = tree_check.verify_worktree(str(box.root), box.head)
    assert problems == ["Scripts/install.sh: Scripts/ on disk is not a plain directory"]


def test_a_tracked_symlink_must_stay_one_with_its_target(box):
    link = box.root / "install-link"
    link.unlink()
    os.symlink("somewhere/else", link)
    assert tree_check.verify_worktree(str(box.root), box.head) == [
        "install-link: differs from the release"]
    link.unlink()
    link.write_text("#!/bin/sh\necho install\n")
    assert tree_check.verify_worktree(str(box.root), box.head) == [
        "install-link: should be a symlink"]


def test_a_rewritten_loose_object_is_caught_in_both_modes(box, tmp_path):
    """git itself reads a rewritten loose object without a word (reset writes
    it to disk); the checker hashes it."""
    planted = tree_with(box.root, box.head, "Scripts/install.sh", "#!/bin/sh\necho planted\n",
                        tmp_path / "idx")
    raw = subprocess.run(["git", "cat-file", "tree", planted], cwd=str(box.root),
                         capture_output=True, check=True).stdout
    tree = git(box.root, "rev-parse", f"{box.head}^{{tree}}")
    overwrite_loose_object(box.root, tree, "tree", raw)
    for check in (tree_check.verify_objects, tree_check.verify_worktree):
        assert check(str(box.root), box.head) == [
            f"the top-level tree: tree {tree[:12]} in .git does not hash to its id"]


def test_a_rewritten_blob_is_caught_before_the_reset(box):
    blob = git(box.root, "rev-parse", f"{box.head}:Scripts/install.sh")
    overwrite_loose_object(box.root, blob, "blob", b"#!/bin/sh\necho planted\n")
    assert tree_check.verify_objects(str(box.root), box.head) == [
        f"Scripts/install.sh: object {blob[:12]} in .git does not hash to its id"]


def test_a_replace_ref_does_not_change_what_is_checked(box, tmp_path):
    planted = tree_with(box.root, box.head, "Scripts/install.sh", "#!/bin/sh\necho planted\n",
                        tmp_path / "idx")
    git(box.root, "replace", git(box.root, "rev-parse", f"{box.head}^{{tree}}"), planted)
    (box.root / "Scripts" / "install.sh").write_text("#!/bin/sh\necho planted\n")
    assert tree_check.verify_worktree(str(box.root), box.head) == [
        "Scripts/install.sh: differs from the release"]


def test_a_commit_the_store_does_not_have_is_reported(box):
    missing = "0123456789abcdef0123456789abcdef01234567"
    assert tree_check.verify_objects(str(box.root), missing) == [
        f"commit {missing[:12]} is missing from .git (the fetch did not bring it)"]


def test_a_short_or_symbolic_name_is_not_accepted(box):
    for name in (box.head[:12], "HEAD", "origin/main"):
        with pytest.raises(tree_check.TreeCheckError):
            tree_check.verify_worktree(str(box.root), name)


def test_the_cli_exit_codes_and_the_git_command_it_is_given(box, tmp_path):
    """update.sh runs it with `python3 -I` and, on a service-account box,
    `sudo -u <owner> -- git` as the git command."""
    log = tmp_path / "git-calls"
    wrapper = tmp_path / "logging-git"
    wrapper.write_text(f'#!/bin/sh\necho "$*" >> {log}\nexec git "$@"\n')
    wrapper.chmod(0o755)

    def cli(*args):
        return subprocess.run([sys.executable, "-I", str(CHECKER), *args],
                              capture_output=True, text=True, timeout=60)

    ok = cli("worktree", str(box.root), box.head, str(wrapper))
    assert ok.returncode == 0 and ok.stdout == "", ok.stderr
    assert log.read_text().splitlines() == [
        " ".join([*tree_check.GIT_SAFE_OPTIONS, "cat-file", "--batch"])]

    (box.root / "notes.txt").write_text("changed\n")
    bad = cli("worktree", str(box.root), box.head)
    assert bad.returncode == 1 and bad.stdout == "notes.txt: differs from the release\n"
    assert cli("objects", str(box.root), "HEAD").returncode == 2
    assert cli("nonsense").returncode == 2


def test_long_problem_lists_are_cut_short():
    problems = [f"f{i}: missing" for i in range(tree_check.MAX_LISTED + 5)]
    out = tree_check.summarize(problems)
    assert len(out) == tree_check.MAX_LISTED + 1 and out[-1] == "... and 5 more"


def test_the_checker_needs_nothing_but_the_standard_library():
    """Scripts/update.sh runs it with the system python3 -I, outside any venv."""
    import ast
    tree = ast.parse(CHECKER.read_text())
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree)
              if isinstance(n, ast.ImportFrom) and n.module}
    assert names <= set(sys.stdlib_module_names) | {"__future__"}, names
