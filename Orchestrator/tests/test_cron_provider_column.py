"""
Tests for M4.1a — the explicit `provider` column on cron_jobs.

A cron job carries a SPECIFIC model id for ANY provider; the coarse
provider-from-model guess is no longer enough (two providers can share a
naming substring, and Auto/empty model has no substring to guess from). So
the row now stores an explicit `provider` alongside `model`.

Covered here:
  (a) round-trip: a job created with provider="anthropic",
      model="claude-opus-4-8" reports BOTH on read-back.
  (b) update_job persists a changed provider.
  (c) backfill: a legacy row whose provider is NULL reports a provider
      DERIVED from its stored model (via _model_to_provider), so old DBs
      still report a sensible provider.
  (d) migration-safety: an existing DB whose cron_jobs table predates the
      provider column upgrades cleanly — constructing the manager ALTERs the
      column in without error, and create/read work afterwards.

No live server: these are pure SQLite + manager unit tests.
"""

import sqlite3

import pytest

from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler.manager import CronJobManager


@pytest.fixture()
def temp_manager(tmp_path, monkeypatch):
    db = tmp_path / "cron_jobs_test.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    return CronJobManager()


# ---------------------------------------------------------------------------
# (a) round-trip — provider + model both persist and read back
# ---------------------------------------------------------------------------

def test_create_job_round_trips_provider_and_model(temp_manager):
    job = temp_manager.create_job(
        name="opus-job",
        prompt="hi",
        schedule="0 15 * * *",
        operator="system",
        provider="anthropic",
        model="claude-opus-4-8",
    )
    assert job["provider"] == "anthropic"
    assert job["model"] == "claude-opus-4-8"

    # Independent read-back through get_job (fresh connection).
    refreshed = temp_manager.get_job(job["id"])
    assert refreshed["provider"] == "anthropic"
    assert refreshed["model"] == "claude-opus-4-8"


def test_provider_in_columns():
    assert "provider" in manager_mod._CRON_JOBS_COLUMNS


# ---------------------------------------------------------------------------
# (b) update_job persists a changed provider
# ---------------------------------------------------------------------------

def test_update_job_changes_provider(temp_manager):
    job = temp_manager.create_job(
        name="p",
        prompt="hi",
        schedule="0 15 * * *",
        operator="system",
        provider="openai",
        model="gpt-5.1",
    )
    updated = temp_manager.update_job(
        job["id"], provider="anthropic", model="claude-opus-4-8"
    )
    assert updated["provider"] == "anthropic"
    assert updated["model"] == "claude-opus-4-8"
    assert temp_manager.get_job(job["id"])["provider"] == "anthropic"


# ---------------------------------------------------------------------------
# (c) backfill — a legacy NULL-provider row derives provider from model
# ---------------------------------------------------------------------------

def test_legacy_null_provider_backfilled_from_model(temp_manager):
    # Create normally, then forcibly NULL the provider to simulate a legacy row
    # that was written before the column existed.
    job = temp_manager.create_job(
        name="legacy",
        prompt="hi",
        schedule="0 15 * * *",
        operator="system",
        model="claude-opus-4-8",
    )
    conn = sqlite3.connect(temp_manager.db_path)
    try:
        conn.execute(
            "UPDATE cron_jobs SET provider = NULL WHERE id = ?", (job["id"],)
        )
        conn.commit()
    finally:
        conn.close()

    refreshed = temp_manager.get_job(job["id"])
    # provider was NULL on the row, but _job_to_dict backfills it from model.
    assert refreshed["provider"] == "anthropic"


def test_legacy_null_provider_backfill_openai(temp_manager):
    job = temp_manager.create_job(
        name="legacy-openai",
        prompt="hi",
        schedule="0 15 * * *",
        operator="system",
        model="gpt-5.1",
    )
    conn = sqlite3.connect(temp_manager.db_path)
    try:
        conn.execute(
            "UPDATE cron_jobs SET provider = NULL WHERE id = ?", (job["id"],)
        )
        conn.commit()
    finally:
        conn.close()
    assert temp_manager.get_job(job["id"])["provider"] == "openai"


# ---------------------------------------------------------------------------
# (d) migration-safety — a pre-provider DB upgrades cleanly
# ---------------------------------------------------------------------------

def test_existing_db_without_provider_column_upgrades(tmp_path, monkeypatch):
    """Simulate a DB created before the provider column existed: a cron_jobs
    table lacking `provider`. Constructing the manager must ALTER the column in
    (no error), and create/read must work afterwards."""
    db = tmp_path / "legacy_cron.db"

    # Hand-build a cron_jobs table WITHOUT the provider column.
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """
            CREATE TABLE cron_jobs (
                id                  TEXT PRIMARY KEY,
                name                TEXT NOT NULL,
                prompt              TEXT NOT NULL,
                schedule            TEXT NOT NULL,
                frequency_hint      TEXT,
                model               TEXT NOT NULL DEFAULT 'gemini',
                delivery            TEXT NOT NULL DEFAULT 'snapshot',
                delivery_target     TEXT,
                operator            TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'active',
                one_shot            INTEGER NOT NULL DEFAULT 0,
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                last_run_at         TEXT,
                last_run_result     TEXT,
                last_run_duration_ms INTEGER,
                next_run_at         TEXT,
                run_count           INTEGER NOT NULL DEFAULT 0,
                error_count         INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    # Sanity: the column really is absent before migration.
    conn = sqlite3.connect(db)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(cron_jobs)")]
    finally:
        conn.close()
    assert "provider" not in cols

    # Constructing the manager runs _init_db, which must migrate-add the column.
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    mgr = CronJobManager()

    conn = sqlite3.connect(db)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(cron_jobs)")]
    finally:
        conn.close()
    assert "provider" in cols

    # And the upgraded DB is fully usable.
    job = mgr.create_job(
        name="post-migrate",
        prompt="hi",
        schedule="0 15 * * *",
        operator="system",
        provider="openai",
        model="gpt-5.1",
    )
    assert mgr.get_job(job["id"])["provider"] == "openai"


def test_init_db_twice_is_idempotent(tmp_path, monkeypatch):
    """Constructing the manager twice against the same DB must not raise
    (the ALTER is guarded by a PRAGMA table_info check)."""
    db = tmp_path / "idem_cron.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    CronJobManager()
    # Second construction re-runs _init_db; the guarded ALTER must be a no-op.
    CronJobManager()
