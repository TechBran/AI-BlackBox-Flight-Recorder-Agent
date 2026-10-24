"""
Numeric day-of-week means standard cron (audit O15-scheduler-admin-01).

Standard cron numbers the week 0=Sunday .. 6=Saturday, and 7 is Sunday again.
APScheduler's CronTrigger.from_crontab hands the fifth field to its own
day_of_week field, which numbers the week 0=Monday .. 6=Sunday and refuses 7,
so '0 9 * * 1-5' used to fire Tuesday through Saturday, '0 9 * * 0' fired on
Monday and '0 9 * * 7' could not be saved at all, while the job's label (and
every surface that writes a numeric weekday, such as the Portal's weekly
picker, where Monday is 1) said otherwise. _build_trigger is the one place
every trigger is built, so it translates the field there.

These tests step the built trigger across two weeks, in two box time zones
(one with a DST change inside the window, one with a half-hour offset and no
DST), and check the weekday of every fire.
"""

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler.manager import CronJobManager

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# New York leaves daylight time on 2026-11-01, inside the stepped window;
# Kolkata is UTC+05:30 all year, so a fire's UTC date differs from its local one.
ZONES = ["America/New_York", "Asia/Kolkata"]


@pytest.fixture(params=ZONES)
def zoned_manager(request, tmp_path, monkeypatch):
    tz = ZoneInfo(request.param)
    monkeypatch.setattr(manager_mod, "LOCAL_TZ", tz)
    monkeypatch.setattr(manager_mod, "DB_PATH", tmp_path / "cron_dow.db")
    return CronJobManager(), tz


def _fire_days(mgr, tz, schedule, days=14):
    """The local weekday names of every fire in a two-week window, in order."""
    trigger = mgr._build_trigger(schedule)
    cursor = datetime(2026, 10, 26, 0, 0, tzinfo=tz)  # a Monday
    end = cursor + timedelta(days=days)
    fired = []
    while True:
        fire = trigger.get_next_fire_time(None, cursor)
        if fire is None or fire >= end:
            break
        local = fire.astimezone(tz)
        assert (local.hour, local.minute) == (9, 0), local
        fired.append(DAYS[local.weekday()])
        cursor = fire + timedelta(seconds=1)
    return fired


@pytest.mark.parametrize("dow, expected", [
    ("0", ["Sun"]),
    ("1", ["Mon"]),
    ("2", ["Tue"]),
    ("3", ["Wed"]),
    ("4", ["Thu"]),
    ("5", ["Fri"]),
    ("6", ["Sat"]),
    ("7", ["Sun"]),
])
def test_each_numeric_day_fires_on_that_day(zoned_manager, dow, expected):
    mgr, tz = zoned_manager
    assert _fire_days(mgr, tz, f"0 9 * * {dow}") == expected * 2


@pytest.mark.parametrize("dow, expected", [
    ("1-5", ["Mon", "Tue", "Wed", "Thu", "Fri"]),
    ("0,6", ["Sat", "Sun"]),
    ("6,0", ["Sat", "Sun"]),
    ("6,7", ["Sat", "Sun"]),
    ("5-7", ["Fri", "Sat", "Sun"]),
    ("0-6", DAYS),
    ("1-7", DAYS),
    ("*/2", ["Tue", "Thu", "Sat", "Sun"]),          # 0,2,4,6
    ("1-5/2", ["Mon", "Wed", "Fri"]),
    ("1/2", ["Mon", "Wed", "Fri", "Sun"]),         # 1,3,5,7
    ("0,3-4", ["Wed", "Thu", "Sun"]),
    ("*", DAYS),
    ("?", DAYS),
])
def test_ranges_lists_and_steps_use_standard_numbering(zoned_manager, dow, expected):
    mgr, tz = zoned_manager
    assert _fire_days(mgr, tz, f"0 9 * * {dow}") == expected * 2


