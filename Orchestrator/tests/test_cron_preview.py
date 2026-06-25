"""
Tests for M5.2 — POST /api/cron/preview.

The preview endpoint returns the next N fire times for a candidate cron
expression (box-local), so the Portal/editor can show "this runs next at …"
while the user types a schedule, BEFORE the job is saved.

  (a) A valid schedule returns >=1 future fire time (box-local, after now),
      strictly increasing.
  (b) An invalid cron returns 400 (the from_crontab ValueError is caught and
      mapped to a customer-facing 400, NOT a 500).

The route handler is awaited directly (matching the other cron route tests),
so no live server is needed; the manager's _build_trigger does the parsing.
"""

from datetime import datetime

import pytest

from Orchestrator.scheduler import manager as manager_mod


@pytest.fixture()
def fresh_manager(tmp_path, monkeypatch):
    import Orchestrator.app  # noqa: F401 — ensures cron_routes is imported/registered

    db = tmp_path / "cron_jobs_preview_test.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    monkeypatch.setattr(manager_mod, "_manager_instance", None, raising=False)
    from Orchestrator.scheduler import get_scheduler_manager

    return get_scheduler_manager()


@pytest.mark.asyncio
async def test_preview_returns_future_increasing_times(fresh_manager):
    """A valid schedule returns >=1 fire time, all in the future and strictly
    increasing, expressed in box-local time."""
    from Orchestrator.routes import cron_routes

    body = await cron_routes.preview_cron(
        cron_routes.CronPreview(schedule="0 15 * * *")
    )

    runs = body["next_runs"]
    assert isinstance(runs, list)
    assert len(runs) >= 1

    now = datetime.now(manager_mod.LOCAL_TZ)
    parsed = [datetime.fromisoformat(r) for r in runs]

    # All in the future.
    for p in parsed:
        assert p > now, f"fire time {p} should be after now {now}"
    # Strictly increasing.
    assert parsed == sorted(parsed)
    assert len(set(parsed)) == len(parsed), "fire times must be distinct/increasing"
    # Box-local: each parsed value is timezone-aware.
    for p in parsed:
        assert p.tzinfo is not None


@pytest.mark.asyncio
async def test_preview_returns_n_times(fresh_manager):
    """The endpoint previews multiple (N>=3) upcoming fire times for a daily
    schedule."""
    from Orchestrator.routes import cron_routes

    body = await cron_routes.preview_cron(
        cron_routes.CronPreview(schedule="0 9 * * *")
    )
    assert len(body["next_runs"]) >= 3


@pytest.mark.asyncio
async def test_preview_invalid_cron_returns_400(fresh_manager):
    """An invalid cron expression returns 400 (not 500)."""
    from fastapi import HTTPException

    from Orchestrator.routes import cron_routes

    with pytest.raises(HTTPException) as exc_info:
        await cron_routes.preview_cron(
            cron_routes.CronPreview(schedule="not a cron at all")
        )
    assert exc_info.value.status_code == 400
