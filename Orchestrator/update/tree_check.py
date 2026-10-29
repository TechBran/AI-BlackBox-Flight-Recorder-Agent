"""Is this checkout, byte for byte, the commit the updater verified? (2026-10
audit rank 79, X04-security-clients-infra-11.)

A SHA is only as good as what is read under it. `git verify-commit` checks the
commit object git reads, and `git reset --hard <sha>` writes whatever the
object store, a refs/replace/ entry, a hook, a filter or another process makes
of it. The object store and the working tree both belong to the account that
owns the checkout, which is the service's, so neither one says what is really
on disk. This module says it without trusting either, starting from a SHA that
came from the network (the pinned origin's own answer) or from the signature:

  objects   read the commit and every tree and blob it holds through
            `git cat-file --batch` and hash each one here: an object whose
            content is not what its id names (a planted pack, an overwritten
            loose object) is refused before anything is reset.
  worktree  after the reset, hash every tracked file on disk (raw bytes: no
            filters, no symlink followed) and compare it with the blob id the
            hash-checked tree lists. A file a hook, a filter or anything else
            rewrote, a missing file, or a directory turned into a symlink is
            refused before root runs anything from the tree.

Untracked files (the ledger, uploads, Manifest/, venvs) are not looked at.
Git runs with GIT_SAFE_OPTIONS (no replace refs, no hooks, no fsmonitor, no
pager) and may run as another account (`sudo -u <owner> -- git`): the hashing
happens in this process, so what git prints cannot pass for anything else.

Standard library only, so Scripts/update.sh runs a copy of this file with
`python3 -I` (as the operator, git as the checkout's owner) and verify.py
imports it for the in-app runner. CLI:

    python3 -I tree_check.py objects|worktree <root> <sha> [<git command...>]

exits 0 when everything matches, 1 with one problem per line on stdout, 2 when
it cannot check at all (usage, git missing).
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import sys
from typing import Iterable, Optional, Sequence

# Global options for every git call on the update path (git_ops._git and
# Scripts/update.sh pass the same ones). The service can write .git: a
# refs/replace/ entry would swap the tree checked out under a signed commit,
# a hook or an fsmonitor program would run during the reset, and a pager could
# run from `git log`. Command-line options beat anything .git/config says.
GIT_SAFE_OPTIONS = ("--no-replace-objects", "--no-pager",
                    "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")

# How many problems are listed before the rest are only counted.
MAX_LISTED = 20

_HEX_ID = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
_MODE_TREE = b"40000"
_MODE_LINK = b"120000"
_MODE_GITLINK = b"160000"
_MODES_FILE = (b"100644", b"100755", b"100664")


class TreeCheckError(Exception):
    """The check could not run at all (git missing or exited, a bad SHA)."""


def verify_objects(root: str, sha: str, git: Sequence[str] = ("git",)) -> list[str]:
    """Problems with the objects of commit `sha` in `root`'s object store: the
    commit, its trees and its blobs, each hashed here. [] = all are what their
    ids name."""
    with _Objects(root, git) as objects:
        entries, problems = _read_tree(objects, sha)
        if problems:
            return problems
        seen: set[str] = set()
        for path, (mode, oid) in entries.items():
            if mode == _MODE_GITLINK or oid in seen:
                continue
            seen.add(oid)
            kind, _ = objects.read(oid)
            if kind is None:
                problems.append(f"{_show(path)}: object {oid[:12]} is missing from .git")
            elif kind != "blob":
                problems.append(f"{_show(path)}: object {oid[:12]} is a {kind}, not a blob")
            elif not objects.last_ok:
                problems.append(f"{_show(path)}: object {oid[:12]} in .git does not hash to its id")
        return problems


def verify_worktree(root: str, sha: str, git: Sequence[str] = ("git",)) -> list[str]:
    """Problems between the tracked files on disk under `root` and commit
    `sha`. [] = every tracked file is, byte for byte, the one `sha` holds."""
    with _Objects(root, git) as objects:
        entries, problems = _read_tree(objects, sha)
    if problems:
        return problems
    algo = _algo(sha)
    top = os.fsencode(root)
    dirs_ok: dict[bytes, bool] = {b"": True}
    for path, (mode, oid) in entries.items():
        if mode == _MODE_GITLINK:
            continue
        parent = os.path.dirname(path)
        if not _real_dir(top, parent, dirs_ok):
            problems.append(f"{_show(path)}: {_show(parent)}/ on disk is not a plain directory")
            continue
        full = os.path.join(top, path)
        try:
            st = os.lstat(full)
        except FileNotFoundError:
            problems.append(f"{_show(path)}: missing")
            continue
        except OSError as e:
            problems.append(f"{_show(path)}: cannot be read ({e.strerror})")
            continue
        if mode == _MODE_LINK:
            if not stat.S_ISLNK(st.st_mode):
                problems.append(f"{_show(path)}: should be a symlink")
                continue
            data = os.readlink(full)
        else:
            if not stat.S_ISREG(st.st_mode):
                problems.append(f"{_show(path)}: is not a regular file")
                continue
            try:
                data = _read_nofollow(full)
            except OSError as e:
                problems.append(f"{_show(path)}: cannot be read ({e.strerror})")
                continue
        if _object_id(algo, "blob", data) != oid:
            problems.append(f"{_show(path)}: differs from the release")
    return problems


def summarize(problems: Sequence[str]) -> list[str]:
    """`problems`, the first MAX_LISTED of them and a count of the rest."""
    head = list(problems[:MAX_LISTED])
    if len(problems) > MAX_LISTED:
        head.append(f"... and {len(problems) - MAX_LISTED} more")
    return head


# ── internals ────────────────────────────────────────────────────────────

class _Objects:
    """`git cat-file --batch`, every object hashed as it is read."""

    def __init__(self, root: str, git: Sequence[str]):
        self.root = root
        self.git = list(git)
        self.proc: Optional[subprocess.Popen] = None
        self.last_ok = False

    def __enter__(self) -> "_Objects":
        try:
            self.proc = subprocess.Popen(
                [*self.git, *GIT_SAFE_OPTIONS, "cat-file", "--batch"],
                cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
        except OSError as e:
            raise TreeCheckError(f"could not run git: {e}") from e
        return self

    def __exit__(self, *exc) -> None:
        if self.proc is not None:
            for stream in (self.proc.stdin, self.proc.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def read(self, oid: str) -> tuple[Optional[str], bytes]:
        """(type, content) of object `oid`, or (None, b"") when git does not
        have it. last_ok says whether the content hashes to `oid`."""
        assert self.proc is not None and self.proc.stdin and self.proc.stdout
        self.last_ok = False
        try:
            self.proc.stdin.write(oid.encode() + b"\n")
            self.proc.stdin.flush()
            header = self.proc.stdout.readline()
        except OSError as e:
            raise TreeCheckError(f"git cat-file stopped: {e}") from e
        parts = header.split()
        if not header.endswith(b"\n") or len(parts) not in (2, 3):
            raise TreeCheckError(f"git cat-file stopped answering at {oid[:12]}")
        if len(parts) == 2:  # "<oid> missing" / "<oid> ambiguous"
            return None, b""
        kind, size = parts[1].decode("ascii", "replace"), int(parts[2])
        data = self.proc.stdout.read(size)
        if len(data) != size or self.proc.stdout.read(1) != b"\n":
            raise TreeCheckError(f"git cat-file stopped in the middle of {oid[:12]}")
        self.last_ok = _object_id(_algo(oid), kind, data) == oid
        return kind, data


def _read_tree(objects: _Objects, sha: str) -> tuple[dict[bytes, tuple[bytes, str]], list[str]]:
    """Every entry of commit `sha`'s tree, {path: (mode, oid)} with raw byte
    paths, the commit and each tree object hash-checked. Returns (entries,
    problems)."""
    if not _HEX_ID.fullmatch(sha):
        raise TreeCheckError(f"not a full object id: {sha!r}")
    kind, data = objects.read(sha)
    if kind is None:
        return {}, [f"commit {sha[:12]} is missing from .git (the fetch did not bring it)"]
    if kind != "commit":
        return {}, [f"{sha[:12]} is a {kind}, not a commit"]
    if not objects.last_ok:
        return {}, [f"commit {sha[:12]} in .git does not hash to its id"]
    first = data.split(b"\n", 1)[0]
    if not first.startswith(b"tree "):
        return {}, [f"commit {sha[:12]} names no tree"]
    entries: dict[bytes, tuple[bytes, str]] = {}
    problems: list[str] = []
    pending = [(b"", first[5:].decode("ascii", "replace"))]
    width = len(sha) // 2
    while pending:
        prefix, tree_id = pending.pop()
        if not _HEX_ID.fullmatch(tree_id):
            problems.append(f"{_show(prefix) or 'the commit'}: names a bad tree id")
            continue
        kind, data = objects.read(tree_id)
        where = _show(prefix) or "the top-level tree"
        if kind != "tree":
            problems.append(f"{where}: tree {tree_id[:12]} is "
                            f"{'missing from .git' if kind is None else 'a ' + kind}")
            continue
        if not objects.last_ok:
            problems.append(f"{where}: tree {tree_id[:12]} in .git does not hash to its id")
            continue
        for mode, name, oid in _tree_entries(data, width):
            if not name or b"/" in name or name in (b".", b"..", b".git"):
                problems.append(f"{_show(prefix) or 'the top-level tree'}: holds an entry "
                                f"named {_show(name)!r}, which git never writes")
                continue
            path = prefix + b"/" + name if prefix else name
            if mode == _MODE_TREE:
                pending.append((path, oid))
            elif mode in _MODES_FILE or mode in (_MODE_LINK, _MODE_GITLINK):
                entries[path] = (mode, oid)
            else:
                problems.append(f"{_show(path)}: unknown tree entry mode {_show(mode)}")
    return entries, problems


def _tree_entries(data: bytes, width: int) -> Iterable[tuple[bytes, bytes, str]]:
    """(mode, name, hex id) for each entry of a raw tree object."""
    i = 0
    while i < len(data):
        try:
            space = data.index(b" ", i)
            nul = data.index(b"\0", space)
        except ValueError:
            raise TreeCheckError("a tree object ends in the middle of an entry") from None
        mode = data[i:space]
        name = data[space + 1:nul]
        oid = data[nul + 1:nul + 1 + width].hex()
        if len(oid) != width * 2:
            raise TreeCheckError("a tree object ends in the middle of an entry")
        yield mode, name, oid
        i = nul + 1 + width


def _real_dir(root: bytes, rel: bytes, cache: dict[bytes, bool]) -> bool:
    """True when every directory from `root` down to `rel` is a directory on
    disk and not a symlink to one (a symlinked directory would make the files
    under it come from somewhere else)."""
    if rel in cache:
        return cache[rel]
    ok = _real_dir(root, os.path.dirname(rel), cache)
    if ok:
        try:
            st = os.lstat(os.path.join(root, rel))
            ok = stat.S_ISDIR(st.st_mode)
        except OSError:
            ok = False
    cache[rel] = ok
    return ok


def _read_nofollow(path: bytes) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as f:
        return f.read()


def _show(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _algo(oid: str) -> str:
    return "sha256" if len(oid) == 64 else "sha1"


def _object_id(algo: str, kind: str, data: bytes) -> str:
    h = hashlib.new(algo)
    h.update(f"{kind} {len(data)}\0".encode())
    h.update(data)
    return h.hexdigest()


def main(argv: Sequence[str]) -> int:
    if len(argv) < 3 or argv[0] not in ("objects", "worktree"):
        print("usage: tree_check.py objects|worktree <root> <sha> [<git command...>]",
              file=sys.stderr)
        return 2
    mode, root, sha, git = argv[0], argv[1], argv[2], list(argv[3:]) or ["git"]
    check = verify_objects if mode == "objects" else verify_worktree
    try:
        problems = check(root, sha, git)
    except TreeCheckError as e:
        print(f"could not check: {e}", file=sys.stderr)
        return 2
    for line in summarize(problems):
        print(line)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
