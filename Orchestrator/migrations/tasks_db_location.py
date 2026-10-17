"""Startup migration: move the task database out of the served Portal/ tree.

WHAT & WHY (audit rank 24 — NEW-core-1 / X03-security-backend-07): /ui is a
StaticFiles mount of Portal/, and TaskDatabase used to default to
Portal/tasks.db, so `GET /ui/tasks.db` downloaded every operator's prompts,
results and error strings (the file was also 0666). config.TASKS_DB_PATH now
points at a 0700 data/ dir; models.py calls migrate_legacy_tasks_db() BEFORE
the first open, which renames a legacy Portal/tasks.db together with its
-wal/-shm/-journal sidecars. Committed rows can live ONLY in -wal (WAL mode,
not yet checkpointed), so moving the main file alone would lose them.

CONTRACT — migrate_legacy_tasks_db(legacy_db, target_db) returns:
  "fresh"    nothing at either path (fresh box). Only the 0700 dir is created;
             TaskDatabase creates the DB itself.
  "moved"    legacy db + sidecars renamed to the target.
  "already"  target present, no legacy db (every boot after the move).
  "conflict" the target already holds data, or a target sidecar with content
             would be overwritten/mis-paired: NOTHING is moved, logged at ERROR.
             The target stays the live DB; the legacy copy is left for a human.
  "error"    a rename failed (e.g. EXDEV: Portal/ on another filesystem): the
             renames already done are rolled back, logged at ERROR, and the
             caller keeps opening the legacy path so no history disappears.
Every outcome re-applies 0700 to the dir and 0600 to the DB files, so re-runs
are idempotent and a later chmod drift is repaired at the next boot.

ORDER: sidecars move FIRST, the main file LAST. A crash between renames leaves
the legacy db in place with its sidecars already at the target, and the next
boot finishes the move (pairing them again); the reverse order could strand a
-wal beside a db that "already" moved.

Stdlib only, no config import — tests drive it with temp dirs.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)

SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
DIR_MODE = 0o700
FILE_MODE = 0o600


def _sidecar(db: Path, suffix: str) -> Path:
    return db.with_name(db.name + suffix)


def _size(p: Path) -> int:
    return p.stat().st_size if p.exists() else 0


def _holds_data(db: Path) -> bool:
    """Whether a DB at `db` may hold rows. A 0-byte main file whose -wal is
    absent or empty carries no SQLite data (a bare sqlite3.connect followed by
    a failed query leaves exactly that), so it does not count as an existing
    DB. A 0-byte main file WITH -wal content does: a fresh WAL-mode DB keeps
    every page in -wal until the first checkpoint."""
    return _size(db) > 0 or _size(_sidecar(db, "-wal")) > 0


def secure_db_files(db: Path) -> None:
    """chmod the DB and any present sidecars 0600. SQLite creates later -wal/-shm
    files with the main file's mode, so this also covers future sidecars."""
    for p in (db, *(_sidecar(db, s) for s in SIDECAR_SUFFIXES)):
        try:
            if p.exists():
                os.chmod(p, FILE_MODE)
        except OSError as exc:
            logger.warning("[TASKS-DB] could not chmod %s to 0600: %s", p, exc)


def _ensure_private_dir(d: Path) -> None:
    d.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    try:
        os.chmod(d, DIR_MODE)  # mkdir's mode is umask-filtered and skips an existing dir
    except OSError as exc:
        # e.g. a dir someone created as another user: the DB still opens, so
        # report it rather than refuse to start the service over a mode bit.
        logger.error("[TASKS-DB] could not chmod %s to 0700: %s", d, exc)


def migrate_legacy_tasks_db(legacy_db, target_db) -> str:
    """Move legacy_db (+ sidecars) to target_db once. See the module docstring."""
    legacy_db, target_db = Path(legacy_db), Path(target_db)
    _ensure_private_dir(target_db.parent)

    legacy_sidecars = [s for s in SIDECAR_SUFFIXES if _sidecar(legacy_db, s).exists()]

    if not legacy_db.exists():
        if legacy_sidecars:
            logger.warning(
                "[TASKS-DB] orphaned %s next to a missing %s — left in place, not "
                "moved; inspect and delete by hand",
                ", ".join(str(_sidecar(legacy_db, s)) for s in legacy_sidecars), legacy_db,
            )
        secure_db_files(target_db)
        return "already" if target_db.exists() else "fresh"

    if _holds_data(target_db):
        logger.error(
            "[TASKS-DB] both %s and %s exist — NOT moving (that would clobber the "
            "newer DB). The service keeps using %s; reconcile the legacy copy by "
            "hand. It is no longer served at /ui and is now 0600.",
            legacy_db, target_db, target_db,
        )
        secure_db_files(legacy_db)
        secure_db_files(target_db)
        return "conflict"

    # The target db is absent or an empty placeholder. Its sidecars must be
    # disposable too: a -wal/-journal with content would be overwritten by a
    # rename, or (left behind) be replayed into a DB it does not belong to.
    # A -shm is only the WAL index, rebuilt from -wal on open.
    stale: List[Path] = []
    for s in SIDECAR_SUFFIXES:
        p = _sidecar(target_db, s)
        if not p.exists():
            continue
        if s != "-shm" and _size(p) > 0:
            logger.error(
                "[TASKS-DB] %s has content but %s holds no DB — NOT moving %s onto "
                "it; inspect both by hand",
                p, target_db, legacy_db,
            )
            secure_db_files(legacy_db)
            return "conflict"
        stale.append(p)

    moved: List[Tuple[Path, Path]] = []
    try:
        for p in stale:
            p.unlink()
        if target_db.exists():
            target_db.unlink()  # the 0-byte placeholder
        for s in legacy_sidecars:  # sidecars FIRST, the main file LAST
            src, dst = _sidecar(legacy_db, s), _sidecar(target_db, s)
            os.rename(src, dst)
            moved.append((src, dst))
        os.rename(legacy_db, target_db)
        moved.append((legacy_db, target_db))
    except OSError as exc:
        for src, dst in reversed(moved):
            try:
                os.rename(dst, src)
            except OSError:
                logger.exception("[TASKS-DB] rollback of %s -> %s failed", dst, src)
        logger.error(
            "[TASKS-DB] could not move %s -> %s (%s). Left in place and still in "
            "use; both paths must be on the same filesystem. Move it by hand "
            "with the service stopped.",
            legacy_db, target_db, exc,
        )
        secure_db_files(legacy_db)
        return "error"

    secure_db_files(target_db)
    logger.info(
        "[TASKS-DB] moved %s%s -> %s (out of the served Portal/ tree)",
        legacy_db,
        f" (+{', '.join(legacy_sidecars)})" if legacy_sidecars else "",
        target_db,
    )
    return "moved"
