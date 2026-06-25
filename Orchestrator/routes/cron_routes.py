#!/usr/bin/env python3
"""
Cron Job Management API Routes

REST endpoints for the Portal UI to manage scheduled tasks.
"""

import asyncio
import logging

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from datetime import datetime, timedelta

from Orchestrator.checkpoint import app
from Orchestrator.scheduler import get_scheduler_manager
from Orchestrator.scheduler.manager import LOCAL_TZ

logger = logging.getLogger(__name__)

# Strong references to in-flight fire-and-forget run-now tasks. The event loop
# only holds a WEAK reference to a bare asyncio.Task, so without this the GC can
# collect a long-running job (it awaits a 180-600s /chat call) mid-flight and it
# would silently never complete. Each task self-removes on done.
_RUN_NOW_TASKS: "set[asyncio.Task]" = set()


class CronJobCreate(BaseModel):
    name: str
    prompt: str
    schedule: str                          # cron expression
    frequency_hint: Optional[str] = None   # human-readable
    model: Optional[str] = "gemini"
    provider: Optional[str] = None         # canonical catalog key (M4); '' model = Auto
    delivery: Optional[str] = "snapshot"
    delivery_target: Optional[str] = None
    operator: str
    one_shot: Optional[bool] = False


class CronJobUpdate(BaseModel):
    name: Optional[str] = None
    prompt: Optional[str] = None
    schedule: Optional[str] = None
    frequency_hint: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None         # canonical catalog key (M4)
    delivery: Optional[str] = None
    delivery_target: Optional[str] = None
    operator: Optional[str] = None
    one_shot: Optional[bool] = None


class CronPreview(BaseModel):
    schedule: str                          # candidate cron expression
    count: int = 3                         # how many upcoming fires to preview


@app.get("/api/cron/contacts")
async def list_cron_contacts(operator: str):
    """List contacts with phone numbers for cron delivery target selection."""
    from Orchestrator.contacts import load_contacts, ensure_operator_book, save_contacts
    data = load_contacts()
    if ensure_operator_book(data, operator):
        save_contacts(data)
    book = data.get(operator, {})
    contacts = [
        {"name": c.get("name", ""), "phone": c.get("phone", ""), "relationship": c.get("relationship", "")}
        for c in book.values() if c.get("phone")
    ]
    contacts.sort(key=lambda c: c["name"].lower())
    return {"contacts": contacts}


@app.get("/api/cron/health")
async def cron_health():
    """Reconcile the DB (source of truth) against the live APScheduler (M3.3).

    For every ACTIVE job, report whether a live trigger exists and its next
    fire, alongside the DB's cached next_run_at, and flag DIVERGENCE — a job
    the DB calls 'active' but for which the scheduler has no trigger (so it
    would silently never fire). diverged_count is the headline number an
    operator/monitor watches.
    """
    manager = get_scheduler_manager()
    jobs = manager.list_jobs(status="active")

    entries = []
    diverged_count = 0
    for job in jobs:
        job_id = job["id"]
        scheduled = None
        try:
            scheduled = manager.scheduler.get_job(job_id)
        except Exception:  # scheduler not running / lookup failure → no trigger
            scheduled = None

        has_trigger = scheduled is not None
        next_run_time = getattr(scheduled, "next_run_time", None) if scheduled else None
        next_run = next_run_time.isoformat() if next_run_time else None
        diverged = not has_trigger
        if diverged:
            diverged_count += 1

        entries.append({
            "job_id": job_id,
            "name": job.get("name"),
            "has_trigger": has_trigger,
            "next_run": next_run,
            "db_next_run": job.get("next_run_at"),
            "diverged": diverged,
        })

    return {"jobs": entries, "diverged_count": diverged_count}


@app.post("/api/cron/preview")
async def preview_cron(body: CronPreview):
    """Preview the next N fire times for a candidate cron expression (M5.2).

    Lets the Portal/editor show "runs next at …" WHILE the user types a
    schedule, before any job is saved. Times are box-local (the same
    authoritative wall clock every job uses), computed by stepping the
    manager's single trigger chokepoint (_build_trigger) forward.

    An invalid cron is the EXPECTED bad input here (the user is mid-edit),
    so a from_crontab ValueError maps to a customer-facing 400 — never a 500.
    """
    manager = get_scheduler_manager()
    try:
        trigger = manager._build_trigger(body.schedule)
    except (ValueError, KeyError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid cron expression '{body.schedule}': {e}",
        )

    # Step the trigger forward in box-local time: ask for the next fire at
    # or after a moving cursor, then push the cursor to JUST PAST that fire
    # so the following call yields the one after it. (Re-feeding the fire as
    # APScheduler's previous_fire_time while holding 'now' fixed would keep
    # returning the same fire — the cursor has to advance.) Clamp the count
    # to a sane ceiling so a hostile/huge value can't spin the loop.
    count = max(1, min(body.count, 25))
    cursor = datetime.now(LOCAL_TZ)
    next_runs = []
    for _ in range(count):
        fire = trigger.get_next_fire_time(None, cursor)
        if fire is None:
            break  # a finite schedule (e.g. a past one-off) can run dry
        next_runs.append(fire.isoformat())
        cursor = fire + timedelta(microseconds=1)

    return {"next_runs": next_runs}


