"""
Tests for M1.3 — frequency_hint is derived from the cron expression.

The human-readable frequency_hint is a *label* for the schedule. The cron
expression is the single source of truth: a client-supplied hint must never be
allowed to contradict the actual schedule, and editing the schedule must
regenerate the hint. The hint is box-local, so it carries a "(local)" marker.
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


def _raw_hint(mgr, job_id):
    conn = sqlite3.connect(mgr.db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT frequency_hint FROM cron_jobs WHERE id = ?", (job_id,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_create_derives_hint_from_cron_ignoring_client(temp_manager):
    """create_job derives the hint from the cron, ignoring any client hint."""
    job = temp_manager.create_job(
        name="weekday digest",
        prompt="hi",
        schedule="30 6 * * 1-5",
        operator="system",
        frequency_hint="every blue moon",  # deliberately wrong client hint
    )
    hint = _raw_hint(temp_manager, job["id"])

    assert hint, "no frequency_hint was derived"
    assert "every blue moon" not in hint, "client hint was not ignored"
    # Server-derived from the cron: a weekday 06:30 schedule.
    low = hint.lower()
    assert "6:30" in hint or "06:30" in hint, f"hint missing the time: {hint!r}"
    assert "weekday" in low, f"hint missing weekday sense: {hint!r}"
    assert "(local)" in low, f"hint missing the (local) marker: {hint!r}"


def test_edit_schedule_regenerates_hint(temp_manager):
    """Editing the schedule regenerates the frequency_hint."""
    job = temp_manager.create_job(
        name="daily", prompt="hi", schedule="0 9 * * *", operator="system"
    )
    first = _raw_hint(temp_manager, job["id"])
    assert first

    temp_manager.update_job(job["id"], schedule="0 15 * * *")
    second = _raw_hint(temp_manager, job["id"])

    assert second != first, "hint did not change when the schedule changed"
    assert "15:00" in second or "3:00" in second, (
        f"regenerated hint does not reflect the new 15:00 schedule: {second!r}"
    )


def test_edit_schedule_overrides_client_hint(temp_manager):
    """Even if the client passes a hint on edit, the cron-derived one wins."""
    job = temp_manager.create_job(
        name="daily", prompt="hi", schedule="0 9 * * *", operator="system"
    )
    temp_manager.update_job(
        job["id"], schedule="0 15 * * *", frequency_hint="whenever"
    )
    hint = _raw_hint(temp_manager, job["id"])
    assert "whenever" not in (hint or ""), "client hint won over the cron"


@pytest.mark.parametrize(
    "schedule, expect_substr, forbid_substr",
    [
        # Named day-of-week forms APScheduler accepts must NOT be mislabelled
        # "Daily" (the M1 review regression). They read with the right day sense.
        ("5 4 * * sun", "Sun", "Daily"),
        ("0 9 * * mon-fri", "Weekdays", "Daily"),
        ("0 9 * * sat,sun", "Weekends", "Daily"),
        ("0 9 * * MON-FRI", "Weekdays", "Daily"),  # case-insensitive
        # Numeric forms remain correct.
        ("30 6 * * 1-5", "Weekdays", "Daily"),
        ("0 9 * * 0", "Sun", "Daily"),
        ("0 9 * * 6,7", "Weekends", "Daily"),
        # A genuinely unconstrained dow IS daily.
        ("0 9 * * *", "Daily", None),
    ],
)
def test_named_dow_not_mislabelled_daily(schedule, expect_substr, forbid_substr):
    """Named/numeric day-of-week crons read correctly and never say 'Daily'
    when the day-of-week field is actually constrained."""
    hint = CronJobManager._hint_from_cron(schedule)
    assert expect_substr in hint, f"{schedule!r} -> {hint!r} missing {expect_substr!r}"
    if forbid_substr is not None:
        assert forbid_substr not in hint, (
            f"{schedule!r} -> {hint!r} wrongly contains {forbid_substr!r}"
        )
    assert "(local)" in hint, f"{schedule!r} -> {hint!r} missing (local) marker"
