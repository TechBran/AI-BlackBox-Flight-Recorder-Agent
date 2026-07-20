"""Lifecycle for terminal attach-file upload folders (plan Task 5).

``POST /cli-agent/zellij/attach-file`` stores uploads under
``{TERMINAL_UPLOADS_DIR}/{session_name}/``. This module owns that storage
concept plus removal, so upload folders die with their session instead of
leaking forever:

- explicit ``DELETE /cli-agent/zellij/sessions/{name}`` calls
  :func:`remove_for_session` — deterministic resume names
  (``{op}__{provider}__{app}``) are REUSED across kill/relaunch, so a
  relaunched session must start with a clean folder;
- the zellij reaper calls :func:`remove_for_session` for every session it
  kills, then :func:`sweep_orphans` for sessions that died out-of-band
  (e.g. killed from zellij's own session-manager, which never hits the
  DELETE endpoint).

The orphan sweep is age-gated: a folder whose session is gone is only
removed once its mtime is older than the reaper's idle window — grace for
EXITED-but-resurrectable sessions and mid-upload races.

LAYERING: this lives in ``Orchestrator.cli_agent`` so both the routes
layer and the reaper can import it (routes import cli_agent, never the
reverse).
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import List, Optional, Set

from Orchestrator.cli_agent import zellij_client
from Orchestrator.config import UPLOADS_DIR

logger = logging.getLogger(__name__)

# Storage root for terminal attach-file uploads:
#   Portal/uploads/terminal/{session_name}/{filename}
# cli_agent_routes rebinds its _TERMINAL_UPLOADS_DIR global to this value.
TERMINAL_UPLOADS_DIR = UPLOADS_DIR / "terminal"


def _is_plain_name(session_name: str) -> bool:
    """Defense-in-depth: the folder component must be a bare directory
    name that cannot escape the base dir. zellij's session-name charset
    (``is_valid_session_name``) already excludes path separators, but it
    ALLOWS dots — so ``".."`` passes it and ``base / ".."`` would point
    at the base dir's PARENT. Exclude dot-names explicitly."""
    return (
        zellij_client.is_valid_session_name(session_name)
        and session_name not in {".", ".."}
    )


def remove_for_session(session_name: str, base_dir: Optional[Path] = None) -> None:
    """Remove a session's upload folder, if it exists (idempotent,
    best-effort — rmtree with ignore_errors)."""
    base = TERMINAL_UPLOADS_DIR if base_dir is None else base_dir
    if not _is_plain_name(session_name):
        logger.warning(
            "terminal_uploads: refusing folder removal for invalid "
            "session name %r", session_name,
        )
        return
    folder = base / session_name
    if not folder.is_dir():
        return
    shutil.rmtree(folder, ignore_errors=True)
    logger.info(
        "terminal_uploads: removed upload folder for session %s (%s)",
        session_name, folder,
    )


def sweep_orphans(
    live_session_names: Set[str],
    max_age_seconds: float,
    base_dir: Optional[Path] = None,
    now: Optional[float] = None,
) -> List[str]:
    """Remove upload folders whose session no longer exists AND whose
    mtime is older than ``max_age_seconds`` (grace for
    EXITED-but-resurrectable sessions and mid-upload races). Live
    sessions' folders are kept regardless of age. Returns the removed
    folder names; missing base dir is a no-op empty list. One bad folder
    never aborts the sweep."""
    base = TERMINAL_UPLOADS_DIR if base_dir is None else base_dir
    try:
        children = list(base.iterdir())
    except OSError:
        # Base dir missing (no upload ever happened) or unreadable.
        return []
    now_ts = time.time() if now is None else now
    removed: List[str] = []
    for child in children:
        try:
            if not child.is_dir():
                continue
            if child.name in live_session_names:
                continue
            age = now_ts - child.stat().st_mtime
            if age < max_age_seconds:
                continue
            shutil.rmtree(child, ignore_errors=True)
            removed.append(child.name)
            logger.info(
                "terminal_uploads: swept orphan upload folder %s "
                "(age=%ds >= %ds)", child.name, int(age), int(max_age_seconds),
            )
        except Exception as exc:  # noqa: BLE001 — one bad folder must not kill the sweep
            logger.warning(
                "terminal_uploads: sweep skipped %s (%s)", child, exc,
            )
    return removed
