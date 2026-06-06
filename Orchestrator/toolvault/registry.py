"""ToolVault v2 — module registry (the single source of truth).

Loads + validates every ``ToolVault/tools/<name>/schema.json`` into an in-memory
canonical list. This list replaces the old static ``TOOL_DEFINITIONS``; the chat
injector, MCP server, and static fallbacks all derive from it.

A module's canonical entry IS its ``schema.json`` dict (keys ``name,
description, category, groups, tier, parameters`` + optional ``executor,
returns, example, notes``). A module is included only if its schema validates
(via :func:`schema_spec.validate_module_dict`); modules that fail validation —
bad JSON or schema errors — are excluded from :func:`load_canonical` and
surfaced in :func:`load_errors` (never raised, never silently dropped).

Caching mirrors ``manifest.py``: results are cached in-memory and invalidated
automatically when the max mtime across ``TOOLS_DIR/**/schema.json`` changes
(a cheap stat sweep on each call), or manually via :func:`invalidate_cache`.

``TOOLS_DIR`` is a module global so tests can override it directly
(``registry.TOOLS_DIR = tmp_path``) without env vars; the cache key is computed
from the *current* global on every call, never captured at import time.
"""

import copy
import importlib.util
import inspect
import json
from pathlib import Path
from typing import Callable, Optional

from .config import PROJECT_ROOT
from . import resolvers, schema_spec

# ---------------------------------------------------------------------------
# Where the modules live (overridable by tests — do not capture this at import).
# ---------------------------------------------------------------------------
TOOLS_DIR: Path = PROJECT_ROOT / "ToolVault" / "tools"

# ---------------------------------------------------------------------------
# Alias maps carried over from the old tool_registry.py.
# ---------------------------------------------------------------------------
# alias name -> canonical tool name
_ALIASES = {
    "search_memory": "search_snapshots",
    "get_recent_snapshots": "list_recent_snapshots",
}
# canonical tool name -> executor name (only when they differ)
_EXECUTOR_NAMES = {
    "search_snapshots": "search_memory",
}

# ---------------------------------------------------------------------------
# In-memory cache, keyed on the max schema.json mtime across TOOLS_DIR.
# ---------------------------------------------------------------------------
_cache_canonical: Optional[list] = None
_cache_errors: Optional[dict] = None
_cache_key: Optional[tuple] = None

# ---------------------------------------------------------------------------
# Executor cache. Separate from the schema cache: executors are loaded lazily
# by :func:`get_executor` (one importlib exec per .py), keyed on the file's
# (path, mtime). ``_executor_cache`` maps folder name -> (mtime, callable);
# ``_executor_errors`` maps folder name -> [error strings] for executors that
# FAILED to load (missing ``execute``, not async, wrong arity, or import error).
# A legitimately-absent executor.py is NOT an error and is not recorded here.
# Both are reset by :func:`invalidate_cache` and surface via :func:`load_errors`.
# ---------------------------------------------------------------------------
_executor_cache: dict = {}
_executor_errors: dict = {}


def _mtime_key() -> tuple:
    """Cheap cache key: (TOOLS_DIR path, frozenset of (path, mtime) pairs).

    Keying on the full file SET — not just the max mtime — makes adds, deletes,
    and edits all cache-miss correctly. A max-mtime-only key would miss:

    * deleting a module whose mtime wasn't the max (max stays the same), and
    * adding a module whose mtime is BELOW the current max (e.g. ``git
      checkout`` / ``cp -p`` / ``tar`` / ``rsync`` preserve source mtimes).

    The path is part of the key so a ``TOOLS_DIR`` reassignment (e.g. in tests)
    is treated as a cache miss even if mtimes happen to coincide. A missing dir
    or no modules yields an empty file set.
    """
    tools_dir = TOOLS_DIR
    files = set()
    if tools_dir.exists():
        for path in tools_dir.glob("*/schema.json"):
            try:
                mt = path.stat().st_mtime
            except OSError:
                continue
            files.add((str(path), mt))
    return (str(tools_dir), frozenset(files))


def _build() -> tuple:
    """Scan TOOLS_DIR, validate every module, return (canonical, errors).

    Never raises on a bad module — a parse/validation failure is recorded in
    ``errors`` keyed by folder name; valid modules go into ``canonical``.
    """
    canonical: list = []
    errors: dict = {}

    tools_dir = TOOLS_DIR
    if not tools_dir.exists():
        return canonical, errors

    for folder in sorted(p for p in tools_dir.iterdir() if p.is_dir()):
        folder_name = folder.name
        schema_path = folder / "schema.json"
        if not schema_path.exists():
            continue

        try:
            raw = schema_path.read_text()
            data = json.loads(raw)
        except (OSError, ValueError) as e:  # ValueError covers JSONDecodeError
            errors[folder_name] = [f"failed to load schema.json: {e}"]
            print(f"[toolvault.registry] WARNING: {folder_name}: {e}")
            continue

        errs = schema_spec.validate_module_dict(
            data, folder_name, known_sources=resolvers.KNOWN_SOURCES
        )
        if errs:
            errors[folder_name] = errs
            print(
                f"[toolvault.registry] WARNING: {folder_name} excluded "
                f"({len(errs)} error(s)): {errs}"
            )
            continue

        canonical.append(data)

    # Stable, deterministic order for every consumer.
    canonical.sort(key=lambda d: d.get("name", ""))
    return canonical, errors


