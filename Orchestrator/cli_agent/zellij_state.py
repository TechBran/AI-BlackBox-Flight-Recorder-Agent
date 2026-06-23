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
import threading
from pathlib import Path
from typing import Optional

from . import zellij_client

logger = logging.getLogger(__name__)

# Module-level lock guarding read-modify-write sequences. FastAPI runs
# request handlers concurrently — two `/launch` requests within
# milliseconds would otherwise both `load()`, append, `save()` and lose
# one row (second writer wins; os.replace is atomic at the FS level but
# does NOT serialize the surrounding read-modify-write).
#
# threading.Lock (not asyncio.Lock) because callers wrap state mutators
# in `asyncio.to_thread(...)` — they run on worker threads, not on the
# event loop. An asyncio.Lock would force every caller to be async.
#
# Read-only helpers (load, list_for_operator) DO NOT take the lock — at
# worst they observe a snapshot from before or after another writer's
# os.replace, never a torn read.
_STATE_LOCK = threading.Lock()

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

    Acquires ``_STATE_LOCK`` for the full read-modify-write so concurrent
    callers cannot lose updates.
    """
    with _STATE_LOCK:
        rows = load()
        now = _now_iso()
        matched = False
        for row in rows:
            if row.get("operator") == operator and row.get("session_name") == session_name:
                row["token_name"] = token_name
                row["expires_at"] = expires_at
                # Preserve original created_at on update; rewrite
                # provider/app in case the caller renamed (defensive —
                # should not happen given the prefix discipline in route
                # handlers).
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
    session that does not exist is a logged no-op.

    Acquires ``_STATE_LOCK`` for the full read-modify-write so concurrent
    callers cannot resurrect a removed row.
    """
    with _STATE_LOCK:
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


def _expired(row: dict, now: "_dt.datetime") -> bool:
    """True iff a row carries an ``expires_at`` that is in the past.

    Terminal rows have ``expires_at is None`` (never expire). Short-lived
    rows (legacy per-session-token model) carried an ISO-8601 expiry; a
    past expiry means the row is stale and reconcile may drop it.
    """
    raw = row.get("expires_at")
    if not raw:
        return False
    try:
        ts = _dt.datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        # Unparseable expiry -> treat as expired (defensive: don't keep a
        # row we can't reason about).
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    return ts <= now


def reconcile_or_wipe() -> None:
    """Reconcile orchestrator state against LIVE zellij sessions at startup,
    PRESERVING terminal rows whose underlying session survived the restart
    (audit C3 + Phase 2 survive-orchestrator-restart).

    .. IMPORTANT::
       This MUST run during orchestrator startup BEFORE any route handler
       is reachable (FastAPI defers request acceptance until startup
       events complete — see startup_cli_agent_zellij). Running concurrent
       with ``add_session`` / ``launch_session`` risks classifying a
       freshly-created session as orphan. The ``_STATE_LOCK`` serializes
       against the state mutators, but NOT against zellij's own session
       lifecycle, so "reconcile fully done before /launch is reachable" is
       the only safe sequencing.

    Why this changed (Phase 2): the master-token model (2026-05-26) made
    EVERY state row carry the constant ``token_name="master"`` placeholder
    instead of a per-session token. The OLD token-set comparison therefore
    never matched the real ``tokens.db`` (which holds ``master-blackbox``),
    so it WIPED all terminal rows AND the persistent master token on every
    restart — destroying still-live terminal sessions and forcing a
    master-token re-mint. That defeats the locked requirement that a
    session survive an orchestrator restart.

    New policy — drive off LIVE SESSIONS, not tokens:

    - For each state row, ask zellij whether a session of that name still
      EXISTS (running OR exited-and-resurrectable per ``list_sessions``).
    - PRESERVE a row iff its session exists AND it is not an expired
      short-lived row. Such a row is a live terminal the user can resume.
    - DROP (do not preserve) a row whose session is gone (genuinely
      orphaned — killed out-of-band) or whose ``expires_at`` is past.
    - The persistent master token in ``tokens.db`` is NEVER wiped here —
      it is intentionally long-lived (ensure_master_zellij_token loads,
      not re-mints, across restarts).

    Fresh-install / corrupt-state safety is preserved: a row with no
    backing zellij session is still discarded, and if zellij is
    unreachable we skip reconcile this boot (no destructive action on
    uncertainty).
    """
    with _STATE_LOCK:
        state_rows = load()

        if not state_rows:
            logger.info(
                "zellij_state.reconcile_or_wipe: no state rows — nothing to "
                "reconcile (no-op; persistent master token left intact)"
            )
            return

        # Enumerate live zellij sessions (running + exited-resurrectable).
        try:
            sessions = zellij_client.list_sessions()
        except Exception as exc:  # noqa: BLE001 — daemon may be down at boot
            logger.warning(
                "zellij_state.reconcile_or_wipe: cannot list zellij sessions "
                "(%s) — skipping reconcile this boot (preserving existing "
                "state; orchestrator retries next start)",
                exc,
            )
            return

        live_names = {s.get("name") for s in sessions if s.get("name")}
        now = _dt.datetime.now(_dt.timezone.utc)

        kept: list[dict] = []
        dropped_orphan: list[str] = []
        dropped_expired: list[str] = []
        for row in state_rows:
            name = row.get("session_name")
            if not name:
                continue
            if _expired(row, now):
                dropped_expired.append(name)
                continue
            if name in live_names:
                kept.append(row)
            else:
                dropped_orphan.append(name)

        if dropped_orphan or dropped_expired:
            logger.warning(
                "zellij_state.reconcile_or_wipe: dropping stale rows — "
                "orphaned (no live session)=%s expired=%s; preserving %d "
                "live terminal row(s)",
                sorted(dropped_orphan),
                sorted(dropped_expired),
                len(kept),
            )
            save(kept)
        else:
            logger.info(
                "zellij_state.reconcile_or_wipe: all %d state row(s) backed "
                "by a live zellij session — preserved across restart (no-op)",
                len(kept),
            )
