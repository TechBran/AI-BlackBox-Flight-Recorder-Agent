"""One reader for the append-only ledger (audit rank 77: O04-tasks-retrieval-02,
O05-ledger-persistence-02/-03/-18).

The ledger (Volumes/SNAPSHOT_VOLUME.txt) is only ever appended to: a byte,
once written, never changes. So a read-only memory map of the file's first N
bytes stays exact for as long as the file is at least N bytes long, and every
snapshot's byte range from the index can be sliced straight out of it, with no
open, no read() and no copy of the rest of the file.

LedgerReader keeps one map per ledger file, keyed by the file's identity, size
and mtime. A look at the ledger (view()) stats the file: unchanged, it gets
the map already held; after a mint (the file grew) or with a replaced file,
it gets a new map. A view taken before a mint keeps the map it took and the
size it saw, whose bytes the mint's append cannot change, so a read in flight
is never torn by a mint and never sees half of one. Old maps are not closed
by hand (a read may still hold one); each goes when its last view does.

The one way to break a map is to shrink or rewrite the file under it, which
the ledger never does: nothing in the box truncates or rewrites
SNAPSHOT_VOLUME.txt (a byte read past the end of a truncated mapped file is a
SIGBUS). A shrunk file is remapped and reported on the next look.

PrefixHasher keeps a running sha256 of the committed prefix, so the hash a
mint records for "the ledger as this mint left it" costs the appended bytes,
not a pass over the whole file.

Stdlib only, and no Orchestrator.config import: scripts/ledger_fsck.py reads a
box's files through this module without loading the app's configuration.
"""
from __future__ import annotations

import hashlib
import mmap
import os
import threading
from collections import OrderedDict
from typing import Optional, Tuple

# The chunk a whole-prefix pass feeds through sha256 or into a copy at a time.
CHUNK = 8 * 1024 * 1024


class LedgerView:
    """The ledger's bytes [0, size) as they were when the view was taken.

    read() and view[a:b] return bytes; slice() returns a zero-copy memoryview
    (bytes when the file could not be mapped). A missing or empty ledger is a
    view of size 0. Usable as a context manager for symmetry with the readers
    it replaces; there is nothing to close.
    """

    __slots__ = ("path", "size", "dev", "ino", "mtime_ns", "_buf", "_fd_file")

    def __init__(self, path: str, size: int = 0, buf=None, *, dev: int = 0, ino: int = 0,
                 mtime_ns: int = 0, fd_file=None):
        self.path = path
        self.size = size
        self.dev, self.ino, self.mtime_ns = dev, ino, mtime_ns
        self._buf = buf          # the mmap, or None (empty ledger, or no mapping)
        self._fd_file = fd_file  # an open file read with pread when no mapping

    @property
    def ident(self) -> Tuple[str, int, int]:
        """Which file this is: a replaced ledger has another identity."""
        return (self.path, self.dev, self.ino)

    def _clamp(self, start: int, end: int) -> Tuple[int, int]:
        return max(0, int(start)), min(int(end), self.size)

    def read(self, start: int, end: int) -> bytes:
        """Bytes [start, end) clamped to the view; b"" when the range is empty."""
        start, end = self._clamp(start, end)
        if start >= end:
            return b""
        if self._buf is not None:
            return self._buf[start:end]
        if self._fd_file is not None:
            return os.pread(self._fd_file.fileno(), end - start, start)
        return b""

    def slice(self, start: int, end: int):
        """Bytes [start, end) without a copy: a memoryview of the map (decode
        it with str(view, "utf-8", "replace")). The same bytes as read()."""
        start, end = self._clamp(start, end)
        if start >= end:
            return b""
        if self._buf is not None:
            return memoryview(self._buf)[start:end]
        return self.read(start, end)

    def buffer(self):
        """The whole view as one bytes-like object for a regex scan (re
        accepts a map). Never close it: other views share it."""
        if self._buf is not None:
            return self._buf
        return self.read(0, self.size)

    def text(self, start: int, end: int) -> str:
        return self.read(start, end).decode("utf-8", errors="replace")

    def prefix(self, size: int) -> "LedgerView":
        """The same ledger cut at `size` bytes (a committed size measured
        under mint_lock, say): reads past it see nothing."""
        size = max(0, min(int(size), self.size))
        return LedgerView(self.path, size, self._buf, dev=self.dev, ino=self.ino,
                          mtime_ns=self.mtime_ns, fd_file=self._fd_file)

    def __len__(self) -> int:
        return self.size

    def __getitem__(self, key):
        if isinstance(key, slice):
            if key.step not in (None, 1):
                return self.buffer()[key]
            start = 0 if key.start is None else key.start
            end = self.size if key.stop is None else key.stop
            if start < 0:
                start = max(0, self.size + start)
            if end < 0:
                end = max(0, self.size + end)
            return self.read(start, end)
        if key < 0:
            key += self.size
        if not 0 <= key < self.size:
            raise IndexError("ledger view index out of range")
        return self.read(key, key + 1)[0]

    def __enter__(self) -> "LedgerView":
        return self

    def __exit__(self, *exc) -> None:
        return None