def _ensure_cache() -> None:
    """Populate the cache if empty or stale (max-mtime changed)."""
    global _cache_canonical, _cache_errors, _cache_key

    key = _mtime_key()
    if _cache_canonical is not None and key == _cache_key:
        return

    _cache_canonical, _cache_errors = _build()
    _cache_key = key


def invalidate_cache() -> None:
    """Force the next load to re-scan + re-validate every module.

    Also clears the lazily-populated executor cache + executor error store so a
    reload re-imports every ``executor.py`` from scratch.
    """
    global _cache_canonical, _cache_errors, _cache_key
    _cache_canonical = None
    _cache_errors = None
    _cache_key = None
    _executor_cache.clear()
    _executor_errors.clear()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_canonical(group: Optional[str] = None) -> list:
    """Return validated canonical entries (full schema dicts), sorted by name.

    Args:
        group: If given, return only tools whose ``groups`` contains it.

    Returns:
        A list of schema dicts. Empty if ``TOOLS_DIR`` is missing/empty.
    """
    _ensure_cache()
    entries = _cache_canonical or []
    if group is None:
        return [copy.deepcopy(e) for e in entries]
    return [copy.deepcopy(e) for e in entries if group in (e.get("groups") or [])]


def get_tool(name: str) -> Optional[dict]:
    """Return the canonical entry for ``name`` (exact tool name, not alias)."""
    _ensure_cache()
    for entry in _cache_canonical or []:
        if entry.get("name") == name:
            return copy.deepcopy(entry)
    return None


def load_errors() -> dict:
    """Return ``{folder_name: [error strings]}`` for modules that FAILED.

    Merges schema-validation failures (from :func:`load_canonical`'s build) with
    executor-load failures recorded lazily by :func:`get_executor`. A folder
    that has both kinds of failure has its error lists concatenated. Valid
    modules are excluded. Empty dict if there are no failures.
    """
    _ensure_cache()
    merged: dict = {k: list(v) for k, v in (_cache_errors or {}).items()}
    for folder, errs in _executor_errors.items():
        merged.setdefault(folder, []).extend(errs)
    return merged


def get_executor(name: str) -> Optional[Callable]:
    """Load + return the ``execute`` callable for a tool's ``executor.py``.

    ``name`` may be an alias (e.g. ``search_memory``); it is resolved to its
    canonical folder via :func:`resolve_alias`, and the executor is loaded from
    ``TOOLS_DIR/<canonical>/executor.py``.

    Returns:
        The module's ``execute`` coroutine function, or ``None`` if:

        * the ``executor.py`` does not exist (a schema-only tool is valid — this
          is NOT an error and is not recorded), or
        * the module fails to load / lacks a valid ``execute`` (must be an
          ``async def`` taking exactly 2 positional params). Real failures ARE
          recorded under the folder name in :func:`load_errors` and never raised.

    The loaded module + callable are cached keyed on the file's mtime, so an
    on-disk edit (mtime bump) is picked up automatically; :func:`invalidate_cache`
    clears the cache outright.
    """
    canonical = resolve_alias(name)
    exec_path = TOOLS_DIR / canonical / "executor.py"

    # Legitimately absent executor.py: valid schema-only tool, not an error.
    try:
        mtime = exec_path.stat().st_mtime
    except OSError:
        # Drop any stale cache/error entry for a now-missing file.
        _executor_cache.pop(canonical, None)
        _executor_errors.pop(canonical, None)
        return None

    cached = _executor_cache.get(canonical)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    # Stale/first load — clear any prior error for this folder before retrying.
    _executor_errors.pop(canonical, None)
    _executor_cache.pop(canonical, None)

    try:
        spec = importlib.util.spec_from_file_location(
            f"toolvault_executor_{canonical}", str(exec_path)
        )
        if spec is None or spec.loader is None:
            raise ImportError("could not create import spec for executor.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001 — never crash on a bad executor.
        _executor_errors[canonical] = [f"failed to load executor.py: {e}"]
        print(f"[toolvault.registry] WARNING: {canonical} executor: {e}")
        return None

    execute = getattr(module, "execute", None)
    err = _validate_execute(execute)
    if err is not None:
        _executor_errors[canonical] = [err]
        print(f"[toolvault.registry] WARNING: {canonical} executor: {err}")
        return None

    _executor_cache[canonical] = (mtime, execute)
    return execute


def _validate_execute(execute) -> Optional[str]:
    """Return an error string if ``execute`` is not a valid executor, else None.

    Valid == an ``async def`` (``inspect.iscoroutinefunction``) taking exactly
    two positional parameters (``params``, ``ctx``).
    """
    if execute is None:
        return "executor.py defines no 'execute' attribute"
    if not callable(execute):
        return "'execute' is not callable"
    if not inspect.iscoroutinefunction(execute):
        return "'execute' must be an async function (async def)"
    try:
        params = list(inspect.signature(execute).parameters.values())
    except (TypeError, ValueError) as e:
        return f"could not inspect 'execute' signature: {e}"
    positional = [
        p
        for p in params
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) != 2:
        return (
            f"'execute' must accept exactly 2 positional params (params, ctx); "
            f"got {len(positional)}"
        )
    return None


def resolve_alias(name: str) -> str:
    """Map an alias to its canonical tool name (identity if not an alias)."""
    return _ALIASES.get(name, name)


def resolve_executor_name(name: str) -> str:
    """Map a canonical tool name to its executor name (identity if same)."""
    return _EXECUTOR_NAMES.get(name, name)
