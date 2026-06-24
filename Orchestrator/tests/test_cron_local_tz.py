"""
Tests for M1.1 — one box-local timezone everywhere.

The cron scheduler must use the PC's box-local IANA timezone as the single
authoritative scheduling baseline, both for the AsyncIOScheduler instance and
for every CronTrigger it builds. A silent OS-local-vs-UTC mismatch would mean
"0 15 * * *" fires at 15:00 UTC instead of 15:00 local — exactly the class of
clock-untruthfulness M1 exists to eliminate.
"""

import importlib

import pytest

from Orchestrator.scheduler import manager as manager_mod
from Orchestrator.scheduler.manager import CronJobManager, LOCAL_TZ


@pytest.fixture()
def temp_manager(tmp_path, monkeypatch):
    """Construct a CronJobManager against a throwaway sqlite db."""
    db = tmp_path / "cron_jobs_test.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    return CronJobManager()


def test_scheduler_bound_to_local_tz(temp_manager):
    """The APScheduler instance must run on the box-local timezone."""
    assert str(temp_manager.scheduler.timezone) == str(LOCAL_TZ)


def test_build_trigger_uses_local_tz(temp_manager):
    """Every cron trigger the manager builds must carry the box-local tz."""
    trigger = temp_manager._build_trigger("0 15 * * *")
    assert str(trigger.timezone) == str(LOCAL_TZ)


def test_local_tz_is_real_iana_zone():
    """LOCAL_TZ must be a real (DST-aware) zone, not a frozen fixed offset."""
    # A real IANA / pytz / zoneinfo zone stringifies to a zone *name*
    # (e.g. 'America/New_York'), never to a bare '+HH:MM' offset.
    s = str(LOCAL_TZ)
    assert not s.startswith("+") and not s.startswith("-"), (
        f"LOCAL_TZ looks like a fixed offset ({s!r}); a fixed offset is wrong "
        "across DST. Expected a real IANA zone name."
    )
