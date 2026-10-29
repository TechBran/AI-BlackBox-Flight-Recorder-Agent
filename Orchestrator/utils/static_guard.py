"""StaticFiles that serves web assets and media only, never runtime data,
server-side source, key/config material or dot-files.

WHY (audit rank 24 — X03-security-backend-07, P07-html-css-01): the /ui mount
served EVERY file under Portal/, which doubled as a data directory, so
/ui/tasks.db (every operator's prompts and results), Portal/package.json and
Portal/styles/css_manifest.json (an absolute home path) were downloadable by
any tailnet/LAN peer. Moving tasks.db out of Portal/ fixes today's file; this
guard makes a future stray file unservable too.

TWO POLICIES, chosen by the first path segment:
  * the code tree (everything outside `media_dirs`) is an ALLOW-LIST of web
    asset suffixes (WEB_ASSET_SUFFIXES: markup, script, style, images, fonts,
    audio/video, text docs). Git fixes what lives there, so a stray config,
    dump, key or extensionless file is refused without having to be named.
    JSON is not a web asset here: the Portal fetches no static JSON (data comes
    from API routes) and the only ones it ever held were internals.
  * `media_dirs` (Portal/uploads/) is a DENY-LIST: it holds whatever operators
    and tools produce or attach (mp3/wav/m4a/mp4/webm/png/jpg/apk/pdf/docx/txt/
    json/bin today, and any extension /upload preserves), fetched by URL from
    the Portal and the Android app, so an allow-list would break the next
    legitimate media type. Archives (zip, tar.gz) are ordinary uploads there.
Both refuse REFUSED_NAME_PATTERNS — SQLite DBs and their -wal/-shm/-journal
sidecars and .bak/.corrupt/.stray copies, server-side code and bytecode, logs,
env/ini/cfg/conf/toml/yaml config, keys, certificates and password stores,
editor and backup copies — plus any dot-file or dot-directory segment,
__pycache__/, node_modules/ and the mount's `refused_dirs` (Portal/artifacts/
has its own /artifacts/{id} route, Portal/dev/ is only served behind [debug]).
Refused paths get the same bare 404 as a missing file.

MEDIA HEADERS: `media_headers`, when given, is called with the headers of
every response whose file resolves into a media dir (the /ui mount passes
portal_csp.harden_upload_headers, which makes uploads inert as script and as
documents). It goes by the file actually opened, so neither the URL's
spelling nor a symlink into uploads/ escapes it.

SYMLINKS: the check runs on the requested path AND on the resolved real path,
so a served-looking name that symlinks to a refused file (or out of the root)
is refused too. That is a check-then-open, so it holds only while no
untrusted local user can write in the served tree — drop_world_write() strips
o+w from the served roots at startup.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import posixpath
import stat
from typing import Callable, FrozenSet, Iterable, Optional, Tuple

from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

REFUSED_NAME_PATTERNS: Tuple[str, ...] = (
    # SQLite: db, -wal/-shm/-journal sidecars, .bak/.corrupt-<ts>/.stray-<ts> copies
    "*.db", "*.db-*", "*.db.*", "*.db3", "*.sqlite*",
    # server-side code and bytecode
    "*.py", "*.pyc", "*.pyo", "*.sh",
    # logs, env and config
    "*.log", "*.log.*", "*.env", "*.ini", "*.cfg", "*.conf", "*.toml", "*.yaml", "*.yml",
    # keys, certificates and password stores
    "*.pem", "*.key", "*.crt", "*.p8", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "*.kdbx", "*.ppk", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    # editor and backup copies
    "*.bak", "*.backup", "*.orig", "*.swp", "*~",
)
REFUSED_DIR_NAMES = frozenset({"__pycache__", "node_modules"})

# What a browser loads from the code tree. Lower-case, with the dot.
WEB_ASSET_SUFFIXES: FrozenSet[str] = frozenset({
    ".html", ".htm", ".js", ".mjs", ".css", ".map", ".wasm", ".webmanifest",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg", ".ico", ".bmp",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".opus", ".flac",
    ".mp4", ".webm", ".mov", ".m4v",
    ".txt", ".md", ".pdf",
})

# Top-level Portal/ dirs served by the deny-list alone (user and tool media).
PORTAL_MEDIA_DIRS: Tuple[str, ...] = ("uploads",)


def portal_refused_dirs(serve_dev_pages: bool) -> Tuple[str, ...]:
    """Top-level Portal/ dirs the /ui mount refuses."""
    return ("artifacts",) if serve_dev_pages else ("artifacts", "dev")


def drop_world_write(*dirs) -> None:
    """Strip o+w from served roots this process owns (the dev box had Portal/
    and Portal/uploads at 0777). Best effort: a mode it cannot change is logged,
    never fatal."""
    for d in dirs:
        try:
            st = os.stat(d)
        except FileNotFoundError:
            continue  # e.g. Portal/uploads on a fresh box, created later
        except OSError as exc:
            logger.warning("[STATIC] could not check %s: %s", d, exc)
            continue
        if not stat.S_ISDIR(st.st_mode) or not st.st_mode & stat.S_IWOTH:
            continue
        if st.st_uid != os.getuid():
            logger.warning("[STATIC] %s is world-writable and not ours to fix", d)
            continue
        try:
            os.chmod(d, stat.S_IMODE(st.st_mode) & ~stat.S_IWOTH)
        except OSError as exc:
            logger.warning("[STATIC] could not remove o+w from %s: %s", d, exc)
            continue
        logger.warning("[STATIC] %s was world-writable — removed o+w", d)


class GuardedStaticFiles(StaticFiles):
    def __init__(
        self,
        *,
        refused_dirs: Iterable[str] = (),
        media_dirs: Iterable[str] = (),
        allowed_suffixes: Optional[Iterable[str]] = WEB_ASSET_SUFFIXES,
        media_headers: Optional[Callable] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.refused_dirs = tuple(d.strip("/").lower() for d in refused_dirs)
        self.media_dirs = tuple(d.strip("/") for d in media_dirs)
        self.media_headers = media_headers
        self.allowed_suffixes = (None if allowed_suffixes is None
                                 else frozenset(s.lower() for s in allowed_suffixes))

    def is_refused(self, rel_path: str, *, is_file: bool = True) -> bool:
        """`rel_path` is relative to the mount directory (either separator).
        `is_file=False` (a directory, or not yet known) skips the suffix
        allow-list, which only names files."""
        # normpath: StaticFiles joins "./index.html" for the mount root's index.
        rel = posixpath.normpath(rel_path.replace(os.sep, "/")).strip("/")
        if rel in ("", "."):
            return False  # the mount root itself (its index.html is checked separately)
        parts = rel.split("/")
        lowered = [seg.lower() for seg in parts]
        if any(seg.startswith(".") or seg in REFUSED_DIR_NAMES for seg in lowered):
            return True
        if lowered[0] in self.refused_dirs:
            return True
        name = lowered[-1]
        if any(fnmatch.fnmatchcase(name, pat) for pat in REFUSED_NAME_PATTERNS):
            return True
        if not is_file or self.allowed_suffixes is None:
            return False
        if len(parts) > 1 and parts[0] in self.media_dirs:
            return False  # media: the deny-list above is the whole policy
        return os.path.splitext(name)[1] not in self.allowed_suffixes

    async def get_response(self, path, scope):
        if self.is_refused(path, is_file=False):
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)

    def _real_rel(self, full_path) -> Optional[str]:
        """`full_path` after symlinks, relative to the mount root it lies in;
        None when it lies in none (follow_symlink=True escaped the root)."""
        real = os.path.realpath(full_path)
        for directory in self.all_directories:
            root = os.path.realpath(directory)
            if os.path.commonpath([real, root]) == root:
                return os.path.relpath(real, root)
        return None

    def lookup_path(self, path):
        full_path, stat_result = super().lookup_path(path)
        if stat_result is None:
            return full_path, stat_result
        is_file = stat.S_ISREG(stat_result.st_mode)
        rel = self._real_rel(full_path)
        if rel is None:
            rel = os.path.basename(os.path.realpath(full_path))
        if self.is_refused(path, is_file=is_file) or self.is_refused(rel, is_file=is_file):
            return "", None
        return full_path, stat_result

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        if self.media_headers is not None and self._in_media_dir(full_path):
            self.media_headers(response.headers)
        return response

    def _in_media_dir(self, full_path) -> bool:
        rel = self._real_rel(full_path)
        if rel is None:
            return False
        first = rel.replace(os.sep, "/").split("/")[0].lower()
        return first in (d.lower() for d in self.media_dirs)
