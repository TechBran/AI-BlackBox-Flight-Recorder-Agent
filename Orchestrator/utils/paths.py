"""Central path-resolver for AI BlackBox.

All code that constructs a filesystem path INSIDE the BlackBox project tree
should use these helpers rather than hardcoding paths or assuming CWD.

The root is determined in this priority:
  1. BLACKBOX_ROOT env var (set by systemd unit, .env, or installer)
  2. Walk up from this file until we find a sentinel (CLAUDE.md + Orchestrator/)

Raises RuntimeError if neither resolves — fail loudly rather than silently
fall back to a path that may not exist on the current machine.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from functools import lru_cache
from urllib.parse import unquote, urlsplit


@lru_cache(maxsize=1)
def blackbox_root() -> Path:
    """Return the absolute Path to the BlackBox project root.

    Cached for process lifetime via @lru_cache; call
    `blackbox_root.cache_clear()` to re-evaluate (useful in tests).

    Resolution priority:
      1. BLACKBOX_ROOT env var (set by systemd unit, .env, or installer)
      2. Walk up from this file looking for sentinels (CLAUDE.md + Orchestrator/)

    Raises RuntimeError if neither resolves — better to fail loudly than
    silently fall back to a path that may not exist on this machine.
    """
    env = os.getenv("BLACKBOX_ROOT")
    if env:
        return Path(env).resolve()

    # Walk up from this file looking for sentinels (skip self, scan parents)
    for parent in Path(__file__).resolve().parents:
        if (parent / "CLAUDE.md").exists() and (parent / "Orchestrator").is_dir():
            return parent

    raise RuntimeError(
        "BLACKBOX_ROOT not configured and project sentinels (CLAUDE.md + Orchestrator/) not found. "
        "Set BLACKBOX_ROOT env var to the absolute path of your blackbox_poc project root."
    )


def resolve(*parts: str) -> Path:
    """Resolve a path under the BlackBox root.

    >>> resolve("Portal", "uploads")
    PosixPath('.../blackbox_poc/Portal/uploads')
    """
    return blackbox_root().joinpath(*parts)


def portal_dir() -> Path:
    return resolve("Portal")


def uploads_dir() -> Path:
    return resolve("Portal", "uploads")


def credentials_dir() -> Path:
    return resolve("credentials")


def manifest_dir() -> Path:
    return resolve("Manifest")


def volume_dir() -> Path:
    return resolve("Volume")


def fossils_dir() -> Path:
    return resolve("Fossils")


def apps_dir() -> Path:
    return resolve("Apps")


# ---------------------------------------------------------------------------
# Upload containment (audit 2026-10-09 rank 16)
#
# Every loader that opens a path named by a request body, a chat message part
# or a model tool call goes through resolve_upload_path(). Media the BlackBox
# serves lives under Portal/uploads (generated files at its top level, agent
# attachments under sessions/<id>/, terminal attachments under terminal/<id>/),
# so that is the only tree a client- or model-supplied path may name. Anything
# else -- '/ui/../.env', '/etc/passwd', a symlink pointing out of the tree --
# is refused BEFORE the file is opened, because the callers' next step is to
# base64 it into a request to a third-party provider.
# ---------------------------------------------------------------------------

IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic", ".heif"})
AUDIO_EXTS = frozenset({".wav", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac",
                        ".webm", ".aiff", ".aif", ".mp4", ".mpga", ".mpeg", ".3gp", ".amr"})
VIDEO_EXTS = frozenset({".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi", ".3gp", ".mpeg", ".mpg"})
DOC_EXTS = frozenset({".pdf", ".txt", ".md", ".csv", ".doc", ".docx"})
MEDIA_EXTS = IMAGE_EXTS | AUDIO_EXTS | VIDEO_EXTS | DOC_EXTS

# Matches the /upload and /upload/session ceiling (MAX_UPLOAD_SIZE): nothing
# larger can have arrived through the API, and every caller reads the whole
# file into memory.
DEFAULT_MAX_UPLOAD_BYTES = 500 * 1024 * 1024
# For loaders that base64 the file INTO a JSON request (chat attachments,
# reference images, Veo's source image): above the inline limits of the
# providers that use it, far below what would balloon the worker's memory.
INLINE_MAX_BYTES = 50 * 1024 * 1024
# Gemini's chat media loaders: its file input docs allow 100 MB of inline data
# per request, counted on the base64 payload, so the largest raw file that can
# fit is 3/4 of that (longer Android overlay screen recordings exceed 50 MB).
GEMINI_INLINE_MAX_BYTES = 100 * 1000 * 1000 * 3 // 4
_MAX_REFERENCE_CHARS = 4096
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_UI_UPLOADS = "/ui/uploads/"


class UploadPathError(ValueError):
    """A client- or model-supplied path that does not name a contained upload.

    Routes turn it into a 400 and tool executors into a failed ToolResult. The
    message never includes the resolved filesystem path.
    """


class UploadNotFoundError(UploadPathError):
    """The reference is well-formed and contained, but no such file exists."""


class UploadNotAcceptedError(UploadPathError):
    """A contained upload that exists, refused only because THIS caller cannot
    take it: larger than its size cap, or an extension outside its allowlist.
    Chat loaders tell the model about these instead of dropping them silently.
    """


def _megabytes(n: int) -> str:
    return f"{n / (1024 * 1024):.0f} MB"


def _describe(value) -> str:
    text = repr(value)
    return text if len(text) <= 120 else text[:117] + "..."


def _relative_candidates(value: str, original) -> list:
    """The uploads-root-relative spellings a non-absolute reference stands for.

    URL forms are tried raw first (the old loaders never decoded) and then
    percent-decoded with any query/fragment dropped, the way StaticFiles serves
    /ui/uploads/... (the terminal attach route quotes the URLs it returns).
    Every candidate is validated identically by the caller.
    """
    scheme = _URL_SCHEME_RE.match(value)
    if scheme:
        if scheme.group(0).lower() not in ("http:", "https:"):
            raise UploadPathError(f"not a BlackBox upload reference: {_describe(original)}")
        # The host is not trusted for anything -- only the path is mapped, and
        # only into the local uploads tree. Tailscale serve, LAN IPs and
        # localhost all name this box, so no host allowlist is attempted.
        path = urlsplit(value).path
        if not path.startswith(_UI_UPLOADS):
            raise UploadPathError(f"URL is not a /ui/uploads/ path: {_describe(original)}")
        return [unquote(path[len(_UI_UPLOADS):])]
    if value.startswith("/ui/"):
        if not value.startswith(_UI_UPLOADS):
            raise UploadPathError(f"not under /ui/uploads/: {_describe(original)}")
        raw = value[len(_UI_UPLOADS):]
        decoded = unquote(urlsplit(value).path)[len(_UI_UPLOADS):]
        return [raw] if decoded == raw else [raw, decoded]
    while value.startswith("./"):
        value = value[2:]
    for prefix in ("Portal/uploads/", "uploads/"):
        if value.startswith(prefix):
            return [value[len(prefix):], value]
    return [value]


def _check_relative(rel: str, original) -> None:
    if not rel:
        raise UploadPathError(f"empty upload path: {_describe(original)}")
    if "\x00" in rel:
        raise UploadPathError(f"NUL byte in upload path: {_describe(original)}")
    if "\\" in rel:
        raise UploadPathError(f"backslash in upload path: {_describe(original)}")
    if rel.startswith("/") or any(part == ".." for part in rel.split("/")):
        raise UploadPathError(f"'..' or absolute segment in upload path: {_describe(original)}")


def resolve_upload_path(value, *, allowed_exts=None, max_bytes=None, root=None) -> Path:
    """Map a client- or model-supplied upload reference to a contained file.

    Accepted spellings (all resolve under ``Portal/uploads`` of
    :func:`blackbox_root`, never the process CWD):

    * ``/ui/uploads/<rel>`` and ``http(s)://<any host>/ui/uploads/<rel>``
    * an absolute path inside ``Portal/uploads`` (agent attachments arrive as
      ``.../Portal/uploads/sessions/<id>/<file>``)
    * ``<rel>``, ``uploads/<rel>`` or ``Portal/uploads/<rel>`` relative to the
      uploads root (``sessions/<id>/...`` and ``terminal/<id>/...`` included)

    Refused with :class:`UploadPathError`: non-strings, absolute paths outside
    the root (even ones whose symlink lands inside), other URL schemes, NUL,
    backslashes, ``..`` segments (percent-encoded too), symlinks escaping the
    root, non-regular files, files over ``max_bytes`` (default
    :data:`DEFAULT_MAX_UPLOAD_BYTES`), and -- when ``allowed_exts`` is given --
    an extension of the RESOLVED file outside it. A contained reference to a
    missing file raises the :class:`UploadNotFoundError` subclass, and the
    size and type refusals raise :class:`UploadNotAcceptedError`.

    Returns the fully resolved :class:`Path`; callers open that, not the input.
    ``root`` overrides the uploads root (the MCP server passes its own).
    """
    if isinstance(value, Path):
        value = str(value)
    if not isinstance(value, str):
        raise UploadPathError(f"upload path must be a string, got {type(value).__name__}")
    original = value
    value = value.strip()
    if not value:
        raise UploadPathError("empty upload path")
    if len(value) > _MAX_REFERENCE_CHARS:
        raise UploadPathError("upload path is too long")
    if "\x00" in value:
        raise UploadPathError(f"NUL byte in upload path: {_describe(original)}")
    if "\\" in value:
        raise UploadPathError(f"backslash in upload path: {_describe(original)}")

    base = Path(root) if root is not None else uploads_dir()
    if not base.is_absolute():
        base = Path.cwd() / base
    try:
        real_base = base.resolve()
    except (OSError, RuntimeError):
        raise UploadPathError("uploads folder is unavailable") from None

    if value.startswith("/") and not value.startswith("/ui/"):
        # Absolute filesystem path: it must LEXICALLY sit inside the root (as
        # configured or as resolved) before any symlink is followed.
        if any(part == ".." for part in value.split("/")):
            raise UploadPathError(f"'..' segment in upload path: {_describe(original)}")
        candidate = Path(os.path.normpath(value))
        anchor = next((a for a in (base, real_base) if candidate.is_relative_to(a)), None)
        if anchor is None or candidate == anchor:
            raise UploadPathError(f"path is outside the uploads folder: {_describe(original)}")
        candidates = [candidate.relative_to(anchor).as_posix()]
    else:
        candidates = _relative_candidates(value, original)

    missing = None
    for rel in candidates:
        _check_relative(rel, original)
        try:
            resolved = (real_base / rel).resolve(strict=True)
        except FileNotFoundError:
            missing = UploadNotFoundError(f"upload not found: {_describe(original)}")
            continue
        except (OSError, RuntimeError):
            raise UploadPathError(f"unreadable upload path: {_describe(original)}") from None
        if resolved == real_base or not resolved.is_relative_to(real_base):
            raise UploadPathError(f"path escapes the uploads folder: {_describe(original)}")
        try:
            st = resolved.stat()
        except OSError:
            raise UploadPathError(f"unreadable upload path: {_describe(original)}") from None
        if not stat.S_ISREG(st.st_mode):
            raise UploadPathError(f"not a regular file: {_describe(original)}")
        if allowed_exts is not None and resolved.suffix.lower() not in allowed_exts:
            raise UploadNotAcceptedError(
                f"file type {resolved.suffix.lower() or '(none)'!r} is not allowed here: "
                f"{_describe(original)}")
        cap = DEFAULT_MAX_UPLOAD_BYTES if max_bytes is None else max_bytes
        if st.st_size > cap:
            raise UploadNotAcceptedError(
                f"file is larger than the {_megabytes(cap)} limit: {_describe(original)}")
        return resolved
    raise missing or UploadPathError(f"not a BlackBox upload reference: {_describe(original)}")


_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._ -]")


def safe_filename(name, *, fallback: str = "file", max_len: int = 120) -> str:
    """Reduce a client- or model-supplied file name to a safe basename.

    Directory parts are dropped, anything outside ``[A-Za-z0-9._ -]`` becomes
    ``_``, leading/trailing dots and spaces are stripped (no hidden files, no
    ``..``), and an over-long name is cut while keeping a short extension.
    """
    base = str(name if name is not None else "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _UNSAFE_FILENAME_CHARS.sub("_", base).strip(" .")
    if not base:
        base = fallback
    if len(base) > max_len:
        stem, dot, ext = base.rpartition(".")
        if dot and stem and len(ext) <= 10:
            base = stem[: max_len - len(ext) - 1].rstrip(" .") + "." + ext
        else:
            base = base[:max_len]
    return base
