"""
Tests for M3.3 — GET /api/cron/health.

The health endpoint reconciles the DB (source of truth) against the live
APScheduler for every ACTIVE job: it reports whether a live trigger exists,
its next fire, the DB's cached next_run_at, and flags DIVERGENCE (DB says
active but the scheduler has no trigger for it — a job that will silently
never fire).

These tests drive the route handler directly on this test's own event loop so
the AsyncIOScheduler can actually start (it binds to the running loop), then
await the handler coroutine — robust against test-ordering (a TestClient in a
sync test has no running loop to start the scheduler on).
"""

import pytest

from Orchestrator.scheduler import manager as manager_mod


@pytest.fixture()
def fresh_manager(tmp_path, monkeypatch):
    import Orchestrator.app  # noqa: F401 — ensures cron_routes is imported/registered

    db = tmp_path / "cron_jobs_health_test.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    monkeypatch.setattr(manager_mod, "_manager_instance", None, raising=False)
    from Orchestrator.scheduler import get_scheduler_manager

    return get_scheduler_manager()


@pytest.mark.asyncio
async def test_registered_job_has_trigger_not_diverged(fresh_manager):
    """A normally-registered active job shows has_trigger=true, diverged=false."""
    from Orchestrator.routes import cron_routes

    mgr = fresh_manager
    await mgr.start()  # starts the scheduler on THIS loop + registers jobs
    try:
        job = mgr.create_job(
            name="healthy", prompt="hi", schedule="0 15 * * *", operator="system"
        )
        job_id = job["id"]

        body = await cron_routes.cron_health()

        assert "jobs" in body and "diverged_count" in body
        entry = next((j for j in body["jobs"] if j["job_id"] == job_id), None)
        assert entry is not None, f"job {job_id} missing from health report"
        assert entry["has_trigger"] is True, "registered job should have a live trigger"
        assert entry["diverged"] is False, "registered job should not be diverged"
        assert entry["next_run"], "registered job should report a next fire time"
        assert body["diverged_count"] == 0
    finally:
        await mgr.shutdown()


@pytest.mark.asyncio
async def test_diverged_job_flagged(fresh_manager):
    """A DB-active job with NO live trigger is flagged diverged."""
    from Orchestrator.routes import cron_routes

    mgr = fresh_manager
    await mgr.start()
    try:
        job = mgr.create_job(
            name="ghost", prompt="hi", schedule="0 16 * * *", operator="system"
        )
        job_id = job["id"]
        # Remove its live trigger while leaving it 'active' in the DB → divergence.
        mgr.scheduler.remove_job(job_id)

        body = await cron_routes.cron_health()

        entry = next((j for j in body["jobs"] if j["job_id"] == job_id), None)
        assert entry is not None
        assert entry["has_trigger"] is False
        assert entry["diverged"] is True
        assert body["diverged_count"] >= 1
    finally:
        await mgr.shutdown()
