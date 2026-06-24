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

from Orchestrator.checkpoint import app
from Orchestrator.scheduler import get_scheduler_manager

logger = logging.getLogger(__name__)


class CronJobCreate(BaseModel):
    name: str
    prompt: str
    schedule: str                          # cron expression
    frequency_hint: Optional[str] = None   # human-readable
    model: Optional[str] = "gemini"
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
    delivery: Optional[str] = None
    delivery_target: Optional[str] = None
    operator: Optional[str] = None
    one_shot: Optional[bool] = None


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
            await manager._execute_job(job_id)
        except Exception:
            logger.exception("Background run-now failed for cron job %s", job_id)

    asyncio.create_task(_run())

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
