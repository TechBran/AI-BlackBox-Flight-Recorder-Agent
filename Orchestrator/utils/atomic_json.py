"""One atomic, locked, corrupt-safe file store for the box's small runtime
stores (audit rank 47: X05-architecture-05/-06, O05-ledger-persistence-12,
O12-messaging-08, O15-scheduler-admin-07/-10, O16-onboarding-update-14,
O19-local-stack-12, O20-mcp-server-12, X03-security-backend-14).

Operator preferences and state, the media index, contacts, the MCP token and
OAuth stores, device attestations, Gmail tokens, .env and config.ini all go
through here, so each gets the same guarantees custom_servers.py pioneered:

  * atomic: new content goes to a private temp sibling (0600 from creation,
    never briefly world-readable), is fsync'ed, then os.replace()d over the
    target and the directory fsync'ed. A reader sees the old file or the new
    one, never half of one, and a crash or power cut mid-write loses nothing.
  * locked: update_json() and locked() hold a per-path lock across the whole
    read-modify-write: a reentrant threading lock for this process's threads,
    and for a store another process writes too (cross_process=True), an
    fcntl.flock on a hidden '.<name>.lock' sibling.
  * corrupt-safe: a file that does not parse, or fails the caller's shape
    check, is renamed to '<name>.corrupt-<UTC>' and the caller gets its
    default, so the next save cannot destroy what was there.
  * never overwrites a corrupt file: write_json() checks the file it is about
    to replace and quarantines it first; when even that rename fails, it
    raises CorruptStoreError and writes nothing.

Stdlib only: the MCP server's lean venv imports it too.
"""
from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import stat
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

try:  # POSIX; elsewhere only the in-process lock applies
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

logger = logging.getLogger(__name__)

SECRET_MODE = 0o600


class StoreError(Exception):
    """A store could not be read safely, so nothing may be written over it."""

    def __init__(self, path: str, message: str):
        self.path = str(path)
        super().__init__(message)


class CorruptStoreError(StoreError):
    """The store does not parse and could not be moved aside."""


class UnreadableStoreError(StoreError):
    """The store exists but reading it failed (permissions, I/O error)."""


def _target(path) -> str:
    """The file a write lands in. A symlinked store keeps its link: the write
    goes to the file the link names, as an in-place open('w') did."""
    return os.path.realpath(os.fspath(path))


# ------------------------------------------------------------------ locking

class _PathLock:
    """Reentrant per-path lock: an RLock for this process's threads, plus,
    when the outermost holder asks for it, an exclusive flock on a hidden
    '.<name>.lock' sibling for other processes. Only the outermost holder
    takes the flock (a second flock from this process would block on itself).
    """

    def __init__(self, target: str):
        directory, name = os.path.split(target)
        self._lockfile = os.path.join(directory, f".{name}.lock")
        self._rlock = threading.RLock()
        self._depth = 0
        self._fd: Optional[int] = None

    def acquire(self, cross_process: bool) -> None:
        self._rlock.acquire()
        try:
            if self._depth == 0 and cross_process and fcntl is not None:
                try:
                    os.makedirs(os.path.dirname(self._lockfile) or ".", exist_ok=True)
                    fd = os.open(self._lockfile, os.O_RDWR | os.O_CREAT, SECRET_MODE)
                except OSError as exc:
                    # A read-only directory: the threading lock still holds and
                    # the write itself will fail loudly.
                    logger.debug("atomic_json: no lock file %s (%s)", self._lockfile, exc)
                else:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX)
                    except OSError:
                        os.close(fd)
                        raise
                    self._fd = fd
            self._depth += 1
        except BaseException:
            self._rlock.release()
            raise

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        self._rlock.release()


_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(target: str) -> _PathLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(target)
        if lock is None:
            lock = _LOCKS[target] = _PathLock(target)
        return lock


@contextlib.contextmanager
def locked(path, *, cross_process: bool = False) -> Iterator[None]:
    """Hold the store's lock: around any read-modify-write that is not a
    single update_json(). Reentrant. `cross_process=True` also serializes
    against other processes (a flock on '.<name>.lock' beside the store):
    use it for a store another process writes too, such as mcp_tokens.json
    (the backend and MCP/deploy/mint_token.py). Every writer of such a store
    must pass it."""
    lock = _lock_for(_target(path))
    lock.acquire(cross_process)
    try:
        yield
    finally:
        lock.release()


# --------------------------------------------------------------- quarantine

