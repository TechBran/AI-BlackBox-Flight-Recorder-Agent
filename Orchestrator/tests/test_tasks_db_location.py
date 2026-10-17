"""U10 (audit rank 24: NEW-core-1 + X03-security-backend-07 + P07-html-css-01,
O01-core-runtime-05): the task database moves OUT of the served Portal/ tree.

/ui is a StaticFiles mount of Portal/, and TaskDatabase defaulted to
Portal/tasks.db, so `GET /ui/tasks.db` downloaded every operator's prompts,
results and error strings (a Range request made it resumable). The fix defines
TASKS_DB_PATH once (config.py, under a 0700 data/ dir) and moves a legacy
Portal/tasks.db with its -wal/-shm sidecars there before the first open.

Pins:
  * config — TASKS_DB_PATH lives under blackbox_root()/data, never under Portal/;
    the module-level task_db singleton opens it.
  * migration — fresh box (nothing to move), real WAL content survives the move,
    0600/0700 perms, idempotent re-runs, refuses to clobber an existing DB, a
    0-byte placeholder is not "an existing DB", a failed rename rolls back.
  * every former 'Portal/tasks.db' literal (monitoring x2, admin_routes x2, the
    relocate_reference_images CLI) reads the configured path.

All hermetic: temp dirs only, no network, no live service.
"""
import errno
import logging
import os
import shutil
import sqlite3
import stat
from pathlib import Path

import pytest


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def _make_wal_db(src_dir: Path, rows: int = 3) -> sqlite3.Connection:
    """A real WAL-mode DB whose committed rows live ONLY in -wal (autocheckpoint
    off, connection left open) — the state the live service leaves behind."""
    src_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(src_dir / "tasks.db"))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, prompt TEXT)")
    conn.commit()
    # Push the schema into the main file, then keep the rows WAL-only.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for i in range(rows):
        conn.execute("INSERT INTO tasks VALUES (?, ?)", (f"t{i}", f"prompt {i}"))
    conn.commit()
    assert (src_dir / "tasks.db-wal").stat().st_size > 0
    return conn


