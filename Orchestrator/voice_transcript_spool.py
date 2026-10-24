"""Durable retry spool for voice-session transcripts (audit rank 51,
O09-live-voice-providers-13).

A live-voice session (Portal, Android or phone) saves its transcript once, at
teardown, through POST /chat/save. A failed save used to be "kept for a later
retry" that nothing ever ran: the conversation sat on the session object until
the reaper evicted it, so a service restart, the 30 s client timeout while
perform_mint embedded, a 5xx, or the phone bridge's 10 s save timeout lost the
whole call. save_voice_transcript (routes/voice_ws_shared.py) now writes the
transcript here BEFORE it posts and removes it after a 200, so a failure, a
timeout, a cancelled save or a crash leaves it on disk; the live-session reaper
retries what is pending on every sweep and once right after startup.

One JSON file per transcript, named ``<session id>--<transcript id>.json`` so
the entries of a session are found by its id. Every transcript carries a
unique ``Voice-Transcript-ID:`` line that is saved into the ledger with it: a
save whose 200 never arrived may still have been minted, so before re-posting,
the retry looks for that line in the part of the ledger written since the
transcript was spooled, and drops the entry instead of minting a duplicate.

Stdlib only; Orchestrator.config is read lazily (paths are cwd-relative there).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

# Under the ledger root (the snapshot volume's directory): Volumes/ is untracked
# and kept by updates, and the pending transcripts belong with the ledger they
# are waiting to join. None = resolve from config at call time.
SPOOL_DIR: Optional[Path] = None
SPOOL_SUBDIR = "pending_transcripts"
# The ledger the retry checks for an already-minted transcript. None = config.VOL_PATH.
LEDGER_PATH: Optional[Path] = None

MARKER_PREFIX = "Voice-Transcript-ID: "
REJECTED_SUFFIX = ".rejected.json"
_SCAN_CHUNK = 4 * 1024 * 1024
# A fresh entry is not retried before this: its own POST may still be minting
# server-side after the client gave up (30 s timeout; the phone bridge cancels
# at 10 s), and the ledger check can only see a mint that has finished.
RETRY_SETTLE_SEC = 120.0


def spool_dir() -> Path:
    if SPOOL_DIR is not None:
        return Path(SPOOL_DIR)
    from Orchestrator.config import VOL_PATH
    return Path(VOL_PATH).parent / SPOOL_SUBDIR


def ledger_path() -> Path:
    if LEDGER_PATH is not None:
        return Path(LEDGER_PATH)
    from Orchestrator.config import VOL_PATH
    return Path(VOL_PATH)


def _ledger_size() -> int:
    try:
        return ledger_path().stat().st_size
    except OSError:
        return 0


def _file_key(session_id: str) -> str:
    key = re.sub(r"[^A-Za-z0-9._-]", "_", session_id or "")[:96].strip("._")
    return key or "voice-session"


def new_record(*, session_id: str, operator: str, user_message: str,
               session_summary: str, model_label: str, log_prefix: str) -> dict:
    """Build a spool record; ``assistant_response`` is the summary plus its marker line."""
    transcript_id = uuid.uuid4().hex
    return {
        "transcript_id": transcript_id,
        "session_id": session_id or "",
        "operator": operator,
        "user_message": user_message,
        "assistant_response": f"{session_summary}\n{MARKER_PREFIX}{transcript_id}",
        "model": model_label,
        "log_prefix": log_prefix,
        "created_at": datetime.now(timezone.utc).isoformat(),
        # Ledger size when the save began: a mint of this transcript can only
        # be at or past this offset.
        "ledger_offset": _ledger_size(),
        "attempts": 0,
        "next_attempt_at": time.time() + RETRY_SETTLE_SEC,
    }


def chat_save_body(record: dict) -> dict:
    """The POST /chat/save body for a record."""
    return {
        "operator": record["operator"],
        "user_message": record["user_message"],
        "assistant_response": record["assistant_response"],
        "model": record["model"],
        "tokens": {"prompt": 0, "completion": 0},
    }


def record_path(record: dict) -> Path:
    return spool_dir() / f"{_file_key(record.get('session_id', ''))}--{record['transcript_id']}.json"


def write(record: dict) -> Path:
    """Atomically write (or rewrite) `record`; 0700 dir, 0600 file. Raises OSError."""
    directory = spool_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    target = record_path(record)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def remove(record: dict) -> None:
    try:
        record_path(record).unlink()
    except FileNotFoundError:
        pass


def park_rejected(record: dict) -> Path:
    """Move a transcript the server refused for good out of the retry set.

    The file stays on disk (renamed ``*.rejected.json``) for an operator to
    recover; it is never re-posted.
    """
    path = record_path(record)
    parked = path.with_name(path.name[: -len(".json")] + REJECTED_SUFFIX)
    os.replace(path, parked)
    return parked


def pending() -> Iterator[dict]:
    """Every spooled record awaiting a save, oldest first. Unreadable files are skipped."""
    directory = spool_dir()
    if not directory.is_dir():
        return iter(())
    records = []
    for path in directory.glob("*.json"):
        if path.name.endswith(REJECTED_SUFFIX) or path.name.startswith(".tmp-"):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("transcript_id") and record.get("operator"):
            records.append(record)
    records.sort(key=lambda r: r.get("created_at", ""))
    return iter(records)


def for_session(session_id: str) -> list:
    """The pending records of one session id."""
    return [r for r in pending() if r.get("session_id") == session_id]


def ledger_has(record: dict) -> bool:
    """True when the ledger already holds this transcript's marker line.

    Scans only from the ledger size recorded when the save began (from the
    start when the ledger has since shrunk), in bounded chunks.
    """
    marker = f"{MARKER_PREFIX}{record['transcript_id']}".encode("utf-8")
    path = ledger_path()
    try:
        size = path.stat().st_size
    except OSError:
        return False
    offset = int(record.get("ledger_offset") or 0)
    if offset > size:
        offset = 0
    overlap = len(marker) - 1
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            tail = b""
            while True:
                chunk = f.read(_SCAN_CHUNK)
                if not chunk:
                    return False
                window = tail + chunk
                if marker in window:
                    return True
                tail = window[-overlap:] if overlap > 0 else b""
    except OSError:
        return False


def backoff_seconds(attempts: int, base: float = 60.0, cap: float = 3600.0) -> float:
    return min(base * (2 ** max(0, attempts - 1)), cap)


def mark_failed(record: dict, now: Optional[float] = None) -> dict:
    """Count a failed retry and schedule the next one."""
    now = time.time() if now is None else now
    record["attempts"] = int(record.get("attempts") or 0) + 1
    record["next_attempt_at"] = now + backoff_seconds(record["attempts"])
    return record