@app.get("/api/cron/jobs")
async def list_cron_jobs(operator: Optional[str] = None, status: Optional[str] = None):
    """List all cron jobs, optionally filtered by operator and/or status."""
    manager = get_scheduler_manager()
    jobs = manager.list_jobs(operator=operator, status=status)
    return {"jobs": jobs, "count": len(jobs)}


@app.post("/api/cron/jobs")
async def create_cron_job(body: CronJobCreate):
    """Create a new scheduled cron job."""
    manager = get_scheduler_manager()
    try:
        job = manager.create_job(
            name=body.name,
            prompt=body.prompt,
            schedule=body.schedule,
            operator=body.operator,
            frequency_hint=body.frequency_hint,
            model=body.model,
            provider=body.provider,
            delivery=body.delivery,
            delivery_target=body.delivery_target,
            one_shot=body.one_shot
        )
        return {"job": job}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/cron/jobs/{job_id}")
async def get_cron_job(job_id: str):
    """Get a single cron job by ID."""
    manager = get_scheduler_manager()
    job = manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": job}


@app.put("/api/cron/jobs/{job_id}")
async def update_cron_job(job_id: str, body: CronJobUpdate):
    """Update an existing cron job."""
    manager = get_scheduler_manager()
    updates = {k: v for k, v in body.dict().items() if v is not None}
    try:
        job = manager.update_job(job_id, **updates)
    except ValueError as e:
        # Surface central validation failures (M2.1) as a 400, not a 500.
        raise HTTPException(status_code=400, detail=str(e))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": job}


@app.delete("/api/cron/jobs/{job_id}")
async def delete_cron_job(job_id: str):
    """Delete a cron job (UI only, not available to AI models)."""
    manager = get_scheduler_manager()
    success = manager.delete_job(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"status": "deleted", "job_id": job_id}


@app.post("/api/cron/jobs/{job_id}/pause")
async def pause_cron_job(job_id: str):
    """Pause a running cron job."""
    manager = get_scheduler_manager()
    job = manager.pause_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": job}


@app.post("/api/cron/jobs/{job_id}/resume")
async def resume_cron_job(job_id: str):
    """Resume a paused cron job."""
    manager = get_scheduler_manager()
    job = manager.resume_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": job}


@app.post("/api/cron/jobs/{job_id}/run")
async def run_cron_job_now(job_id: str):
    """Manually trigger a cron job to run immediately (fire-and-forget, M2.9).

    Returns 202 right away with a small ack and runs the job in the
    background instead of blocking the request for the full job duration
    (which could be 180-600s). The Portal's 5s history poll observes
    completion. The background run goes through _execute_job, so the M2.6
    per-job lock prevents a manual run from colliding with a scheduled fire
    (a manual run that lands while one is already in flight simply records a
    "skipped" history note).
    """
    manager = get_scheduler_manager()
    # Validate the job exists BEFORE scheduling the background task, so an
    # unknown id still returns 404 (not a fire-and-forget 202 for nothing).
    job = manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    async def _run() -> None:
        try:
            await manager.run_job_now(job_id)
        except Exception:
            logger.exception("Background run-now failed for cron job %s", job_id)

    # Retain a strong reference until the task completes (see _RUN_NOW_TASKS).
    task = asyncio.create_task(_run())
    _RUN_NOW_TASKS.add(task)
    task.add_done_callback(_RUN_NOW_TASKS.discard)

    return JSONResponse(
        status_code=202,
        content={"status": "started", "job_id": job_id},
    )


@app.get("/api/cron/jobs/{job_id}/history")
async def get_cron_job_history(job_id: str, limit: int = 20):
    """Get execution history for a cron job."""
    manager = get_scheduler_manager()
    history = manager.get_job_history(job_id, limit=limit)
    return {"history": history, "count": len(history)}
