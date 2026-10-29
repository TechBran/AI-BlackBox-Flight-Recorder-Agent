"""Scheduled storage maintenance (audit X06-ops-deploy-04,
O05-ledger-persistence-02, rank 77).

Every mint used to write a full copy of the ledger into Archive/ (up to
1 GB a day); a mint now records the ledger's prefix hash instead
(volume.record_append), and the full copy is made here, once a day
([archive] copy_interval_hours), keeping [archive] keep_copies of them. Every
agent attachment lands in a Portal/uploads/sessions/<id>/ folder, and nothing
ever called the upload sweep (the POST /upload/cleanup route existed,
unscheduled). run_storage_maintenance() applies one pass: the scheduled copy,
the archive retention, the upload sweep, the expired-artifact sweep and, once
a day, the ledger check (Orchestrator/ledger_fsck.py) whose counts /health
shows. startup.py runs it on a timer (startup_storage_maintenance, skipped
under BLACKBOX_TESTING) and startup_check_index applies the same retention at
boot.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

from Orchestrator import volume
from Orchestrator.config import UPLOADS_DIR

# Archive retention: the boot cleanup's age and size limits, and the number
# of copies (None: [archive] keep_copies, read fresh; see archive_retention).
ARCHIVE_RETENTION = {"days_to_keep": 30, "max_size_gb": 5.0, "max_files": None}
# Agent-mode attachment folders (Portal/uploads/sessions/<id>/), as documented
# for POST /upload/cleanup.
SESSION_UPLOAD_MAX_AGE_DAYS = 3
# How often the scheduled pass runs.
MAINTENANCE_INTERVAL_S = 3600


def session_uploads_dir() -> Path:
    return UPLOADS_DIR / "sessions"


def archive_retention() -> Dict[str, Any]:
    """The archive retention to apply now: ARCHIVE_RETENTION with the copy
    count from [archive] keep_copies (default 30, a month of daily copies)."""
    retention = dict(ARCHIVE_RETENTION)
    if retention.get("max_files") is None:
        retention["max_files"] = volume.archive_keep_copies()
    return retention


def cleanup_old_session_files(max_age_days: int = SESSION_UPLOAD_MAX_AGE_DAYS,
                              sessions_dir: Optional[Path] = None) -> int:
    """Clean up session folders older than max_age_days.

    Called on the maintenance timer and by POST /upload/cleanup, to prevent
    unbounded file growth. Returns how many folders were removed.
    """
    sessions_dir = sessions_dir or session_uploads_dir()
    if not sessions_dir.exists():
        return 0

    cutoff_time = time.time() - (max_age_days * 24 * 60 * 60)
    cleaned = 0

    for session_folder in sessions_dir.iterdir():
        if session_folder.is_dir():
            # Check folder modification time
            try:
                mtime = session_folder.stat().st_mtime
                if mtime < cutoff_time:
                    shutil.rmtree(session_folder)
                    print(f"[Session Cleanup] Removed old session folder: {session_folder.name}")
                    cleaned += 1
            except Exception as e:
                print(f"[Session Cleanup] Error cleaning {session_folder}: {e}")

    return cleaned


def run_storage_maintenance() -> Dict[str, Any]:
    """One maintenance pass: the scheduled ledger copy, archive retention
    with the free-space floor, the upload session sweep, the expired-artifact
    sweep and the daily ledger check. Each step is independent; a failure is
    logged and reported, never raised."""
    report: Dict[str, Any] = {"archive_copy": None, "archives": None,
                              "upload_sessions_removed": None, "ledger_fsck": None, "errors": []}
    try:
        report["archive_copy"] = volume.scheduled_archive_copy()
        if report["archive_copy"].get("copied"):
            print(f"[MAINTENANCE] Archived the ledger ({report['archive_copy']['bytes']:,} bytes) "
                  f"as {report['archive_copy']['path']}")
    except volume.LedgerError as e:
        report["errors"].append(f"archive copy: {e.detail}")
        print(f"[MAINTENANCE] Archive copy skipped: {e.detail}")
    except Exception as e:
        report["errors"].append(f"archive copy: {e}")
        print(f"[MAINTENANCE] Archive copy failed: {e}")
    try:
        report["archives"] = volume.cleanup_old_archives(
            **archive_retention(), min_free_gb=volume.archive_min_free_gb())
        deleted = report["archives"]["deleted_files"]
        if deleted:
            print(f"[MAINTENANCE] Removed {deleted} archive copies "
                  f"({report['archives']['deleted_size_mb']:.1f} MB; {report['archives']['reason']})")
    except Exception as e:
        report["errors"].append(f"archives: {e}")
        print(f"[MAINTENANCE] Archive cleanup failed: {e}")
    try:
        report["upload_sessions_removed"] = cleanup_old_session_files(
            SESSION_UPLOAD_MAX_AGE_DAYS, session_uploads_dir())
    except Exception as e:
        report["errors"].append(f"upload sessions: {e}")
        print(f"[MAINTENANCE] Upload session cleanup failed: {e}")
    try:
        volume.cleanup_old_artifacts()
    except Exception as e:
        report["errors"].append(f"artifacts: {e}")
        print(f"[MAINTENANCE] Artifact cleanup failed: {e}")
    try:
        if volume.ledger_fsck_due():
            from Orchestrator.ledger_fsck import summary
            fsck = volume.run_ledger_fsck()
            report["ledger_fsck"] = summary(fsck)
            if not fsck["ok"]:
                print(f"[MAINTENANCE] LEDGER CHECK FAILED: {fsck['failures']} problem(s): "
                      f"{fsck['problems'][:5]} (scripts/ledger_fsck.py for the full report)")
    except Exception as e:
        report["errors"].append(f"ledger check: {e}")
        print(f"[MAINTENANCE] Ledger check failed to run: {e}")
    return report
