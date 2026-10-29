"""cron_jobs.db is owner-only (0600), like data/tasks.db (M2 U10).

It holds every job's prompt, delivery target and run history, and was 0644 on
both boxes (SQLite creates it with the process umask). The manager now creates
it 0600, tightens an existing wider file and its -wal/-shm sidecars when it
opens it at boot, and never fails to start over a mode it cannot change. The
file stays where it is (Orchestrator/cron_jobs.db): no migration.
"""
import logging
import os
import sqlite3
import stat

import pytest

from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler.manager import CronJobManager


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "cron_jobs.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", path)
    old = os.umask(0o022)  # the service's umask: what made the file 0644
    yield path
    os.umask(old)


def test_a_new_db_is_created_owner_only(db):
    CronJobManager()
    assert db.exists()
    assert _mode(db) == 0o600


def test_sidecars_written_later_inherit_the_mode(db):
    CronJobManager()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT OR REPLACE INTO cron_meta (key, value) VALUES ('k', 'v')")
        conn.commit()
        wal = db.with_name(db.name + "-wal")
        assert wal.exists()
        assert _mode(wal) == 0o600
    finally:
        conn.close()


def test_an_existing_wider_db_and_its_sidecars_are_tightened_at_open(db):
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    wal = db.with_name(db.name + "-wal")
    shm = db.with_name(db.name + "-shm")
    for p in (db, wal, shm):
        if not p.exists():
            p.write_bytes(b"")
        os.chmod(p, 0o644)
    CronJobManager()
    assert _mode(db) == 0o600
    for p in (wal, shm):
        if p.exists():
            assert _mode(p) == 0o600, p.name


def test_a_mode_that_cannot_be_changed_warns_and_the_scheduler_still_starts(db, monkeypatch, caplog):
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    os.chmod(db, 0o644)

    def refuse(path, mode, *a, **k):
        raise PermissionError(30, "Read-only file system", str(path))

    monkeypatch.setattr(os, "chmod", refuse)
    with caplog.at_level(logging.WARNING):
        mgr = CronJobManager()
    assert mgr.db_path == str(db)
    assert any("0600" in r.getMessage() and "cron_jobs.db" in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]