def _map(path: str) -> LedgerView:
    try:
        f = open(path, "rb")
    except OSError:
        return LedgerView(path)
    try:
        st = os.fstat(f.fileno())
        meta = {"dev": st.st_dev, "ino": st.st_ino, "mtime_ns": st.st_mtime_ns}
        if st.st_size == 0:
            f.close()
            return LedgerView(path, 0, None, **meta)
        try:
            buf = mmap.mmap(f.fileno(), st.st_size, access=mmap.ACCESS_READ)
        except (OSError, ValueError) as e:
            # A filesystem that cannot map it: pread from the open file.
            print(f"[LEDGER] cannot map {path} ({e}); reading ranges with pread")
            return LedgerView(path, st.st_size, None, fd_file=f, **meta)
        f.close()  # the map holds its own reference to the file
        return LedgerView(path, st.st_size, buf, **meta)
    except BaseException:
        f.close()
        raise


class LedgerReader:
    """Memory maps of ledger files, one per path, reused while the file's
    (device, inode, size, mtime) is unchanged."""

    def __init__(self, max_files: int = 4):
        self._lock = threading.Lock()
        self._views: "OrderedDict[str, Tuple[tuple, LedgerView]]" = OrderedDict()
        self._max = max_files

    def view(self, path) -> LedgerView:
        p = os.path.abspath(os.fspath(path))
        try:
            st = os.stat(p)
        except OSError:
            return LedgerView(p)
        key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        with self._lock:
            held = self._views.get(p)
            if held is not None and held[0] == key:
                self._views.move_to_end(p)
                return held[1]
            view = _map(p)
            if (held is not None and held[1].ident == view.ident
                    and view.size < held[1].size):
                print(f"[LEDGER] WARNING: {p} shrank from {held[1].size} to {view.size} bytes; "
                      f"the ledger is append-only, so its bytes were rewritten")
            self._views[p] = ((view.dev, view.ino, view.size, view.mtime_ns), view)
            self._views.move_to_end(p)
            while len(self._views) > self._max:
                self._views.popitem(last=False)
            return view

    def forget(self, path=None) -> None:
        """Drop the held maps (all, or one path's); open views keep theirs."""
        with self._lock:
            if path is None:
                self._views.clear()
            else:
                self._views.pop(os.path.abspath(os.fspath(path)), None)


READER = LedgerReader()


def view(path) -> LedgerView:
    """The shared reader's view of the ledger at `path`."""
    return READER.view(path)


class PrefixHasher:
    """A running sha256 of a ledger's first `pos` bytes.

    digest_upto(view, n) returns sha256(ledger[:n]). Called in append order
    (every mint does, under mint_lock), each call hashes only the bytes
    appended since the last one. A different file, or an n below the bytes
    already hashed, starts over from byte 0: right, just slower.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._ident: Optional[tuple] = None
        self._pos = 0
        self._h = hashlib.sha256()

    def digest_upto(self, v: LedgerView, upto: int) -> str:
        if upto > v.size:
            raise ValueError(f"cannot hash {upto} bytes of a {v.size}-byte ledger view")
        with self._lock:
            if v.ident != self._ident or upto < self._pos:
                self._ident, self._pos, self._h = v.ident, 0, hashlib.sha256()
            while self._pos < upto:
                end = min(upto, self._pos + CHUNK)
                self._h.update(v.slice(self._pos, end))
                self._pos = end
            return self._h.copy().hexdigest()

    def reset(self) -> None:
        with self._lock:
            self._ident, self._pos, self._h = None, 0, hashlib.sha256()
