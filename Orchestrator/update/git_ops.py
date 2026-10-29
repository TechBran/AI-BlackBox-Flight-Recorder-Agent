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

WHY every call carries tree_check.GIT_SAFE_OPTIONS (2026-10 audit rank 79,
X04-security-clients-infra-11): .git is writable by the service, so a
refs/replace/ entry could swap the tree checked out under a verified commit and
a hook or an fsmonitor program could run during the reset. The options turn
both off whatever .git/config says. Scripts/update.sh passes the same ones.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from Orchestrator.update.tree_check import GIT_SAFE_OPTIONS


# Canonical public repo (renamed 2026-07-23; GitHub 301-redirects the old
# blackbox-poc slug indefinitely — see Scripts/update.sh's origin self-heal).
DEFAULT_REMOTE_URL = "https://github.com/TechBran/ai-blackbox-flight-recorder-agent.git"


def _git(root: Path, *args: str, check: bool = True,
         timeout: Optional[float] = 60.0,
         env: Optional[dict] = None) -> subprocess.CompletedProcess:
    """Run `git <args>` with cwd=root. Captures stdout+stderr as text.

    Default timeout is 60s — covers git fetch over typical residential
    connections. Caller passes timeout=None for git clone of the whole
    repo (which can take longer on first install). `env` replaces the
    environment (tracked_changes passes GIT_INDEX_FILE this way).
    """
    return subprocess.run(
        ["git", *GIT_SAFE_OPTIONS, *args],
        cwd=str(root),
        capture_output=True, text=True, timeout=timeout, check=check, env=env,
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


# The ref every update installs, and where the fetch keeps a copy of it.
MAIN_REF = "refs/heads/main"
TRACKING_REF = "refs/remotes/origin/main"


def fetch_origin_main(root: Path) -> None:
    """Bring origin's main into the object store and refs/remotes/origin/main.
    Does NOT touch the working tree. The refspec is given in full on the
    command line, so a remote.origin.fetch in .git/config cannot send main
    elsewhere. What gets installed is never read back from that ref (see
    latest_origin_sha): this only brings the objects."""
    _git(root, "fetch", "origin", f"+{MAIN_REF}:{TRACKING_REF}", timeout=120.0)


def latest_origin_sha(root: Path) -> str:
    """Full SHA of main on the checkout's origin, as the origin itself reports
    it now (git ls-remote), for the commit an update installs (2026-10 audit
    X04-security-clients-infra-11). Never the local origin/main: a tag named
    origin/main wins git's ref lookup over refs/remotes/origin/main, and the
    service can write both. Call it BEFORE fetch_origin_main, so the fetch
    brings this commit (or a newer one that contains it)."""
    return remote_main_sha(origin_url(root))


def remote_main_sha(url: str) -> str:
    """Full SHA of refs/heads/main at `url`, from `git ls-remote` run outside
    any checkout (cwd /) with the GIT_* variables that redirect git removed,
    so no checkout's .git/config (http.*, url.*.insteadOf) and none of its
    refs can change the answer. Raises CalledProcessError when the remote
    does not answer or has no main."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") or k == "GIT_TERMINAL_PROMPT"}
    out = subprocess.run(["git", "ls-remote", "--", url, MAIN_REF], cwd="/", env=env,
                         capture_output=True, text=True, timeout=60.0, check=True).stdout
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == MAIN_REF and _is_full_sha(sha):
            return sha
    raise subprocess.CalledProcessError(2, ["git", "ls-remote", url, MAIN_REF], out,
                                        f"{url} has no {MAIN_REF}")


def has_commit(root: Path, sha: str) -> bool:
    """True when commit `sha` is in the object store (after a fetch)."""
    return _git(root, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def _is_full_sha(s: str) -> bool:
    return len(s) in (40, 64) and all(c in "0123456789abcdef" for c in s)


def origin_url(root: Path) -> str:
    """The URL `git fetch origin` uses: remote.origin.url with any
    url.*.insteadOf rewrite applied (verify.check_origin compares it with the
    pinned update origin)."""
    return _git(root, "remote", "get-url", "origin").stdout.strip()


def set_origin_url(root: Path, url: str) -> None:
    """`git remote set-url origin <url>` (verify.check_origin's re-point of the
    retired slug)."""
    _git(root, "remote", "set-url", "origin", url)


def raw_commit(root: Path, sha: str) -> str:
    """The raw commit object `sha` (header, then a blank line, then the
    message). verify.check_commit reads its gpgsig header."""
    return _git(root, "cat-file", "commit", sha).stdout


def verify_commit(root: Path, sha: str, *, allowed_signers: str, ssh_keygen: str,
                  false: str) -> subprocess.CompletedProcess:
    """`git verify-commit sha` against `allowed_signers` ONLY: the -c options
    beat .git/config, so a planted gpg.ssh.program or allowedSignersFile is
    ignored, and the PGP and X.509 verifiers are `false`. Never raises on a
    bad signature: the caller reads returncode."""
    return _git(root,
                "-c", f"gpg.ssh.allowedSignersFile={allowed_signers}",
                "-c", f"gpg.ssh.program={ssh_keygen}",
                "-c", f"gpg.program={false}",
                "-c", f"gpg.x509.program={false}",
                "verify-commit", sha, check=False)


def commits_behind(root: Path, base: str = "HEAD",
                    target: str = TRACKING_REF) -> int:
    """How many commits target is ahead of base (i.e. updates available)."""
    out = _git(root, "rev-list", "--count", f"{base}..{target}").stdout.strip()
    return int(out) if out else 0


def commits_ahead(root: Path, base: str = "HEAD",
                   target: str = TRACKING_REF) -> int:
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


def tracked_changes(root: Path) -> list[str]:
    """Paths of TRACKED files with local changes, staged or not (audit
    O16-01). `git reset --hard` destroys every one of them.

    Untracked files are left out on purpose: a box's runtime files (ledger,
    uploads, Manifest/) are untracked or ignored, and a reset keeps them,
    except where the target commit has a file of its own: see
    paths_in_the_way(). --no-optional-locks keeps this probe from taking
    .git/index.lock, so a status poll can never make the runner's reset fail
    on a held lock. On a ZIP-installed box whose index was never filled, the
    files on disk are compared with HEAD instead (see below), so only files
    that really differ are listed.
    """
    status = ("--no-optional-locks", "status", "--porcelain=v1", "-z",
              "--untracked-files=no")
    if _index_unfilled(root):
        # A ZIP-installed box (install.sh Step 0a / lazy_init): HEAD names a
        # commit but the index was never filled, so plain status calls every
        # file a staged deletion. Compare the files on disk with HEAD through
        # a throwaway index instead; the box's own index is never written. It
        # lives in the git dir, which the service can always write (/tmp may
        # not be writable under the unit's sandboxing).
        git_dir = _git(root, "rev-parse", "--absolute-git-dir").stdout.strip()
        with tempfile.TemporaryDirectory(prefix="blackbox-status-", dir=git_dir) as tmp:
            env = {**os.environ, "GIT_INDEX_FILE": os.path.join(tmp, "index")}
            _git(root, "read-tree", "HEAD", env=env)
            out = _git(root, *status, env=env).stdout
    else:
        out = _git(root, *status).stdout
    entries = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        # -z puts a rename's or copy's source path in the next entry. The
        # worktree column can say R too: an intent-to-add file paired with a
        # deleted one (" R new\0old\0").
        if entry[0] in "RC" or entry[1] in "RC":
            i += 1
        paths.append(entry[3:])
    return paths


def _index_unfilled(root: Path) -> bool:
    """True when HEAD has files but the index has none: what install.sh Step
    0a and lazy_init leave on a ZIP-installed box until its first reset."""
    if _git(root, "ls-files", "-z").stdout:
        return False
    tree = _git(root, "ls-tree", "--name-only", "HEAD", check=False)
    return tree.returncode == 0 and bool(tree.stdout.strip())


def paths_in_the_way(root: Path, target: str) -> list[str]:
    """Untracked or ignored paths that `git reset --hard <target>` would
    overwrite without a word: a file on disk where `target` adds one that HEAD
    does not have, or a file where it needs a directory. Neither a stash nor a
    tag holds them, so the runner moves them aside before it resets.

    Run it after stashing: a staged new file is then off the disk, and what is
    left at those paths is not tracked.
    """
    out = _git(root, "diff", "--name-only", "-z", "--no-renames",
               "--diff-filter=A", "HEAD", target).stdout
    hits: set[str] = set()
    for rel in filter(None, out.split("\0")):
        # A file or a symlink where the target puts a directory, checked from
        # the top: git does not follow a symlink there, it replaces it.
        for parent in reversed(Path(rel).parents[:-1]):
            p = root / parent
            if p.is_symlink() or (p.exists() and not p.is_dir()):
                hits.add(parent.as_posix())
                break
        else:
            if os.path.lexists(root / rel):
                hits.add(rel)
    return sorted(hits)


def tag_commit(root: Path, name: str) -> str:
    """Full SHA of the commit tag `name` points at, or "" when there is no such
    tag. Resolved through refs/tags/ so a name can never be read as an option
    or as some other ref."""
    out = _git(root, "rev-parse", "-q", "--verify", f"refs/tags/{name}^{{commit}}",
               check=False)
    return out.stdout.strip() if out.returncode == 0 else ""


def tag(root: Path, name: str, ref: str = "HEAD") -> None:
    """Create a lightweight git tag pointing at `ref`. Used for rollback anchors."""
    _git(root, "tag", name, ref)


def delete_tag(root: Path, name: str) -> None:
    """Delete a local tag. Used to clean up successful pre-update tags
    after the update has been stable for some time (caller policy)."""
    _git(root, "tag", "-d", name, check=False)


def reset_hard(root: Path, ref: str) -> None:
    """git reset --hard <ref>.

    Destroys every local change to a tracked file, and overwrites the
    untracked files paths_in_the_way() lists. Caller MUST hold the update
    mutex and must have checked tracked_changes() first: refuse a dirty tree,
    or stash it when the operator agreed to discard the changes (audit
    O16-01). The runner (UpdateRunner._keep_then_reset) re-checks both in the
    same step as the reset. It resets, then installs the new dependencies,
    then restarts; the old process keeps running the code it already imported
    until the restart.
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
    was actually stashed (False = clean tree, no stash entry created).
    Tracked changes only: untracked files stay where they are."""
    out = _git(root, "stash", "push", "-m", message).stdout
    return "No local changes" not in out


def latest_stash(root: Path) -> str:
    """Full SHA of the newest stash entry, or "" when there is none. Reported
    to the operator so a stashed change can be found again."""
    out = _git(root, "rev-parse", "-q", "--verify", "refs/stash", check=False)
    return out.stdout.strip() if out.returncode == 0 else ""


def stash_apply(root: Path, sha: str) -> None:
    """`git stash apply <sha>`: put a stash made by the updater back in the
    working tree. Raises CalledProcessError when it does not apply cleanly
    (git then leaves the working tree as it was for files the stash touches)."""
    _git(root, "stash", "apply", "-q", sha)


def drop_stash(root: Path, sha: str) -> bool:
    """Drop the stash entry whose commit is `sha`, found by SHA (an entry's
    stash@{n} index moves whenever another stash is made). False when no entry
    has that SHA."""
    out = _git(root, "stash", "list", "--format=%H %gd", check=False).stdout
    for line in out.splitlines():
        entry_sha, _, ref = line.partition(" ")
        if entry_sha == sha and ref:
            _git(root, "stash", "drop", "-q", ref)
            return True
    return False