def _snapshot_into(src_dir: Path, dest_dir: Path) -> None:
    """Copy db + sidecars as a crash would leave them (writer still open)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name in ("tasks.db", "tasks.db-wal", "tasks.db-shm"):
        if (src_dir / name).exists():
            shutil.copy2(src_dir / name, dest_dir / name)


def _row_count(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def layout(tmp_path):
    root = tmp_path / "blackbox"
    legacy = root / "Portal" / "tasks.db"
    target = root / "data" / "tasks.db"
    legacy.parent.mkdir(parents=True)
    return legacy, target


# ---------------------------------------------------------------------------
# config + the singleton
# ---------------------------------------------------------------------------
def test_tasks_db_path_is_defined_once_outside_the_served_tree():
    from Orchestrator import config
    from Orchestrator.utils.paths import blackbox_root

    root = blackbox_root()
    assert config.TASKS_DB_PATH == root / "data" / "tasks.db"
    assert config.TASKS_DB_PATH.parent == config.BLACKBOX_DATA_DIR
    assert config.LEGACY_TASKS_DB_PATH == root / "Portal" / "tasks.db"
    portal = (root / "Portal").resolve()
    assert portal not in config.TASKS_DB_PATH.resolve().parents


def test_task_db_singleton_opens_the_configured_path():
    from Orchestrator import config, models

    assert Path(models.task_db.db_path) == config.TASKS_DB_PATH
    assert _mode(config.TASKS_DB_PATH) == 0o600
    assert _mode(config.BLACKBOX_DATA_DIR) == 0o700


def test_task_database_creates_its_file_0600(tmp_path):
    from Orchestrator.models import TaskDatabase

    old_umask = os.umask(0)  # the service runs with a permissive umask
    try:
        db = TaskDatabase(str(tmp_path / "fresh" / "tasks.db"))
    finally:
        os.umask(old_umask)
    assert _mode(Path(db.db_path)) == 0o600


# ---------------------------------------------------------------------------
# the migration
# ---------------------------------------------------------------------------
def test_fresh_box_has_nothing_to_move(layout):
    from Orchestrator.migrations.tasks_db_location import migrate_legacy_tasks_db

    legacy, target = layout
    assert migrate_legacy_tasks_db(legacy, target) == "fresh"
    assert target.parent.is_dir() and _mode(target.parent) == 0o700
    assert not target.exists()  # the migration never creates an empty DB
    assert not legacy.exists()


def test_moves_db_with_wal_and_shm_and_keeps_wal_only_rows(layout, tmp_path):
    from Orchestrator.migrations.tasks_db_location import migrate_legacy_tasks_db

    legacy, target = layout
    writer = _make_wal_db(tmp_path / "live", rows=3)
    try:
        _snapshot_into(tmp_path / "live", legacy.parent)
    finally:
        writer.close()
    for p in (legacy, Path(f"{legacy}-wal"), Path(f"{legacy}-shm")):
        os.chmod(p, 0o666)  # what the dev box actually had

    # Control: the main file alone does NOT hold the rows — moving only
    # tasks.db (and orphaning -wal) would lose them.
    lone = tmp_path / "lone"
    lone.mkdir()
    shutil.copy2(legacy, lone / "tasks.db")
    assert _row_count(lone / "tasks.db") == 0

    assert migrate_legacy_tasks_db(legacy, target) == "moved"

    assert not legacy.exists()
    assert not Path(f"{legacy}-wal").exists()
    assert not Path(f"{legacy}-shm").exists()
    assert _row_count(target) == 3
    assert _mode(target) == 0o600
    assert _mode(target.parent) == 0o700


def test_sidecar_perms_are_tightened(layout):
    from Orchestrator.migrations.tasks_db_location import migrate_legacy_tasks_db

    legacy, target = layout
    for suffix in ("", "-wal", "-shm"):
        Path(f"{legacy}{suffix}").write_bytes(b"x" * 64)
        os.chmod(f"{legacy}{suffix}", 0o666)
    target.parent.mkdir(mode=0o777)
    os.chmod(target.parent, 0o777)

    assert migrate_legacy_tasks_db(legacy, target) == "moved"
    for suffix in ("", "-wal", "-shm"):
        assert _mode(Path(f"{target}{suffix}")) == 0o600
    assert _mode(target.parent) == 0o700


def test_rerun_is_idempotent(layout):
    from Orchestrator.migrations.tasks_db_location import migrate_legacy_tasks_db

    legacy, target = layout
    legacy.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    assert migrate_legacy_tasks_db(legacy, target) == "moved"
    before = target.read_bytes()
    assert migrate_legacy_tasks_db(legacy, target) == "already"
    assert migrate_legacy_tasks_db(legacy, target) == "already"
    assert target.read_bytes() == before
    assert not legacy.exists()


def test_refuses_to_clobber_an_existing_db_and_logs_loudly(layout, caplog):
    from Orchestrator.migrations.tasks_db_location import migrate_legacy_tasks_db

    legacy, target = layout
    legacy.write_bytes(b"LEGACY" * 100)
    Path(f"{legacy}-wal").write_bytes(b"LEGACY-WAL")
    os.chmod(legacy, 0o666)
    target.parent.mkdir(mode=0o700)
    target.write_bytes(b"NEWER" * 100)

    with caplog.at_level(logging.WARNING):
        assert migrate_legacy_tasks_db(legacy, target) == "conflict"

    assert target.read_bytes() == b"NEWER" * 100
    assert legacy.read_bytes() == b"LEGACY" * 100
    assert Path(f"{legacy}-wal").read_bytes() == b"LEGACY-WAL"
    assert not Path(f"{target}-wal").exists()
    # The stranded legacy copy is no longer world-writable.
    assert _mode(legacy) == 0o600
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors and str(legacy) in errors[0].getMessage()
    assert str(target) in errors[0].getMessage()


def test_a_zero_byte_placeholder_is_not_an_existing_db(layout):
    """A 0-byte file with no WAL holds no SQLite data (a bare sqlite3.connect +
    failed query leaves exactly this), so it must not strand the real DB."""
    from Orchestrator.migrations.tasks_db_location import migrate_legacy_tasks_db

    legacy, target = layout
    legacy.write_bytes(b"REAL" * 100)
    target.parent.mkdir(mode=0o700)
    target.touch()

    assert migrate_legacy_tasks_db(legacy, target) == "moved"
    assert target.read_bytes() == b"REAL" * 100


def test_a_zero_byte_db_with_wal_content_is_an_existing_db(layout):
    """In WAL mode a brand-new DB can be 0 bytes with every page in -wal."""
    from Orchestrator.migrations.tasks_db_location import migrate_legacy_tasks_db

    legacy, target = layout
    legacy.write_bytes(b"REAL" * 100)
    target.parent.mkdir(mode=0o700)
    target.touch()
    Path(f"{target}-wal").write_bytes(b"PAGES")

    assert migrate_legacy_tasks_db(legacy, target) == "conflict"
    assert legacy.read_bytes() == b"REAL" * 100
    assert Path(f"{target}-wal").read_bytes() == b"PAGES"


def test_target_sidecar_collision_moves_nothing(layout):
    from Orchestrator.migrations.tasks_db_location import migrate_legacy_tasks_db

    legacy, target = layout
    legacy.write_bytes(b"REAL" * 100)
    Path(f"{legacy}-wal").write_bytes(b"LEGACY-WAL")
    target.parent.mkdir(mode=0o700)
    Path(f"{target}-wal").write_bytes(b"STRAY-WAL")

    assert migrate_legacy_tasks_db(legacy, target) == "conflict"
    assert legacy.read_bytes() == b"REAL" * 100
    assert Path(f"{legacy}-wal").read_bytes() == b"LEGACY-WAL"
    assert Path(f"{target}-wal").read_bytes() == b"STRAY-WAL"
    assert not target.exists()


def test_failed_rename_rolls_back_and_leaves_the_legacy_db_usable(layout, monkeypatch):
    """A cross-filesystem layout (EXDEV) must not leave db and -wal split."""
    from Orchestrator.migrations import tasks_db_location as mod

    legacy, target = layout
    legacy.write_bytes(b"REAL" * 100)
    Path(f"{legacy}-wal").write_bytes(b"LEGACY-WAL")

    real_rename = os.rename

    def flaky_rename(src, dst):
        if str(src) == str(legacy):  # sidecars go first; fail on the main file
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst)

    monkeypatch.setattr(mod.os, "rename", flaky_rename)
    assert mod.migrate_legacy_tasks_db(legacy, target) == "error"
    assert legacy.read_bytes() == b"REAL" * 100
    assert Path(f"{legacy}-wal").read_bytes() == b"LEGACY-WAL"
    assert not target.exists()
    assert not Path(f"{target}-wal").exists()


def test_orphaned_legacy_sidecars_are_reported(layout, caplog):
    from Orchestrator.migrations.tasks_db_location import migrate_legacy_tasks_db

    legacy, target = layout
    target.parent.mkdir(mode=0o700)
    target.write_bytes(b"MIGRATED")
    Path(f"{legacy}-wal").write_bytes(b"ORPHAN")

    with caplog.at_level(logging.WARNING):
        assert migrate_legacy_tasks_db(legacy, target) == "already"
    assert any(f"{legacy}-wal" in r.getMessage() for r in caplog.records
               if r.levelno >= logging.WARNING)
    assert Path(f"{legacy}-wal").read_bytes() == b"ORPHAN"  # never deleted


# ---------------------------------------------------------------------------
# every former 'Portal/tasks.db' literal reads the configured path
# ---------------------------------------------------------------------------
def _seed_db(path: Path, rows) -> None:
    from Orchestrator.models import TaskDatabase

    TaskDatabase(str(path))
    conn = sqlite3.connect(str(path))
    conn.executemany(
        "INSERT INTO tasks (task_id, task_type, status, created_at, updated_at, prompt) "
        "VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


_OLD = "2020-01-01T00:00:00Z"


def test_monitoring_reads_the_configured_tasks_db(tmp_path, monkeypatch):
    from Orchestrator import monitoring

    db = tmp_path / "elsewhere" / "tasks.db"
    _seed_db(db, [("stuck-1", "image_generation", "processing", _OLD, _OLD, "p")])
    monkeypatch.setattr(monitoring, "TASKS_DB_PATH", db, raising=False)
    monkeypatch.chdir(tmp_path)  # no Portal/tasks.db relative to the cwd

    usage = monitoring.get_disk_usage()
    assert usage["database_mb"] == pytest.approx(db.stat().st_size / (1024 ** 2))

    stats = monitoring.cleanup_stuck_tasks(max_hours=1)
    assert stats["checked"] == 1 and stats["marked_failed"] == 1


def test_admin_dashboard_and_watchdog_read_the_configured_tasks_db(tmp_path, monkeypatch):
    import Orchestrator.app  # noqa: F401 — registers admin routes on the shared app
    from Orchestrator import monitoring
    from Orchestrator.routes import admin_routes

    db = tmp_path / "elsewhere" / "tasks.db"
    _seed_db(db, [("a", "image_generation", "failed", _OLD, _OLD, "p"),
                  ("b", "image_generation", "completed", _OLD, _OLD, "p")])
    vol = tmp_path / "vol.txt"
    vol.write_text("")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    for mod in (admin_routes, monitoring):
        monkeypatch.setattr(mod, "TASKS_DB_PATH", db, raising=False)
    monkeypatch.setattr(admin_routes, "VOL_PATH", vol)
    monkeypatch.setattr(admin_routes, "MANIFEST", manifest)
    monkeypatch.chdir(tmp_path)

    dash = admin_routes.dashboard()
    assert dash["task_status"]["total"] == 2
    assert dash["task_status"]["failed"] == 1

    assert admin_routes.watchdog()["checks"]["database_accessible"] is True
    monkeypatch.setattr(admin_routes, "TASKS_DB_PATH", tmp_path / "missing.db", raising=False)
    assert admin_routes.watchdog()["checks"]["database_accessible"] is False


def test_relocate_cli_defaults_to_the_configured_path(tmp_path):
    from Orchestrator import config
    from Orchestrator.migrations import relocate_reference_images as migration

    assert migration.default_db_path() == str(config.TASKS_DB_PATH)

    missing = tmp_path / "data" / "tasks.db"
    with pytest.raises(FileNotFoundError):
        migration.run(str(missing), str(tmp_path / "uploads"))
    # A one-shot migration must never CREATE an empty DB (an empty file at the
    # configured path would strand the real one at the legacy location).
    assert not missing.exists()
