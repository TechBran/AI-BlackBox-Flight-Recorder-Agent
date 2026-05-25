"""
Orchestrator-owned mapping of (operator, provider, app) → zellij session
metadata.

State stored at ``Orchestrator/cli_agent/state/zellij_sessions.json``.

SECURITY (audit I7): NEVER stores raw token values. Only ``token_name``
(Zellij-assigned ``token_1``, ``token_2``, etc.). Raw token UUIDs exist
only transiently in launch-response payloads.

Acceptance criterion (audit I7): grep ``Orchestrator/cli_agent/state/``
post-launch finds zero UUID-shaped strings.

Schema per row::

    {
      "operator": str,             # "Brandon", "Brandon-DEV", ...
      "provider": str,             # "claude" | "gemini" | "codex" | "agy" | "terminal"
      "app": str | None,           # Apps/{name} context, or None for root
      "session_name": str,         # "{op}__{provider}__{app_or_root}"
      "token_name": str,           # Zellij-assigned name (e.g., "token_3")
      "created_at": str,           # ISO-8601
      "expires_at": str | None,    # ISO-8601 for short-lived; None for terminal
    }
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from pathlib import Path
from typing import Optional

from . import zellij_client

logger = logging.getLogger(__name__)

# Module-level state directory + file. Kept under cli_agent/state/ so the
# `state/` directory is the single place to grep for "is there any raw
# secret material on disk" (audit I7 acceptance).
_STATE_DIR = Path(__file__).resolve().parent / "state"
_STATE_PATH = _STATE_DIR / "zellij_sessions.json"

# Zellij stores per-token hashes here. reconcile_or_wipe() removes this
# file as part of the wipe path. NOT read directly — only used to clean
# up. Token enumeration goes through zellij_client.list_tokens().
_ZELLIJ_TOKENS_DB = Path.home() / ".local" / "share" / "zellij" / "tokens.db"


# --- I/O primitives -------------------------------------------------------


def _now_iso() -> str:
    """UTC ISO-8601 timestamp without microseconds — matches the rest of
    the orchestrator's state-file style."""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def load() -> list[dict]:
    """Load state from JSON. Returns ``[]`` if the file does not exist or
    is corrupt (logged loudly)."""
    if not _STATE_PATH.exists():
        return []
    try:
        text = _STATE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("zellij_state.load: cannot read %s: %s", _STATE_PATH, exc)
        return []
    if not text.strip():
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error(
            "zellij_state.load: corrupt JSON in %s (%s) — treating as empty",
            _STATE_PATH,
            exc,
        )
        return []
    if not isinstance(data, list):
        logger.error(
            "zellij_state.load: %s root is %s, expected list — "
            "treating as empty",
            _STATE_PATH,
            type(data).__name__,
        )
        return []
    return data


def save(rows: list[dict]) -> None:
    """Persist state to JSON atomically (write to .tmp + os.replace).

    The atomic write means a crash mid-save can never leave a
    half-written file; the worst case is the previous valid file
    surviving.
    """
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = _STATE_PATH.with_suffix(_STATE_PATH.suffix + ".tmp")
    serialized = json.dumps(rows, indent=2, sort_keys=True) + "\n"
    tmp_path.write_text(serialized, encoding="utf-8")
    os.replace(tmp_path, _STATE_PATH)
    logger.debug("zellij_state.save: wrote %d row(s) to %s", len(rows), _STATE_PATH)


# --- mutation helpers ----------------------------------------------------


def add_session(
    operator: str,
    provider: str,
    app: Optional[str],
    session_name: str,
    token_name: str,
    expires_at: Optional[str],
) -> None:
    """Append (or update) a session row. Idempotent on
    ``(operator, session_name)`` — if a matching row already exists, its
    ``token_name`` and ``expires_at`` are refreshed in place rather than
    duplicated.

    SECURITY (audit I7): the parameter is ``token_name`` not
    ``token_value``. Callers MUST pass the Zellij-assigned name
    (``token_1``, etc.), NOT the raw UUID returned by
    ``zellij_client.mint_token()``. The UUID lives only in the
    launch-response payload and dies with the in-memory request scope.
    """
    rows = load()
    now = _now_iso()
    matched = False
    for row in rows:
        if row.get("operator") == operator and row.get("session_name") == session_name:
            row["token_name"] = token_name
            row["expires_at"] = expires_at
            # Preserve original created_at on update; rewrite provider/app
            # in case the caller renamed (defensive — should not happen
            # given the prefix discipline in route handlers).
            row["provider"] = provider
            row["app"] = app
            matched = True
            break
    if not matched:
        rows.append({
            "operator": operator,
            "provider": provider,
            "app": app,
            "session_name": session_name,
            "token_name": token_name,
            "created_at": now,
            "expires_at": expires_at,
        })
    save(rows)
    logger.info(
        "zellij_state.add_session: operator=%s session=%s token_name=%s "
        "(updated=%s)",
        operator,
        session_name,
        token_name,
        matched,
    )