@pytest.mark.parametrize("dow, expected", [
    ("mon-fri", ["Mon", "Tue", "Wed", "Thu", "Fri"]),
    ("sun", ["Sun"]),
    ("SAT,sun", ["Sat", "Sun"]),
    ("Mon,3", ["Mon", "Wed"]),
    ("sun-tue", ["Mon", "Tue", "Sun"]),
    # APScheduler numbers sun last, so it read these as written; a stored
    # weekend job must keep firing on the weekend.
    ("sat-sun", ["Sat", "Sun"]),
    ("SAT-SUN", ["Sat", "Sun"]),
    ("fri-sun", ["Fri", "Sat", "Sun"]),
    ("thu-sun", ["Thu", "Fri", "Sat", "Sun"]),
    ("mon-sun", DAYS),
    ("sun-sun", ["Sun"]),
    ("fri-sun/2", ["Fri", "Sun"]),
    ("mon,sat-sun", ["Mon", "Sat", "Sun"]),
])
def test_named_days_keep_their_meaning(zoned_manager, dow, expected):
    mgr, tz = zoned_manager
    assert _fire_days(mgr, tz, f"0 9 * * {dow}") == expected * 2


@pytest.mark.parametrize("dow", ["8", "5-1", "fri-mon", "sun-sat-mon", "5-0", "xyz", "1-",
                                 "*/0", "1,,2", "-1"])
def test_bad_day_of_week_is_refused(zoned_manager, dow):
    mgr, _ = zoned_manager
    with pytest.raises(ValueError):
        mgr._build_trigger(f"0 9 * * {dow}")


def test_wrong_field_count_is_still_refused(zoned_manager):
    mgr, _ = zoned_manager
    with pytest.raises(ValueError):
        mgr._build_trigger("0 9 * *")


def test_label_and_fire_days_agree(zoned_manager):
    """The frequency hint already reads the field as standard cron; the fire
    days now match it."""
    mgr, tz = zoned_manager
    job = mgr.create_job(name="weekday", prompt="hi", schedule="30 9 * * 1-5",
                         operator="system")
    assert job["frequency_hint"].startswith("Weekdays at 09:30")
    trigger = mgr._build_trigger(job["schedule"])
    fire = trigger.get_next_fire_time(None, datetime(2026, 10, 31, 0, 0, tzinfo=tz))  # Sat
    assert DAYS[fire.astimezone(tz).weekday()] == "Mon"


def test_job_create_with_sunday_seven_succeeds(zoned_manager):
    """'7' (Sunday) used to raise ValueError at create time."""
    mgr, tz = zoned_manager
    job = mgr.create_job(name="sunday", prompt="hi", schedule="0 9 * * 7",
                         operator="system")
    nxt = datetime.fromisoformat(job["next_run_at"]).astimezone(tz)
    assert DAYS[nxt.weekday()] == "Sun"


def test_preview_route_steps_standard_days(monkeypatch, app_client):
    """POST /api/cron/preview goes through the same chokepoint."""
    tz = ZoneInfo("America/New_York")
    monkeypatch.setattr(manager_mod, "LOCAL_TZ", tz)
    import Orchestrator.routes.cron_routes as cron_routes
    monkeypatch.setattr(cron_routes, "LOCAL_TZ", tz)
    monkeypatch.setattr(manager_mod, "_manager_instance", None)
    resp = app_client.post("/api/cron/preview", json={"schedule": "0 9 * * 1-5", "count": 10})
    assert resp.status_code == 200, resp.text
    days = {DAYS[datetime.fromisoformat(r).astimezone(tz).weekday()]
            for r in resp.json()["next_runs"]}
    assert days == {"Mon", "Tue", "Wed", "Thu", "Fri"}


