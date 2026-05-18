"""Tests for Orchestrator/update/git_ops.py — git wrapper correctness.

Uses a real temp git repo fixture (tmp_path + git init) so the actual
subprocess plumbing is exercised. Each test creates an isolated repo so
state from one test can't leak to another.

Skipping rationale: the lazy_init() test would clone from github.com,
which is brittle in CI. Tested separately during MSO2 hardware validation.
"""
import subprocess
from pathlib import Path

import pytest

from Orchestrator.update import git_ops


@pytest.fixture
def repo(tmp_path):
    """Create a temp git repo with one initial commit; yield the path."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)
    (root / "file1.txt").write_text("initial\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=root, check=True)
    return root


def test_is_initialized_true_for_real_repo(repo):
    assert git_ops.is_initialized(repo) is True


def test_is_initialized_false_for_plain_dir(tmp_path):
    assert git_ops.is_initialized(tmp_path) is False


def test_current_sha_returns_full_hash(repo):
    sha = git_ops.current_sha(repo)
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_current_short_is_7_chars(repo):
    short = git_ops.current_short(repo)
    assert len(short) == 7


def test_status_porcelain_clean_repo_returns_empty(repo):
    assert git_ops.status_porcelain(repo) == []


def test_status_porcelain_dirty_repo_returns_lines(repo):
    (repo / "file1.txt").write_text("modified\n")
    (repo / "newfile.txt").write_text("new\n")
    lines = git_ops.status_porcelain(repo)
    assert any("file1.txt" in line for line in lines)
    assert any("newfile.txt" in line for line in lines)


def test_tag_creates_lightweight_tag(repo):
    git_ops.tag(repo, "test-tag")
    result = subprocess.run(["git", "tag", "--list"], cwd=repo,
                            capture_output=True, text=True)
    assert "test-tag" in result.stdout


def test_reset_hard_reverts_to_tag(repo):
    git_ops.tag(repo, "before")
    (repo / "file1.txt").write_text("changed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "edit"], cwd=repo, check=True)
    assert (repo / "file1.txt").read_text() == "changed\n"
    git_ops.reset_hard(repo, "before")
    assert (repo / "file1.txt").read_text() == "initial\n"


def test_diff_files_lists_changed_paths_between_commits(repo):
    sha_before = git_ops.current_sha(repo)
    (repo / "file2.txt").write_text("second\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add second"], cwd=repo, check=True)
    sha_after = git_ops.current_sha(repo)
    changed = git_ops.diff_files(repo, sha_before, sha_after)
    assert "file2.txt" in changed
    assert "file1.txt" not in changed  # unchanged across the range


def test_commits_between_returns_subjects_and_authors(repo):
    sha_before = git_ops.current_sha(repo)
    (repo / "file3.txt").write_text("third\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add third"], cwd=repo, check=True)
    sha_after = git_ops.current_sha(repo)
    commits = git_ops.commits_between(repo, sha_before, sha_after)
    assert len(commits) == 1
    assert commits[0]["subject"] == "add third"
    assert commits[0]["author"] == "test"
    assert len(commits[0]["short"]) == 7


def test_commits_behind_zero_when_at_head(repo):
    # Self-compare: HEAD..HEAD is always 0 commits "behind".
    assert git_ops.commits_behind(repo, base="HEAD", target="HEAD") == 0


def test_commits_behind_counts_diff(repo):
    sha_before = git_ops.current_sha(repo)
    for i in range(3):
        (repo / f"f_{i}.txt").write_text(f"{i}\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"commit {i}"], cwd=repo, check=True)
    # 3 new commits between sha_before and current HEAD
    assert git_ops.commits_behind(repo, base=sha_before, target="HEAD") == 3


def test_stash_push_returns_false_on_clean_tree(repo):
    assert git_ops.stash_push(repo, "test-stash") is False


def test_stash_push_returns_true_when_dirty(repo):
    (repo / "file1.txt").write_text("dirty\n")
    assert git_ops.stash_push(repo, "test-stash") is True
    # Tree should be clean after stash
    assert git_ops.status_porcelain(repo) == []


def test_worktree_add_creates_separate_checkout(repo, tmp_path):
    wt_path = tmp_path / "worktree"
    git_ops.tag(repo, "stable")
    git_ops.worktree_add(repo, wt_path, "stable")
    assert wt_path.is_dir()
    assert (wt_path / "file1.txt").read_text() == "initial\n"
    git_ops.worktree_remove(repo, wt_path)