def remove_session(session_name: str) -> None:
    """Remove the row matching ``session_name``. Idempotent — removing a
    session that does not exist is a logged no-op."""
    rows = load()
    new_rows = [r for r in rows if r.get("session_name") != session_name]
    if len(new_rows) == len(rows):
        logger.debug(
            "zellij_state.remove_session: %s not found (idempotent no-op)",
            session_name,
        )
        return
    save(new_rows)
    logger.info("zellij_state.remove_session: removed %s", session_name)


def list_for_operator(operator: str) -> list[dict]:
    """Return rows where ``operator`` matches. Used by the
    ``/cli-agent/zellij/sessions`` endpoint to scope the list to the
    requesting operator (audit I8)."""
    return [r for r in load() if r.get("operator") == operator]


# --- reconciliation (audit C3) -------------------------------------------


def _wipe(reason: str) -> None:
    """Wipe both state file and Zellij's tokens.db. Logs the reason
    loudly so operators reading journalctl after a session-disappeared
    incident know what happened."""
    logger.warning(
        "zellij_state: WIPING state + tokens.db (reason: %s)",
        reason,
    )
    # Wipe orchestrator state file first — even if the tokens.db unlink
    # fails (permissions, exotic FS), at least the orchestrator side is
    # consistent and the next launch will be clean.
    try:
        if _STATE_PATH.exists():
            _STATE_PATH.unlink()
            logger.info("zellij_state: removed %s", _STATE_PATH)
    except OSError as exc:
        logger.error(
            "zellij_state: failed to remove %s: %s",
            _STATE_PATH,
            exc,
        )
    try:
        if _ZELLIJ_TOKENS_DB.exists():
            _ZELLIJ_TOKENS_DB.unlink()
            logger.info("zellij_state: removed %s", _ZELLIJ_TOKENS_DB)
    except OSError as exc:
        logger.error(
            "zellij_state: failed to remove %s: %s",
            _ZELLIJ_TOKENS_DB,
            exc,
        )


def reconcile_or_wipe() -> None:
    """Compare orchestrator state ↔ Zellij ``tokens.db`` at orchestrator
    startup (audit C3).

    Four cases:

    1. Both clean (no state rows, no Zellij tokens)        → no-op.
    2. Both populated and ``token_name``s match            → no-op.
    3. Both populated but ``token_name``s mismatch         → WIPE both.
    4. One populated and the other empty                   → WIPE both.

    The wipe is the safer default for a fresh-customer-install or a
    reinstall with mismatched state — an orphaned token row points at a
    session whose backend may or may not still exist, and trying to
    salvage a partial mapping is more dangerous than starting clean.
    """
    state_rows = load()
    state_token_names = {
        row.get("token_name")
        for row in state_rows
        if row.get("token_name")
    }

    try:
        zellij_tokens = zellij_client.list_tokens()
    except Exception as exc:  # noqa: BLE001 — defensive, daemon may be down
        logger.warning(
            "zellij_state.reconcile_or_wipe: cannot list zellij tokens (%s) "
            "— skipping reconcile this boot. orchestrator will retry next start.",
            exc,
        )
        return

    zellij_token_names = {t["name"] for t in zellij_tokens if "name" in t}

    state_present = bool(state_token_names)
    zellij_present = bool(zellij_token_names)

    if not state_present and not zellij_present:
        logger.info(
            "zellij_state.reconcile_or_wipe: both state and tokens.db clean "
            "(no-op)"
        )
        return

    if state_present and zellij_present:
        if state_token_names == zellij_token_names:
            logger.info(
                "zellij_state.reconcile_or_wipe: state and tokens.db match "
                "(%d token(s); no-op)",
                len(state_token_names),
            )
            return
        only_in_state = sorted(state_token_names - zellij_token_names)
        only_in_zellij = sorted(zellij_token_names - state_token_names)
        _wipe(
            f"mismatch — only_in_state={only_in_state} "
            f"only_in_zellij={only_in_zellij}"
        )
        return

    # Exactly one side populated.
    if state_present and not zellij_present:
        _wipe(
            f"state has {len(state_token_names)} token(s) "
            "but tokens.db is empty"
        )
    else:
        _wipe(
            f"tokens.db has {len(zellij_token_names)} token(s) "
            "but state is empty"
        )
