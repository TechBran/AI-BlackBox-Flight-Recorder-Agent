"""Startup migration: move the task database out of the served Portal/ tree.

WHAT & WHY (audit rank 24 — NEW-core-1 / X03-security-backend-07): /ui is a
StaticFiles mount of Portal/, and TaskDatabase used to default to
Portal/tasks.db, so `GET /ui/tasks.db` downloaded every operator's prompts,
results and error strings (the file was also 0666). config.TASKS_DB_PATH now
points at a 0700 data/ dir; models.py calls prepare_tasks_db() BEFORE the first
open, which moves a legacy Portal/tasks.db together with its -wal/-shm/-journal
sidecars. Committed rows can live ONLY in -wal (WAL mode, not yet
checkpointed), so moving the main file alone would lose them.

HOW — safe while ANOTHER process still uses the legacy path. The move runs on
the first import of Orchestrator.models, which is not always the restarted
service: pytest or a script in the serving tree between `git pull` and the
restart imports it while the old service is still writing. And the in-app
Rollback restores code that only knows Portal/tasks.db. So the DB is LINKED,
never renamed out from under a reader, and the legacy name ends up as a
relative compat symlink, Portal/tasks.db -> ../data/tasks.db:
  1. hold a SQLite connection on the legacy DB. SQLite deletes -wal/-shm only
     on the LAST connection's close, so no other process can delete or
     recreate them while the names are re-pointed;
  2. hard-link each sidecar, then the main file, to the target names;
  3. os.replace() a fresh symlink over the legacy name. At every instant
     Portal/tasks.db names the same inode;
  4. unlink the legacy sidecar names, then close the holding connection.
SQLite resolves a symlinked DB name before it derives the -wal/-shm names (the
unix VFS does, when built with readlink/lstat; probed once per move, see
_sqlite_follows_symlinks), so every opener of the legacy path — the old
service's next per-call connection, or the pre-move code a Rollback restores —
reads and writes the SAME data/tasks.db, its -wal and its locks. The link
resolves outside Portal/, so StaticFiles (guarded or not) 404s it.

CONTRACT — migrate_legacy_tasks_db(legacy_db, target_db) returns:
  "fresh"    nothing at either path (fresh box). Only the 0700 dir is created;
             TaskDatabase creates the DB itself.
  "moved"    legacy db + sidecars now live at the target; the legacy name is
             the compat symlink (absent on a SQLite build that does not
             resolve symlinks — logged).
  "already"  the target is the live DB and the legacy name is the compat link,
             absent, or a retired empty stray (every boot after the move).
  "conflict" both paths hold data (task rows, or a file that cannot be read
             as a task DB), or a target sidecar with content would be
             overwritten/mis-paired: NOTHING is moved, logged at ERROR, the
             other copy is left for a human (it is 0600 and refused at /ui).
  "error"    linking failed (e.g. EXDEV: Portal/ on another filesystem, or the
             DB is locked): links already made are removed, logged at ERROR.
prepare_tasks_db() also answers which path this process opens: the target,
except after "error", or a "conflict" whose target is no DB of its own (an
empty or absent main file beside orphaned sidecars, a half-done link, or not
SQLite at all) — then the intact legacy DB, so no history disappears and no
foreign -wal is replayed into a new file.
Every outcome re-applies 0700 to the dir and 0600 to the DB files, so re-runs
are idempotent and a later chmod drift is repaired at the next boot.

SELF-HEALING:
  * a crash mid-move leaves target names that are the SAME inode as the legacy
    files (or a compat link with leftover legacy sidecar names); the next run
    drops and redoes those links (or unlinks the leftovers) and finishes;
  * a DB that holds no task rows (schema-only, no `tasks` table, or 0 bytes) is
    not "an existing DB": a stray one is quarantined to
    data/tasks.db.stray-<UTC> instead of turning every boot into a conflict.

`python -m Orchestrator.migrations.tasks_db_location --status` prints both
paths, their modes and task row counts (the boxes have no sqlite3 CLI).

Stdlib only, no config import — tests drive it with temp dirs.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
DIR_MODE = 0o700
FILE_MODE = 0o600


def _sidecar(db: Path, suffix: str) -> Path:
    return db.with_name(db.name + suffix)


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _swap_link(legacy_db: Path) -> Path:
    """Where the compat symlink is built before os.replace() puts it in place
    (a dot-file: /ui refuses it even if a crash leaves it behind)."""
    return legacy_db.with_name(f".{legacy_db.name}.migrating")


def _is_sqlite(db: Path) -> bool:
    """Whether `db` starts with the SQLite header. Checked before any SQLite
    open: SQLite deletes a -wal beside a main file it cannot use, so a stray
    non-DB file is never opened as one. (Only called while this process holds
    no SQLite connection to the file — closing a plain fd on it would drop that
    connection's POSIX locks.)"""
    try:
        with open(db, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _holds_data(db: Path) -> bool:
    """Whether a DB at `db` may hold rows. A 0-byte main file whose -wal is
    absent or empty carries no SQLite data (a bare sqlite3.connect followed by
    a failed query leaves exactly that), so it does not count as an existing
    DB. A 0-byte main file WITH -wal content is never assumed empty."""
    return _size(db) > 0 or _size(_sidecar(db, "-wal")) > 0


def _task_rows(db: Path) -> Optional[int]:
    """Rows in the `tasks` table of the DB at `db`; 0 when it holds none (absent,
    no data, or no `tasks` table). None when it cannot be read as SQLite, and
    for a 0-byte main file with -wal content: neither is opened at all
    (SQLite would delete that -wal) — None never counts as disposable."""
    if not _holds_data(db):
        return 0
    if not _is_sqlite(db):  # incl. a 0-byte main file with -wal content
        return None
    try:
        conn = sqlite3.connect(str(db), timeout=30)
        try:
            conn.execute("PRAGMA query_only=ON")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='tasks'").fetchone() is None:
                return 0
            return int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def secure_db_files(db: Path, *, log_tag: str = "TASKS-DB") -> None:
    """chmod the DB and any present sidecars 0600. SQLite creates later -wal/-shm
    files with the main file's mode, so this also covers future sidecars. Never
    raises: a mode this process cannot change is logged (``log_tag`` names the
    DB's owner; the scheduler's cron_jobs.db uses it too)."""
    for p in (db, *(_sidecar(db, s) for s in SIDECAR_SUFFIXES)):
        try:
            if p.exists():
                os.chmod(p, FILE_MODE)
        except OSError as exc:
            logger.warning("[%s] could not chmod %s to 0600: %s", log_tag, p, exc)


def _ensure_private_dir(d: Path) -> None:
    d.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    try:
        os.chmod(d, DIR_MODE)  # mkdir's mode is umask-filtered and skips an existing dir
    except OSError as exc:
        # e.g. a dir someone created as another user: the DB still opens, so
        # report it rather than refuse to start the service over a mode bit.
        logger.error("[TASKS-DB] could not chmod %s to 0700: %s", d, exc)


def _sqlite_follows_symlinks(work_dir: Path) -> bool:
    """Whether this SQLite build derives -wal/-shm names from a symlink's
    TARGET. Debian/Ubuntu builds do; an amalgamation built without
    readlink/lstat does not, and there a compat symlink would pair the DB with
    a second -wal beside the link. Proved on a throwaway DB in `work_dir`."""
    probe = Path(tempfile.mkdtemp(prefix=".tasks-db-probe-", dir=str(work_dir)))
    try:
        real = probe / "real.db"
        link = probe / "link.db"
        conn = sqlite3.connect(str(real))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
        conn.close()
        os.symlink(real.name, link)
        conn = sqlite3.connect(str(link))
        try:
            conn.execute("INSERT INTO t VALUES (1)")
            conn.commit()
            return _sidecar(real, "-wal").exists() and not _sidecar(link, "-wal").exists()
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("[TASKS-DB] symlink probe failed (%s); no compat link", exc)
        return False
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def _install_compat_link(legacy_db: Path, target_db: Path) -> None:
    """Atomically make `legacy_db` a relative symlink to `target_db`."""
    tmp = _swap_link(legacy_db)
    if os.path.lexists(tmp):
        tmp.unlink()
    os.symlink(os.path.relpath(target_db, legacy_db.parent), tmp)
    try:
        os.replace(tmp, legacy_db)
    except OSError:
        tmp.unlink()
        raise


def _is_compat_link(legacy_db: Path, target_db: Path) -> bool:
    return (legacy_db.is_symlink()
            and os.path.realpath(legacy_db) == os.path.realpath(target_db))


def _hold_open(db: Path) -> Optional[sqlite3.Connection]:
    """A connection that keeps `db`'s -wal/-shm open, and so undeletable by any
    other process, until it is closed. None when `db` is not SQLite at all (no
    process can be using it as a DB, so there is nothing to hold). A locked DB
    raises sqlite3.OperationalError: the caller must not move it now.

    The DB is put in WAL mode first, as every TaskDatabase connection does. A
    rollback-journal DB is not safe to re-point under a live writer: that
    writer would keep creating its -journal beside the old name."""
    if not _is_sqlite(db):
        return None
    conn = sqlite3.connect(str(db), timeout=30)
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise sqlite3.OperationalError(f"journal_mode is {mode}, not wal")
        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        return conn
    except sqlite3.OperationalError:
        conn.close()
        raise
    except sqlite3.DatabaseError:  # "file is not a database"
        conn.close()
        return None


def _quarantine(db: Path, into: Path) -> Optional[Path]:
    """Rename a DB holding no task rows (+ sidecars) to
    <into>/tasks.db.stray-<UTC>. Returns the new path, or None if it failed."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = into / f"{db.name}.stray-{stamp}"
    if os.path.lexists(dest):
        dest = into / f"{db.name}.stray-{stamp}-{os.getpid()}"
    done: List[Tuple[Path, Path]] = []
    try:
        for s in SIDECAR_SUFFIXES:
            if _sidecar(db, s).exists():
                os.rename(_sidecar(db, s), _sidecar(dest, s))
                done.append((_sidecar(db, s), _sidecar(dest, s)))
        os.rename(db, dest)
    except OSError as exc:
        for src, dst in reversed(done):
            try:
                os.rename(dst, src)
            except OSError:
                logger.exception("[TASKS-DB] rollback of %s -> %s failed", dst, src)
        logger.error("[TASKS-DB] could not quarantine %s: %s", db, exc)
        return None
    secure_db_files(dest)
    return dest


def _settle_compat_link(legacy_db: Path, target_db: Path) -> str:
    """The legacy name is already a symlink: the move happened. Unlink legacy
    sidecar names a crash left behind (only the ones that ARE the target's)."""
    if not _is_compat_link(legacy_db, target_db):
        logger.warning(
            "[TASKS-DB] %s is a symlink to %s, not to %s — left in place; the "
            "service uses %s", legacy_db, os.readlink(legacy_db), target_db, target_db,
        )
    else:
        for s in SIDECAR_SUFFIXES:
            p = _sidecar(legacy_db, s)
            if not os.path.lexists(p):
                continue
            if _same(p, _sidecar(target_db, s)):
                p.unlink()
            else:
                logger.warning("[TASKS-DB] orphaned %s beside the compat link — "
                               "left in place; inspect and delete by hand", p)
    secure_db_files(target_db)
    return "already" if target_db.exists() else "fresh"


def _move(legacy_db: Path, target_db: Path) -> str:
    """Link legacy (+ sidecars) to the target and swap in the compat link. The
    target main is absent, an empty placeholder, or (resume) a link of legacy."""
    stale: List[Path] = []
    for s in SIDECAR_SUFFIXES:
        t = _sidecar(target_db, s)
        if not os.path.lexists(t):
            continue
        # A link of the legacy sidecar is an interrupted move: redo it. A -shm
        # is only the WAL index, rebuilt from -wal on open. Any other -wal or
        # -journal with content would be overwritten, or (left behind) replayed
        # into a DB it does not belong to.
        if not _same(t, _sidecar(legacy_db, s)) and s != "-shm" and _size(t) > 0:
            logger.error(
                "[TASKS-DB] %s has content but no DB beside it — NOT moving %s "
                "onto it. The service keeps using %s; inspect %s by hand.",
                t, legacy_db, legacy_db, t,
            )
            secure_db_files(legacy_db)
            return "conflict"
        stale.append(t)

    try:
        hold = _hold_open(legacy_db)
    except sqlite3.OperationalError as exc:
        logger.error("[TASKS-DB] %s is busy (%s) — not moved this time; the "
                     "service keeps using it and the next start retries",
                     legacy_db, exc)
        secure_db_files(legacy_db)
        return "error"
    try:
        compat = _sqlite_follows_symlinks(target_db.parent)
        # Re-list AFTER the hold: opening a WAL DB creates any missing sidecar.
        sidecars = [s for s in SIDECAR_SUFFIXES if _sidecar(legacy_db, s).exists()]
        linked: List[Path] = []
        try:
            for p in stale:
                p.unlink()
            if os.path.lexists(target_db):
                target_db.unlink()  # the 0-byte placeholder, or a link of legacy
            for s in sidecars:  # sidecars FIRST, the main file LAST
                os.link(_sidecar(legacy_db, s), _sidecar(target_db, s))
                linked.append(_sidecar(target_db, s))
            os.link(legacy_db, target_db)
            linked.append(target_db)
            if compat:
                _install_compat_link(legacy_db, target_db)
            else:
                legacy_db.unlink()
        except OSError as exc:
            for p in reversed(linked):
                try:
                    p.unlink()
                except OSError:
                    logger.exception("[TASKS-DB] could not remove partial link %s", p)
            logger.error(
                "[TASKS-DB] could not move %s -> %s (%s). Left in place and still "
                "in use; both paths must be on the same filesystem. Move it by "
                "hand with the service stopped.", legacy_db, target_db, exc,
            )
            secure_db_files(legacy_db)
            return "error"
        for s in sidecars:
            p = _sidecar(legacy_db, s)
            if _same(p, _sidecar(target_db, s)):
                try:
                    p.unlink()
                except OSError:
                    logger.warning("[TASKS-DB] could not unlink %s (next start retries)", p)
    finally:
        if hold is not None:
            hold.close()

    secure_db_files(target_db)
    logger.info(
        "[TASKS-DB] moved %s%s -> %s (out of the served Portal/ tree)%s",
        legacy_db,
        f" (+{', '.join(sidecars)})" if sidecars else "",
        target_db,
        "" if compat else
        "; this SQLite build does not resolve symlinks, so no compat link was "
        "left and a rollback to pre-move code must move the DB back by hand",
    )
    return "moved"


def migrate_legacy_tasks_db(legacy_db, target_db) -> str:
    """Move legacy_db (+ sidecars) to target_db once. See the module docstring."""
    legacy_db, target_db = Path(legacy_db), Path(target_db)
    _ensure_private_dir(target_db.parent)
    tmp = _swap_link(legacy_db)
    if tmp.is_symlink():
        tmp.unlink()  # a compat link a crash never put in place

    if legacy_db.is_symlink():
        return _settle_compat_link(legacy_db, target_db)

    if not legacy_db.exists():
        orphans = [str(_sidecar(legacy_db, s)) for s in SIDECAR_SUFFIXES
                   if os.path.lexists(_sidecar(legacy_db, s))]
        if orphans:
            logger.warning(
                "[TASKS-DB] orphaned %s next to a missing %s — left in place, not "
                "moved; inspect and delete by hand", ", ".join(orphans), legacy_db,
            )
        secure_db_files(target_db)
        return "already" if target_db.exists() else "fresh"

    if _size(target_db) == 0 or _same(legacy_db, target_db):
        # No target DB yet (absent or a 0-byte placeholder; _move vets any
        # sidecars beside it), or resuming a crash that already linked it.
        return _move(legacy_db, target_db)

    legacy_rows = _task_rows(legacy_db)
    if legacy_rows == 0:
        # A stray with no task rows, e.g. one pre-move code created at a
        # then-missing legacy path. Keep it for forensics, out of Portal/.
        dest = _quarantine(legacy_db, target_db.parent)
        if dest is None:
            secure_db_files(legacy_db)
            secure_db_files(target_db)
            return "conflict"
        logger.warning("[TASKS-DB] %s held no task rows — quarantined to %s; %s "
                       "stays the live DB", legacy_db, dest, target_db)
        if _sqlite_follows_symlinks(target_db.parent):
            try:
                _install_compat_link(legacy_db, target_db)
            except OSError as exc:
                logger.warning("[TASKS-DB] could not link %s -> %s: %s",
                               legacy_db, target_db, exc)
        secure_db_files(target_db)
        return "already"

    if _task_rows(target_db) == 0:
        dest = _quarantine(target_db, target_db.parent)
        if dest is not None:
            logger.warning("[TASKS-DB] %s held no task rows — quarantined to %s "
                           "so %s can move in", target_db, dest, legacy_db)
            return _move(legacy_db, target_db)

    logger.error(
        "[TASKS-DB] both %s and %s hold data — NOT moving (that would clobber "
        "the newer DB). The service keeps using %s; reconcile the other copy by "
        "hand (`python -m Orchestrator.migrations.tasks_db_location --status`). "
        "Neither is served at /ui; both are now 0600.",
        legacy_db, target_db, open_path_for("conflict", legacy_db, target_db),
    )
    secure_db_files(legacy_db)
    secure_db_files(target_db)
    return "conflict"


def open_path_for(status: str, legacy_db, target_db) -> Path:
    """Which DB a process opens after migrate_legacy_tasks_db returned `status`."""
    legacy_db, target_db = Path(legacy_db), Path(target_db)
    if status == "error":
        return legacy_db
    if status == "conflict" and not legacy_db.is_symlink() and (
            _size(target_db) == 0 or _same(legacy_db, target_db)
            or (_is_sqlite(legacy_db) and not _is_sqlite(target_db))):
        # The target is no DB of its own (orphaned sidecars, a half-done link
        # whose -wal is not the legacy one, or not SQLite at all): the legacy
        # DB is the intact one.
        return legacy_db
    return target_db


def prepare_tasks_db(legacy_db, target_db) -> Tuple[str, Path]:
    """Run the migration and return (status, path this process must open)."""
    status = migrate_legacy_tasks_db(legacy_db, target_db)
    return status, open_path_for(status, legacy_db, target_db)


def describe(path: Path) -> str:
    """One status line for `path` (+ sidecars). Opens only an existing DB."""
    if path.is_symlink():
        head = f"symlink -> {os.readlink(path)}"
        if not path.exists():
            return f"{path}: {head} (dangling)"
    elif not path.exists():
        return f"{path}: absent"
    else:
        head = "file"
    st = path.stat()
    sidecars = [f"{s} {_size(_sidecar(path, s))}B" for s in SIDECAR_SUFFIXES
                if _sidecar(path, s).exists()]
    rows = _task_rows(path)
    return (f"{path}: {head}, {st.st_size}B, mode {stat.S_IMODE(st.st_mode):04o}"
            f"{', ' + ', '.join(sidecars) if sidecars else ''}, "
            f"task rows: {'unreadable' if rows is None else rows}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--status", action="store_true", required=True,
                        help="print both task DB paths (read-only)")
    parser.add_argument("--root", help="BlackBox root (default: resolved like the service)")
    args = parser.parse_args(argv)
    if args.root:
        root = Path(args.root).resolve()
    else:
        from Orchestrator.utils.paths import blackbox_root  # stdlib-only resolver
        root = blackbox_root()
    data_dir = root / "data"
    print(f"data dir: {data_dir}: " + (
        f"mode {stat.S_IMODE(data_dir.stat().st_mode):04o}" if data_dir.is_dir() else "absent"))
    print("target  " + describe(data_dir / "tasks.db"))
    print("legacy  " + describe(root / "Portal" / "tasks.db"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
