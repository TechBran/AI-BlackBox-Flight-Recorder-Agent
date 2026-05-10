"""Central path-resolver for AI BlackBox.

All code that constructs a filesystem path INSIDE the BlackBox project tree
should use these helpers rather than hardcoding paths or assuming CWD.

The root is determined in this priority:
  1. BLACKBOX_ROOT env var (set by systemd unit, .env, or installer)
  2. Walk up from this file until we find a sentinel (CLAUDE.md + Orchestrator/)
  3. Fall back to /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc
     (the legacy default; warns to log if reached)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from functools import lru_cache

LEGACY_DEFAULT = "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"


@lru_cache(maxsize=1)
def blackbox_root() -> Path:
    """Return the absolute Path to the BlackBox project root."""
    env = os.getenv("BLACKBOX_ROOT")
    if env:
        return Path(env).resolve()

    # Walk up from this file looking for sentinels
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "CLAUDE.md").exists() and (parent / "Orchestrator").is_dir():
            return parent

    # Fallback — warn loudly so we can grep for any caller hitting this
    print(
        f"[paths.py] WARNING: BLACKBOX_ROOT not set and sentinels not found; "
        f"falling back to legacy default {LEGACY_DEFAULT}",
        file=sys.stderr,
    )
    return Path(LEGACY_DEFAULT)


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
