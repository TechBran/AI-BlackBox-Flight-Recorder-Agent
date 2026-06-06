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
import json
from pathlib import Path
from typing import Optional

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
    """Force the next load to re-scan + re-validate every module."""
    global _cache_canonical, _cache_errors, _cache_key
    _cache_canonical = None
    _cache_errors = None
    _cache_key = None


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

    Valid modules are excluded. Empty dict if there are no failures.
    """
    _ensure_cache()
    return dict(_cache_errors or {})


def resolve_alias(name: str) -> str:
    """Map an alias to its canonical tool name (identity if not an alias)."""
    return _ALIASES.get(name, name)


def resolve_executor_name(name: str) -> str:
    """Map a canonical tool name to its executor name (identity if same)."""
    return _EXECUTOR_NAMES.get(name, name)