def quarantine(path) -> Optional[str]:
    """Rename a corrupt store to '<name>.corrupt-<UTC>' (never over an
    earlier quarantined copy). Returns the new path, or None when the rename
    failed or the file is already gone."""
    target = _target(path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = f"{target}.corrupt-{stamp}"
    n = 1
    while os.path.lexists(dest):
        dest = f"{target}.corrupt-{stamp}-{n}"
        n += 1
    try:
        os.rename(target, dest)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.error("atomic_json: could not quarantine %s (%s)", target, exc)
        return None
    return dest


# ------------------------------------------------------------------ reading

_MISSING = object()


def _parse(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"))


def _read(target: str, default: Any, valid: Optional[Callable[[Any], bool]],
          strict: bool) -> Any:
    try:
        with open(target, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return copy.deepcopy(default)
    except OSError as exc:
        if strict:
            raise UnreadableStoreError(target, f"cannot read {target}: {exc}") from exc
        logger.warning("atomic_json: unreadable store %s (%s), using the default", target, exc)
        return copy.deepcopy(default)
    problem = None
    try:
        data = _parse(raw)
    except (ValueError, UnicodeDecodeError) as exc:  # JSONDecodeError is a ValueError
        problem = f"does not parse ({exc})"
    else:
        if valid is not None and not valid(data):
            problem = "has the wrong shape"
    if problem is None:
        return data
    moved = quarantine(target)
    if moved is None and os.path.lexists(target):
        if strict:
            raise CorruptStoreError(
                target, f"{target} {problem} and could not be moved aside; "
                        "refusing to write over it")
        logger.error("atomic_json: %s %s and could not be quarantined", target, problem)
        return copy.deepcopy(default)
    logger.warning("atomic_json: %s %s; quarantined to %s, starting from the default",
                   target, problem, moved)
    return copy.deepcopy(default)


def read_json(path, default: Any = None, *, valid: Optional[Callable[[Any], bool]] = None) -> Any:
    """The store's content, for a reader. Never raises.

    A missing file yields a copy of `default`; a corrupt one (bad JSON, bad
    UTF-8, or `valid(data)` false) is quarantined first, then yields the
    default; an unreadable one is logged and yields the default.
    """
    return _read(_target(path), default, valid, strict=False)


def load_json(path, default: Any = None, *, valid: Optional[Callable[[Any], bool]] = None) -> Any:
    """As read_json, for a caller about to write the store back: raises
    UnreadableStoreError when the file cannot be read and CorruptStoreError
    when a corrupt file could not be moved aside, so it never writes over
    data it could not see."""
    return _read(_target(path), default, valid, strict=True)


# ------------------------------------------------------------------ writing

def _fsync_dir(directory: str) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _final_mode(target: str, mode: Optional[int]) -> int:
    if mode is not None:
        return mode
    try:
        return stat.S_IMODE(os.stat(target).st_mode)
    except OSError:
        return SECRET_MODE


def _replace(target: str, payload: bytes, mode: Optional[int]) -> None:
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    final_mode = _final_mode(target, mode)
    # mkstemp creates the file 0600, owner-only, before a byte is written.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(target)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
            if final_mode != SECRET_MODE:
                os.fchmod(f.fileno(), final_mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


def write_bytes(path, data: bytes, *, mode: Optional[int] = SECRET_MODE) -> None:
    """Atomically replace the file with `data`. `mode` is the file's final
    mode (default 0600); None keeps the existing file's mode (0600 when new).
    No content check: use write_json for a JSON store."""
    _replace(_target(path), data, mode)


def write_text(path, text: str, *, mode: Optional[int] = SECRET_MODE, encoding: str = "utf-8") -> None:
    """write_bytes for text."""
    _replace(_target(path), text.encode(encoding), mode)


def _dumps(data: Any, indent: Optional[int], trailing_newline: bool) -> bytes:
    text = json.dumps(data, indent=indent)
    return (text + "\n" if trailing_newline else text).encode("utf-8")


def _guard_existing(target: str) -> None:
    """Quarantine a corrupt file before it is replaced; refuse when that fails."""
    try:
        with open(target, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise UnreadableStoreError(target, f"cannot read {target} before replacing it: {exc}") from exc
    try:
        _parse(raw)
        return
    except (ValueError, UnicodeDecodeError):
        pass
    moved = quarantine(target)
    if moved is None and os.path.lexists(target):
        raise CorruptStoreError(target, f"{target} does not parse and could not be moved "
                                        "aside; refusing to write over it")
    logger.warning("atomic_json: %s did not parse; quarantined to %s before the write",
                   target, moved)


def write_json(path, data: Any, *, mode: Optional[int] = SECRET_MODE, indent: Optional[int] = 2,
               trailing_newline: bool = False, cross_process: bool = False) -> None:
    """Atomically replace a JSON store with `data` (serialized first, so a
    value json cannot encode leaves the file untouched). A file already there
    that does not parse is quarantined first, never overwritten."""
    target = _target(path)
    payload = _dumps(data, indent, trailing_newline)
    with locked(target, cross_process=cross_process):
        _guard_existing(target)
        _replace(target, payload, mode)


@contextlib.contextmanager
def update_json(path, default: Any = _MISSING, *, valid: Optional[Callable[[Any], bool]] = None,
                mode: Optional[int] = SECRET_MODE, indent: Optional[int] = 2,
                trailing_newline: bool = False, cross_process: bool = False) -> Iterator[Any]:
    """Locked read-modify-write of a JSON store:

        with update_json(path, {}) as data:
            data["key"] = "value"

    Yields the current content (load_json semantics: a corrupt file is
    quarantined and the default yielded; an unreadable one raises) and writes
    it back atomically when the block exits cleanly and the content changed
    (an unchanged store is not rewritten, nor created). An exception in the
    block writes nothing. Mutate the yielded object in
    place; to replace it wholesale, clear() and update() it.
    """
    if default is _MISSING:
        default = {}
    target = _target(path)
    with locked(target, cross_process=cross_process):
        data = load_json(target, default, valid=valid)
        before = json.dumps(data, sort_keys=True)
        yield data
        if json.dumps(data, sort_keys=True) != before:
            _replace(target, _dumps(data, indent, trailing_newline), mode)
