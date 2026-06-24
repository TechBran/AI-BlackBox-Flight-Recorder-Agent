"""
Tests for M2.9 — fire-and-forget POST /api/cron/jobs/{id}/run.

The route used to AWAIT run_job_now and block for the full job duration
(up to 180/600s). It now returns 202 immediately with a small ack and runs
the job in the BACKGROUND (through _execute_job, so the M2.6 per-job lock
still serialises a manual run against a scheduled fire). The Portal's 5s
history poll observes completion.

  - 202 + {"status":"started", "job_id": ...} returned QUICKLY, before the
    (mocked, slow) underlying execution finishes.
  - 404 still returned for an unknown job id (existence is validated BEFORE
    scheduling the background task).
"""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from Orchestrator.scheduler import manager as manager_mod


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Importing Orchestrator.app registers every route (incl. cron) onto the
    # shared app instance used by the TestClient.
    import Orchestrator.app  # noqa: F401 — registers routes onto the shared app
    from Orchestrator.checkpoint import app

    db = tmp_path / "cron_jobs_run_test.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    # Reset the singleton so the route's get_scheduler_manager() picks up the
    # patched DB_PATH instead of any previously-built instance.
    monkeypatch.setattr(manager_mod, "_manager_instance", None, raising=False)
    return TestClient(app)


def test_run_returns_202_without_blocking(client, monkeypatch):
    """POST /run returns 202 quickly and does NOT block on the full job
    duration — the underlying execution is backgrounded."""
    from Orchestrator.scheduler import get_scheduler_manager

    mgr = get_scheduler_manager()
    job = mgr.create_job(
        name="bg", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    job_id = job["id"]

    finished = {"done": False}

    # A deliberately slow execution: if the route awaited it, the request would
    # take ~3s. Fire-and-forget must return well before that.
    async def slow_execute(job_id_arg):
        await asyncio.sleep(3)
        finished["done"] = True

    monkeypatch.setattr(mgr, "_execute_job", slow_execute)

    t0 = time.monotonic()
    resp = client.post(f"/api/cron/jobs/{job_id}/run")
    elapsed = time.monotonic() - t0

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "started"
    assert body["job_id"] == job_id
    # Returned BEFORE the 3s execution could have completed.
    assert elapsed < 2.5, f"route blocked on execution ({elapsed:.2f}s)"
    assert finished["done"] is False, "execution must not have completed yet"


def test_run_unknown_job_returns_404(client):
    """An unknown job id is rejected with 404 (existence validated before the
    background task is scheduled)."""
    resp = client.post("/api/cron/jobs/does-not-exist/run")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_run_now_task_is_retained_then_cleaned(tmp_path, monkeypatch):
    """B1: the fire-and-forget task MUST be held by a strong reference while it
    runs (or the GC can collect it mid-flight and the job silently never
    completes), and released once done. Verified by awaiting the handler on
    THIS test's own loop — TestClient cancels the background task on request
    teardown, which would hide the retention entirely.
    """
    import Orchestrator.app  # noqa: F401 — registers routes / loads cron_routes
    from Orchestrator.routes import cron_routes
    from Orchestrator.scheduler import get_scheduler_manager

    db = tmp_path / "cron_retain_test.db"
    monkeypatch.setattr(manager_mod, "DB_PATH", db)
    monkeypatch.setattr(manager_mod, "_manager_instance", None, raising=False)
    mgr = get_scheduler_manager()
    job = mgr.create_job(
        name="bg", prompt="hi", schedule="0 15 * * *", operator="system"
    )
    job_id = job["id"]

    cron_routes._RUN_NOW_TASKS.clear()
    started = asyncio.Event()
    release = asyncio.Event()

    async def gated_execute(jid):
        started.set()
        await release.wait()

    monkeypatch.setattr(mgr, "_execute_job", gated_execute)

    resp = await cron_routes.run_cron_job_now(job_id)
    assert resp.status_code == 202

    # In flight → exactly one retained task (not GC-collectable).
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert len(cron_routes._RUN_NOW_TASKS) == 1, "in-flight task not retained"

    # Let it finish; the done-callback discards the ref.
    release.set()
    for _ in range(10):
        if not cron_routes._RUN_NOW_TASKS:
            break
        await asyncio.sleep(0)
    assert len(cron_routes._RUN_NOW_TASKS) == 0, "task ref not cleaned after done"
