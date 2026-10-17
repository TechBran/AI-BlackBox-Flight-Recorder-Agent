"""StaticFiles that refuses runtime data, server-side source and dot-files.

WHY (audit rank 24 — X03-security-backend-07, P07-html-css-01): the /ui mount
served EVERY file under Portal/, which doubled as a data directory, so
/ui/tasks.db (every operator's prompts and results), Portal/package.json and
Portal/styles/css_manifest.json (an absolute home path) were downloadable by
any tailnet/LAN peer. Moving tasks.db out of Portal/ fixes today's file; this
guard makes a future stray file unservable too.

DENY-LIST, NOT ALLOW-LIST: Portal/uploads/ holds whatever operators and tools
produce or attach (mp3/wav/m4a/mp4/webm/png/jpg/apk/pdf/docx/txt/bin today, and
any extension /upload preserves), and the Portal and the Android app fetch
those by URL. An allow-list of extensions would break the next legitimate media
type; the deny-list names the classes that are never a web asset:
  * SQLite DBs and their -wal/-shm/-journal sidecars and .bak/.corrupt copies
  * server-side code and bytecode (.py/.pyc/.pyo/.sh), logs, .env/.ini config,
    key material (.pem/.key/.p12/.pfx), editor and backup copies
  * every .json — the Portal fetches no static JSON (data comes from API
    routes) and the only ones it ever held were internals; uploads never get
    JSON from the backend (attachments are read server-side from disk)
  * any dot-file or dot-directory segment, __pycache__/, node_modules/
  * mount-specific top-level dirs (`refused_dirs`): Portal/artifacts/ has its
    own /artifacts/{id} route, Portal/dev/ is only served behind [debug].
Refused paths get the same bare 404 as a missing file. The check runs on the
requested path AND on the resolved real path, so a served-looking name that
symlinks to a refused file is refused too.
"""
from __future__ import annotations

import fnmatch
import os
from typing import Iterable, Tuple

from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles

REFUSED_NAME_PATTERNS: Tuple[str, ...] = (
    # SQLite: db, -wal/-shm/-journal sidecars, .bak/.corrupt-<ts> copies
    "*.db", "*.db-*", "*.db.*", "*.sqlite*",
    # server-side code and bytecode
    "*.py", "*.pyc", "*.pyo", "*.sh",
    # logs, env and config
    "*.log", "*.log.*", "*.env", "*.ini",
    # key material
    "*.pem", "*.key", "*.p12", "*.pfx",
    # manifests / indexes / package metadata
    "*.json",
    # editor and backup copies
    "*.bak", "*.backup", "*.orig", "*.swp", "*~",
)
REFUSED_DIR_NAMES = frozenset({"__pycache__", "node_modules"})


def portal_refused_dirs(serve_dev_pages: bool) -> Tuple[str, ...]:
    """Top-level Portal/ dirs the /ui mount refuses."""
    return ("artifacts",) if serve_dev_pages else ("artifacts", "dev")


class GuardedStaticFiles(StaticFiles):
    def __init__(self, *, refused_dirs: Iterable[str] = (), **kwargs) -> None:
        super().__init__(**kwargs)
        self.refused_dirs = tuple(d.strip("/").lower() for d in refused_dirs)

    def is_refused(self, rel_path: str) -> bool:
        """`rel_path` is relative to the mount directory (either separator)."""
        rel = rel_path.replace(os.sep, "/").strip("/").lower()
        if rel in ("", "."):
            return False  # the mount root itself (its index.html is checked separately)
        parts = rel.split("/")
        if any(seg.startswith(".") or seg in REFUSED_DIR_NAMES for seg in parts):
            return True
        if parts[0] in self.refused_dirs:
            return True
        return any(fnmatch.fnmatchcase(parts[-1], pat) for pat in REFUSED_NAME_PATTERNS)

    async def get_response(self, path, scope):
        if self.is_refused(path):
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)

    def lookup_path(self, path):
        full_path, stat_result = super().lookup_path(path)
        if stat_result is None:
            return full_path, stat_result
        real = os.path.realpath(full_path)
        for directory in self.all_directories:
            root = os.path.realpath(directory)
            if os.path.commonpath([real, root]) == root:
                rel = os.path.relpath(real, root)
                break
        else:
            rel = os.path.basename(real)  # follow_symlink=True escaped the root
        if self.is_refused(rel):
            return "", None
        return full_path, stat_result
