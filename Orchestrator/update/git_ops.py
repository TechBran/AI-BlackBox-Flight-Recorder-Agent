"""Thin subprocess wrappers around git commands.

All functions take a `root: Path` argument (the BLACKBOX_ROOT git checkout)
and run git via subprocess. Errors raise subprocess.CalledProcessError —
the caller (runner.py) catches and translates to update-state failure.

WHY subprocess vs GitPython/pygit2:
  - No new pip dependency (git is already in Scripts/onboarding/system-packages.txt
    MUST_HAVE so we know it's installed).
  - Plays nice with whatever git version the customer has (pygit2 is
    fragile on Ubuntu 24.04's older libgit2).
  - Behavior matches what an operator sees when they SSH in and run the
    same commands — no surprise differences.

WHY each call sets cwd= explicitly:
  - The blackbox.service process can chdir() during long-running tasks
    (CLI agent PTY bridge does this). Trusting the global cwd is brittle.
  - Each invocation is self-contained → safe to call from threads / async
    tasks without cwd lock contention.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional


# Canonical public repo (renamed 2026-07-23; GitHub 301-redirects the old
# blackbox-poc slug indefinitely — see Scripts/update.sh's origin self-heal).
DEFAULT_REMOTE_URL = "https://github.com/TechBran/ai-blackbox-flight-recorder-agent.git"


def _git(root: Path, *args: str, check: bool = True,
         timeout: Optional[float] = 60.0) -> subprocess.CompletedProcess:
    """Run `git <args>` with cwd=root. Captures stdout+stderr as text.

    Default timeout is 60s — covers git fetch over typical residential
    connections. Caller passes timeout=None for git clone of the whole
    repo (which can take longer on first install).
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True, text=True, timeout=timeout, check=check,
    )


def is_initialized(root: Path) -> bool:
    """Return True if `root` is a git checkout (has .git/)."""
    return (root / ".git").is_dir()


def lazy_init(root: Path, remote_url: str = DEFAULT_REMOTE_URL) -> None:
    """Bootstrap git in an existing directory WITHOUT touching files.

    Mirrors install.sh's Step 0a lazy-init for ZIP-installed customers.
    After this call, `root` has a .git/ pointing at the public repo with
    main branch tracking origin/main — but no `git reset --hard` has run,
    so customer files (including any local edits) stay where they are.

    Raises subprocess.CalledProcessError if any git command fails.
    """
    _git(root, "init", "-q")
    # Idempotent: remote add fails if 'origin' exists, so swallow that case.
    add = _git(root, "remote", "add", "origin", remote_url, check=False)
    if add.returncode != 0 and "already exists" not in add.stderr:
        raise subprocess.CalledProcessError(add.returncode, add.args,
                                             add.stdout, add.stderr)
    _git(root, "fetch", "-q", "origin", "main", timeout=120.0)
    _git(root, "update-ref", "refs/heads/main", "FETCH_HEAD")
    _git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    # branch upstream — may fail on bare git; non-fatal.
    _git(root, "branch", "--set-upstream-to=origin/main", "main", check=False)


def current_sha(root: Path) -> str:
    """Return the full 40-char SHA of HEAD."""
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def current_short(root: Path) -> str:
    """Return the 7-char abbreviation of HEAD."""
    return _git(root, "rev-parse", "--short", "HEAD").stdout.strip()


def fetch_origin_main(root: Path) -> None:
    """git fetch origin main. Updates refs/remotes/origin/main to the
    latest GitHub state. Does NOT touch the working tree."""
    _git(root, "fetch", "origin", "main", timeout=120.0)


def latest_origin_sha(root: Path) -> str:
    """Return the full SHA of origin/main (last-fetched value).
    Caller is responsible for fetch_origin_main first if a fresh read is needed."""
    return _git(root, "rev-parse", "origin/main").stdout.strip()


def commits_behind(root: Path, base: str = "HEAD",
                    target: str = "origin/main") -> int:
    """How many commits target is ahead of base (i.e. updates available)."""
    out = _git(root, "rev-list", "--count", f"{base}..{target}").stdout.strip()
    return int(out) if out else 0


def commits_ahead(root: Path, base: str = "HEAD",
                   target: str = "origin/main") -> int:
    """How many commits base is ahead of target (i.e. local unpushed work)."""
    out = _git(root, "rev-list", "--count", f"{target}..{base}").stdout.strip()
    return int(out) if out else 0


def commits_between(root: Path, from_sha: str, to_sha: str) -> list[dict]:
    """Return a list of commits in (from_sha, to_sha] for UI display.
    Each dict: {sha, short, subject, author, date_iso}. Newest first."""
    fmt = "%H%x00%h%x00%s%x00%an%x00%aI"
    out = _git(root, "log", f"{from_sha}..{to_sha}", f"--format={fmt}").stdout
    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x00")
        if len(parts) != 5:
            continue
        sha, short, subject, author, date_iso = parts
        commits.append({
            "sha": sha,
            "short": short,
            "subject": subject,
            "author": author,
            "date_iso": date_iso,
        })
    return commits


def diff_files(root: Path, from_sha: str, to_sha: str) -> list[str]:
    """Return list of file paths changed in (from_sha, to_sha]."""
    out = _git(root, "diff", "--name-only", f"{from_sha}..{to_sha}").stdout
    return [line for line in out.splitlines() if line.strip()]


def status_porcelain(root: Path) -> list[str]:
    """Return a list of dirty-file lines (empty list = clean working tree).
    Each line is git's porcelain format e.g. " M file.py" or "?? newfile.txt"."""
    out = _git(root, "status", "--porcelain").stdout
    return [line for line in out.splitlines() if line.strip()]


def tag(root: Path, name: str, ref: str = "HEAD") -> None:
    """Create a lightweight git tag pointing at `ref`. Used for rollback anchors."""
    _git(root, "tag", name, ref)


def delete_tag(root: Path, name: str) -> None:
    """Delete a local tag. Used to clean up successful pre-update tags
    after the update has been stable for some time (caller policy)."""
    _git(root, "tag", "-d", name, check=False)


def reset_hard(root: Path, ref: str) -> None:
    """git reset --hard <ref>. Atomic file swap.

    Caller MUST hold the update mutex AND have already validated that any
    side-effect-causing tasks succeeded against a worktree-staging copy
    (audit C2). uvicorn workers re-importing modules mid-reset will crash
    on partially-written .py files — so this call must be followed
    immediately by the detached restart with no intervening async/await.
    """
    _git(root, "reset", "--hard", ref)


def worktree_add(root: Path, path: Path, ref: str) -> None:
    """Create a separate working tree at `path` checked out to `ref`.
    Used by the runner to stage + validate update tasks before the
    live working tree is touched (audit C2 atomicity model)."""
    _git(root, "worktree", "add", str(path), ref, timeout=120.0)


def worktree_remove(root: Path, path: Path, force: bool = True) -> None:
    """Remove a worktree previously created by worktree_add. The --force
    handles the case where files in the worktree got modified during
    validation (e.g., pip install left a __pycache__/ dirty)."""
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(path))
    _git(root, *args, check=False)


def stash_push(root: Path, message: str) -> bool:
    """git stash push with the given message. Returns True if anything
    was actually stashed (False = clean tree, no stash entry created)."""
    out = _git(root, "stash", "push", "-m", message).stdout
    return "No local changes" not in out