def test_startup_reregisters_stored_numeric_job_on_the_right_day(tmp_path, monkeypatch):
    """A job stored before the fix carries a next_run_at computed with the old
    (Monday=0) reading. start() re-registers every active job through
    _build_trigger and persists the corrected next fire."""
    import asyncio

    tz = ZoneInfo("America/New_York")
    monkeypatch.setattr(manager_mod, "LOCAL_TZ", tz)
    monkeypatch.setattr(manager_mod, "DB_PATH", tmp_path / "cron_dow_start.db")
    mgr = CronJobManager()
    job = mgr.create_job(name="monday", prompt="hi", schedule="0 9 * * 1",
                         operator="system")
    # What the old trigger would have stored: the next TUESDAY, in the future.
    now = datetime.now(tz)
    stale = (now + timedelta(days=(1 - now.weekday()) % 7 or 7)).replace(
        hour=9, minute=0, second=0, microsecond=0)
    assert stale.weekday() == 1 and stale > now
    conn = sqlite3.connect(mgr.db_path)
    conn.execute("UPDATE cron_jobs SET next_run_at = ? WHERE id = ?",
                 (stale.isoformat(), job["id"]))
    conn.commit()
    conn.close()

    async def _run():
        restarted = CronJobManager()
        await restarted.start()
        try:
            return restarted.get_job(job["id"])
        finally:
            await restarted.shutdown()

    refreshed = asyncio.run(_run())
    nxt = datetime.fromisoformat(refreshed["next_run_at"]).astimezone(tz)
    assert DAYS[nxt.weekday()] == "Mon"


def _store_raw_job(db_path, job_id, schedule, *, status="active"):
    """Insert a row the way the code before this fix stored it (no trigger built)."""
    now = datetime.now(ZoneInfo("UTC")).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO cron_jobs (id, name, prompt, schedule, model, provider, delivery, "
        "operator, status, one_shot, created_at, updated_at) "
        "VALUES (?, ?, 'hi', ?, '', 'google', 'snapshot', 'system', ?, 0, ?, ?)",
        (job_id, job_id, schedule, status, now, now),
    )
    conn.commit()
    conn.close()


def _start_and_stop(caplog=None):
    import asyncio

    async def _run():
        mgr = CronJobManager()
        await mgr.start()
        try:
            return {j["id"]: (j, mgr.scheduler.get_job(j["id"])) for j in mgr.list_jobs()}
        finally:
            await mgr.shutdown()

    return asyncio.run(_run())


def test_stored_weekend_range_job_stays_active_after_restart(tmp_path, monkeypatch):
    """'0 10 * * sat-sun' worked before the fix; the first restart after it
    must not mark the job 'error'."""
    tz = ZoneInfo("America/New_York")
    monkeypatch.setattr(manager_mod, "LOCAL_TZ", tz)
    monkeypatch.setattr(manager_mod, "DB_PATH", tmp_path / "cron_weekend.db")
    CronJobManager()  # creates the schema
    _store_raw_job(manager_mod.DB_PATH, "wk", "0 10 * * sat-sun")

    jobs = _start_and_stop()

    row, scheduled = jobs["wk"]
    assert row["status"] == "active"
    assert scheduled is not None
    nxt = datetime.fromisoformat(row["next_run_at"]).astimezone(tz)
    assert DAYS[nxt.weekday()] in ("Sat", "Sun")


def test_moved_fire_days_are_logged_once_and_name_the_change(tmp_path, monkeypatch, caplog):
    """The notice says which days a stored numeric job moves from and to, is
    given on the first start after the fix only, and is not given for a job
    whose days do not move."""
    import logging

    tz = ZoneInfo("America/New_York")
    monkeypatch.setattr(manager_mod, "LOCAL_TZ", tz)
    monkeypatch.setattr(manager_mod, "DB_PATH", tmp_path / "cron_notice.db")
    CronJobManager()
    _store_raw_job(manager_mod.DB_PATH, "weekdays", "0 9 * * 1-5")
    _store_raw_job(manager_mod.DB_PATH, "daily", "0 9 * * 0-6")
    _store_raw_job(manager_mod.DB_PATH, "named", "0 9 * * mon-fri")

    with caplog.at_level(logging.INFO, logger=manager_mod.logger.name):
        _start_and_stop()
    notices = [r.getMessage() for r in caplog.records if "day-of-week in" in r.getMessage()]
    assert len(notices) == 1, notices
    assert "weekdays" in notices[0]
    assert "Tue-Sat" in notices[0] and "Mon-Fri" in notices[0]

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=manager_mod.logger.name):
        _start_and_stop()
    assert not [r for r in caplog.records if "day-of-week in" in r.getMessage()], \
        "the notice is for the first start after the fix, not every boot"
