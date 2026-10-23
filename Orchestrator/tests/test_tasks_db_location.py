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
    0-byte placeholder is not "an existing DB", a failed link rolls back.
  * the adversarial review (2026-10-17): an OLD-code process still writing
    through Portal/tasks.db while new code imports models keeps working on the
    same DB (compat symlink + held connection); the in-app Rollback's pre-move
    code finds the history through the link and roll-forward is "already"; a
    crash at any step resumes; a stray DB with no task rows is quarantined, not
    a conflict; a conflict with no target DB of its own keeps the legacy open.
  * every former 'Portal/tasks.db' literal (monitoring x2, admin_routes x2, the
    relocate_reference_images CLI) reads the path the task_db singleton uses.

Hermetic: no network, no live service. The migration tests use temp dirs; the
config/singleton tests use THIS checkout's data/ (the root conftest pins
BLACKBOX_ROOT to the checkout, so a live .env can never redirect them).
"""
import errno
import json
import logging
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import textwrap
import time
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


def _is_compat_link(legacy: Path) -> bool:
    return legacy.is_symlink() and os.readlink(legacy) == "../data/tasks.db"


def _no_legacy_sidecars(legacy: Path) -> bool:
    return not any(os.path.lexists(f"{legacy}{s}") for s in ("-wal", "-shm", "-journal"))


def _make_tasks_db(db: Path, ids) -> None:
    """A WAL-mode DB with a minimal `tasks` table."""
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, prompt TEXT)")
    conn.executemany("INSERT INTO tasks VALUES (?, 'p')", [(i,) for i in ids])
    conn.commit()
    conn.close()


def _ids(db: Path):
    conn = sqlite3.connect(str(db))
    try:
        return sorted(r[0] for r in conn.execute("SELECT task_id FROM tasks"))
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

    # The path the import-time singleton opened: the suite's store sandbox
    # (conftest.py) repoints the live singleton for the session afterwards.
    assert Path(models._tasks_db_path) == config.TASKS_DB_PATH
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

    assert _is_compat_link(legacy)  # the legacy name now resolves to the target
    assert _no_legacy_sidecars(legacy)
    assert not target.is_symlink()
    assert _row_count(target) == 3
    assert _row_count(legacy) == 3  # pre-move code reading the old path
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
    assert _is_compat_link(legacy)
    assert not os.path.lexists(legacy.with_name(".tasks.db.migrating"))


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


def test_two_real_dbs_with_rows_conflict_and_the_target_stays_live(layout):
    from Orchestrator.migrations.tasks_db_location import prepare_tasks_db

    legacy, target = layout
    _make_tasks_db(legacy, ["old-1"])
    _make_tasks_db(target, ["new-1"])
    assert prepare_tasks_db(legacy, target) == ("conflict", target)
    assert _ids(legacy) == ["old-1"] and _ids(target) == ["new-1"]
    assert not legacy.is_symlink()


def test_a_conflict_with_a_non_sqlite_target_keeps_the_real_legacy_db(layout):
    from Orchestrator.migrations.tasks_db_location import prepare_tasks_db

    legacy, target = layout
    _make_tasks_db(legacy, ["old-1"])
    target.parent.mkdir(mode=0o700)
    target.write_bytes(b"NOT A DATABASE" * 50)
    assert prepare_tasks_db(legacy, target) == ("conflict", legacy)
    assert _ids(legacy) == ["old-1"]


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
    """A 0-byte main file beside a -wal with content is never assumed empty,
    and never opened (SQLite would delete that -wal)."""
    from Orchestrator.migrations.tasks_db_location import migrate_legacy_tasks_db

    legacy, target = layout
    legacy.write_bytes(b"REAL" * 100)
    target.parent.mkdir(mode=0o700)
    target.touch()
    Path(f"{target}-wal").write_bytes(b"PAGES")

    assert migrate_legacy_tasks_db(legacy, target) == "conflict"
    assert legacy.read_bytes() == b"REAL" * 100
    assert Path(f"{target}-wal").read_bytes() == b"PAGES"
    # The target is no DB of its own: this process keeps opening the legacy.
    from Orchestrator.migrations.tasks_db_location import open_path_for
    assert open_path_for("conflict", legacy, target) == legacy


def test_target_sidecar_collision_moves_nothing(layout):
    from Orchestrator.migrations.tasks_db_location import migrate_legacy_tasks_db

    legacy, target = layout
    legacy.write_bytes(b"REAL" * 100)
    Path(f"{legacy}-wal").write_bytes(b"LEGACY-WAL")
    target.parent.mkdir(mode=0o700)
    Path(f"{target}-wal").write_bytes(b"STRAY-WAL")

    from Orchestrator.migrations.tasks_db_location import prepare_tasks_db

    assert prepare_tasks_db(legacy, target) == ("conflict", legacy)
    assert legacy.read_bytes() == b"REAL" * 100
    assert Path(f"{legacy}-wal").read_bytes() == b"LEGACY-WAL"
    assert Path(f"{target}-wal").read_bytes() == b"STRAY-WAL"
    assert not target.exists()  # never opened: SQLite would replay STRAY-WAL into it


def test_failed_link_rolls_back_and_leaves_the_legacy_db_usable(layout, monkeypatch):
    """A cross-filesystem layout (EXDEV) must not leave db and -wal split."""
    from Orchestrator.migrations import tasks_db_location as mod

    legacy, target = layout
    legacy.write_bytes(b"REAL" * 100)
    Path(f"{legacy}-wal").write_bytes(b"LEGACY-WAL")

    real_link = os.link

    def flaky_link(src, dst):
        if str(src) == str(legacy):  # sidecars go first; fail on the main file
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_link(src, dst)

    monkeypatch.setattr(mod.os, "link", flaky_link)
    assert mod.prepare_tasks_db(legacy, target) == ("error", legacy)
    assert not legacy.is_symlink()
    assert legacy.read_bytes() == b"REAL" * 100
    assert Path(f"{legacy}-wal").read_bytes() == b"LEGACY-WAL"
    assert not target.exists()
    assert not Path(f"{target}-wal").exists()


def test_failed_compat_link_rolls_back_too(layout, monkeypatch):
    from Orchestrator.migrations import tasks_db_location as mod

    legacy, target = layout
    _make_tasks_db(legacy, ["t1"])

    def no_replace(src, dst):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(mod.os, "replace", no_replace)
    assert mod.prepare_tasks_db(legacy, target) == ("error", legacy)
    assert not legacy.is_symlink() and _ids(legacy) == ["t1"]
    assert not os.path.lexists(target)
    assert not os.path.lexists(legacy.with_name(".tasks.db.migrating"))


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
# adversarial review 2026-10-17: old code still using the legacy path
# ---------------------------------------------------------------------------
# A stand-in for the PRE-move service in its own process: TaskDatabase._connect
# opened `Portal/tasks.db` (WAL) on every call, so a move under it used to make
# the next call create an empty file there ("no such table: tasks").
_OLD_SERVICE = textwrap.dedent("""
    import json, os, sqlite3, sys
    legacy, ctl, held_mode = sys.argv[1], sys.argv[2], sys.argv[3] == "held"
    def connect():
        c = sqlite3.connect(legacy, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        return c
    held = connect() if held_mode else None
    ok, errors, n = 0, [], 0
    def write():
        global ok, n
        c = held or connect()
        try:
            c.execute("INSERT INTO tasks VALUES (?, 'p')", (f"w{n}",))
            c.commit()
            ok += 1
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            n += 1
            if held is None:
                c.close()
    while not os.path.exists(ctl + ".stop"):
        write()
        if n == 20:
            open(ctl + ".ready", "w").close()
    for _ in range(20):  # certainly after the move
        write()
    if held is not None:
        held.close()
    print(json.dumps({"ok": ok, "errors": errors[:3]}))
""")


@pytest.mark.parametrize("held", [False, True], ids=["per-call", "held-connection"])
def test_an_old_service_writing_through_the_legacy_path_survives_the_move(layout, tmp_path, held):
    from Orchestrator.migrations.tasks_db_location import prepare_tasks_db

    legacy, target = layout
    _make_tasks_db(legacy, [f"h{i}" for i in range(50)])
    ctl = str(tmp_path / "ctl")
    script = tmp_path / "old_service.py"
    script.write_text(_OLD_SERVICE)
    proc = subprocess.Popen([sys.executable, "-I", str(script), str(legacy), ctl,
                             "held" if held else "per-call"],
                            stdout=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 30
        while not os.path.exists(ctl + ".ready"):
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        assert prepare_tasks_db(legacy, target) == ("moved", target)  # mid-write
        time.sleep(0.2)
    finally:
        Path(ctl + ".stop").touch()
        out, _ = proc.communicate(timeout=60)
    result = json.loads(out)

    assert result["errors"] == []
    ids = _ids(target)
    assert len(ids) == 50 + result["ok"]  # nothing written by the old process is lost
    assert {f"w{i}" for i in range(result["ok"])} <= set(ids)
    conn = sqlite3.connect(str(target))
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    conn.close()
    assert _is_compat_link(legacy) and _no_legacy_sidecars(legacy)
    assert prepare_tasks_db(legacy, target) == ("already", target)


def test_rollback_to_pre_move_code_keeps_history_and_roll_forward_is_clean(layout):
    """The in-app Rollback (POST /update/rollback) restores code that opens
    Portal/tasks.db; it must see every task, and what it writes must still be
    there when the update is re-applied."""
    from Orchestrator.migrations.tasks_db_location import prepare_tasks_db

    legacy, target = layout
    _make_tasks_db(legacy, ["t-old-1"])
    assert prepare_tasks_db(legacy, target)[0] == "moved"

    # Pre-move code after the rollback: TaskDatabase("Portal/tasks.db").
    old = sqlite3.connect(str(legacy))
    old.execute("PRAGMA journal_mode=WAL")
    old.execute("CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, prompt TEXT)")
    assert [r[0] for r in old.execute("SELECT task_id FROM tasks")] == ["t-old-1"]
    old.execute("INSERT INTO tasks VALUES ('t-during-rollback', 'p')")
    old.commit()
    # SQLite resolved the link: its -wal sits beside the real DB, not in Portal/.
    assert os.path.exists(f"{target}-wal") and _no_legacy_sidecars(legacy)
    old.close()

    assert prepare_tasks_db(legacy, target) == ("already", target)
    assert _ids(target) == ["t-during-rollback", "t-old-1"]


def test_this_sqlite_build_derives_sidecar_names_from_a_link_target(tmp_path):
    """The compat link is only safe because SQLite does this; the migration
    probes it per move, and this pins it for the build the box ships."""
    from Orchestrator.migrations.tasks_db_location import _sqlite_follows_symlinks

    assert _sqlite_follows_symlinks(tmp_path) is True
    assert list(tmp_path.iterdir()) == []  # the probe cleans up after itself


def test_without_symlink_support_the_move_still_completes(layout, monkeypatch):
    from Orchestrator.migrations import tasks_db_location as mod

    legacy, target = layout
    _make_tasks_db(legacy, ["t1"])
    monkeypatch.setattr(mod, "_sqlite_follows_symlinks", lambda _d: False)
    assert mod.prepare_tasks_db(legacy, target) == ("moved", target)
    assert not os.path.lexists(legacy) and _no_legacy_sidecars(legacy)
    assert _ids(target) == ["t1"]


def test_a_locked_legacy_db_is_left_in_use(layout, monkeypatch):
    from Orchestrator.migrations import tasks_db_location as mod

    legacy, target = layout
    _make_tasks_db(legacy, ["t1"])

    def locked(_db):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(mod, "_hold_open", locked)
    assert mod.prepare_tasks_db(legacy, target) == ("error", legacy)
    assert not legacy.is_symlink() and not os.path.lexists(target)


def test_a_rollback_journal_legacy_db_is_moved_in_wal_mode(layout):
    """A -journal under a live writer would keep being created beside the old
    name, so the move first puts the DB in WAL mode (as TaskDatabase does)."""
    from Orchestrator.migrations.tasks_db_location import prepare_tasks_db

    legacy, target = layout
    conn = sqlite3.connect(str(legacy))
    conn.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, prompt TEXT)")
    conn.execute("INSERT INTO tasks VALUES ('t1', 'p')")
    conn.commit()
    conn.close()
    assert prepare_tasks_db(legacy, target) == ("moved", target)
    conn = sqlite3.connect(str(target))
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()
    assert _ids(target) == ["t1"] and _is_compat_link(legacy)


# --- a crash at each step of the move resumes on the next run ---------------
def _wal_only_rows_db(legacy: Path, tmp_path: Path, rows: int = 3):
    writer = _make_wal_db(tmp_path / "live", rows=rows)
    try:
        _snapshot_into(tmp_path / "live", legacy.parent)
    finally:
        writer.close()


@pytest.mark.parametrize("step", ["sidecars-linked", "main-linked", "link-swapped"])
def test_a_crash_mid_move_is_finished_by_the_next_run(layout, tmp_path, step):
    from Orchestrator.migrations.tasks_db_location import prepare_tasks_db

    legacy, target = layout
    _wal_only_rows_db(legacy, tmp_path)
    target.parent.mkdir(mode=0o700)
    for s in ("-wal", "-shm"):
        os.link(f"{legacy}{s}", f"{target}{s}")
    if step != "sidecars-linked":
        os.link(legacy, target)
    if step == "link-swapped":
        os.symlink("../data/tasks.db", legacy.with_name(".tasks.db.migrating"))
        os.replace(legacy.with_name(".tasks.db.migrating"), legacy)

    status, path = prepare_tasks_db(legacy, target)
    assert (status, path) == ("already" if step == "link-swapped" else "moved", target)
    assert _row_count(target) == 3  # the WAL-only rows came along
    assert _is_compat_link(legacy) and _no_legacy_sidecars(legacy)
    assert prepare_tasks_db(legacy, target) == ("already", target)


def test_a_stale_swap_link_from_a_crash_is_removed(layout):
    from Orchestrator.migrations.tasks_db_location import migrate_legacy_tasks_db

    legacy, target = layout
    os.symlink("../data/tasks.db", legacy.with_name(".tasks.db.migrating"))
    assert migrate_legacy_tasks_db(legacy, target) == "fresh"
    assert not os.path.lexists(legacy.with_name(".tasks.db.migrating"))


# --- strays with no task rows are not conflicts -------------------------------
def test_an_empty_stray_at_the_legacy_path_is_quarantined(layout, caplog):
    """The reviewer's case: pre-move code re-created an empty Portal/tasks.db
    after the DB moved. Every later boot used to report a false conflict."""
    from Orchestrator.migrations.tasks_db_location import prepare_tasks_db

    legacy, target = layout
    _make_tasks_db(target, ["real-1"])
    conn = sqlite3.connect(str(legacy))  # no schema at all, like the 4096-byte stray
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    assert legacy.stat().st_size > 0

    with caplog.at_level(logging.WARNING):
        assert prepare_tasks_db(legacy, target) == ("already", target)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert _is_compat_link(legacy) and _ids(target) == ["real-1"]
    strays = sorted(p.name for p in target.parent.glob("tasks.db.stray-*"))
    assert len(strays) == 1 and _mode(target.parent / strays[0]) == 0o600
    assert prepare_tasks_db(legacy, target) == ("already", target)


def test_a_schema_only_legacy_with_zero_rows_is_quarantined_too(layout):
    from Orchestrator.migrations.tasks_db_location import prepare_tasks_db

    legacy, target = layout
    _make_tasks_db(target, ["real-1"])
    _make_tasks_db(legacy, [])
    assert prepare_tasks_db(legacy, target) == ("already", target)
    assert _is_compat_link(legacy) and _ids(target) == ["real-1"]


def test_an_empty_target_db_gives_way_to_the_legacy_history(layout):
    from Orchestrator.migrations.tasks_db_location import prepare_tasks_db

    legacy, target = layout
    _make_tasks_db(legacy, ["t1", "t2"])
    _make_tasks_db(target, [])  # e.g. a stray schema-only file
    assert prepare_tasks_db(legacy, target) == ("moved", target)
    assert _ids(target) == ["t1", "t2"] and _is_compat_link(legacy)
    assert len(list(target.parent.glob("tasks.db.stray-*"))) == 1


def test_a_foreign_symlink_at_the_legacy_path_is_left_alone(layout, tmp_path, caplog):
    from Orchestrator.migrations.tasks_db_location import prepare_tasks_db

    legacy, target = layout
    elsewhere = tmp_path / "elsewhere.db"
    _make_tasks_db(elsewhere, ["x"])
    os.symlink(elsewhere, legacy)
    with caplog.at_level(logging.WARNING):
        assert prepare_tasks_db(legacy, target) == ("fresh", target)
    assert os.readlink(legacy) == str(elsewhere)
    assert any("not to" in r.getMessage() for r in caplog.records)


def test_status_cli_reports_both_paths_without_a_sqlite3_binary(layout, capsys):
    from Orchestrator.migrations import tasks_db_location as mod

    legacy, target = layout
    _make_tasks_db(legacy, ["t1", "t2"])
    root = legacy.parent.parent
    assert mod.main(["--status", "--root", str(root)]) == 0
    before = capsys.readouterr().out
    assert "target  " in before and f"{target}: absent" in before
    assert f"{legacy}: file" in before and "task rows: 2" in before
    assert not target.exists()  # --status never creates or moves anything

    mod.prepare_tasks_db(legacy, target)
    mod.main(["--status", "--root", str(root)])
    after = capsys.readouterr().out
    assert "data dir: " in after and "mode 0700" in after
    assert "symlink -> ../data/tasks.db" in after
    assert after.count("task rows: 2") == 2


# ---------------------------------------------------------------------------
# every former 'Portal/tasks.db' literal reads the path the singleton uses
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


def _use_db(monkeypatch, db: Path) -> None:
    """Point the task_db singleton at `db`, as prepare_tasks_db does when it
    has to keep the legacy file in use ("error")."""
    from Orchestrator import models

    monkeypatch.setattr(models.task_db, "db_path", str(db))
    assert models.active_tasks_db_path() == db


def test_monitoring_reads_the_db_the_singleton_uses(tmp_path, monkeypatch):
    from Orchestrator import monitoring

    db = tmp_path / "Portal" / "tasks.db"  # the legacy file, left in use
    _seed_db(db, [("stuck-1", "image_generation", "processing", _OLD, _OLD, "p")])
    _use_db(monkeypatch, db)
    monkeypatch.chdir(tmp_path)

    usage = monitoring.get_disk_usage()
    assert usage["database_mb"] == pytest.approx(db.stat().st_size / (1024 ** 2))

    stats = monitoring.cleanup_stuck_tasks(max_hours=1)
    assert stats["checked"] == 1 and stats["marked_failed"] == 1


def test_admin_dashboard_and_watchdog_read_the_db_the_singleton_uses(tmp_path, monkeypatch):
    import Orchestrator.app  # noqa: F401 — registers admin routes on the shared app
    from Orchestrator.routes import admin_routes

    db = tmp_path / "Portal" / "tasks.db"
    _seed_db(db, [("a", "image_generation", "failed", _OLD, _OLD, "p"),
                  ("b", "image_generation", "completed", _OLD, _OLD, "p")])
    vol = tmp_path / "vol.txt"
    vol.write_text("")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    _use_db(monkeypatch, db)
    monkeypatch.setattr(admin_routes, "VOL_PATH", vol)
    monkeypatch.setattr(admin_routes, "MANIFEST", manifest)
    monkeypatch.chdir(tmp_path)

    dash = admin_routes.dashboard()
    assert dash["task_status"]["total"] == 2
    assert dash["task_status"]["failed"] == 1

    assert admin_routes.watchdog()["checks"]["database_accessible"] is True
    _use_db(monkeypatch, tmp_path / "missing.db")
    assert admin_routes.watchdog()["checks"]["database_accessible"] is False


def test_no_reader_hard_codes_a_task_db_path():
    """The readers ask models.active_tasks_db_path(); none imports TASKS_DB_PATH
    (which is wrong whenever the migration had to keep the legacy file)."""
    repo = Path(__file__).resolve().parents[2]
    for rel in ("Orchestrator/monitoring.py", "Orchestrator/routes/admin_routes.py"):
        src = (repo / rel).read_text()
        assert "TASKS_DB_PATH" not in src and "active_tasks_db_path()" in src, rel


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
