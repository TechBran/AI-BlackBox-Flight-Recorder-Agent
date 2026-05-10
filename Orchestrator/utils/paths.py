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
from pathlib import Path
from functools import lru_cache


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
