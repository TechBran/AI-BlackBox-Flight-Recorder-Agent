# Cron Job Management System Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a complete cron job management system with APScheduler backend, SQLite persistence, Portal UI for full CRUD management, and LLM tools (create/edit/search) available to all model endpoints.

**Architecture:** APScheduler runs inside the Orchestrator process with SQLite job store for persistence across restarts. A new `Orchestrator/scheduler/` module handles all scheduling logic. A new `Orchestrator/routes/cron_routes.py` exposes REST API endpoints for the Portal UI. Three new tools (create_cron_job, edit_cron_job, search_cron_jobs) are added to `blackbox_tools.py` for all AI models. The Portal gets a "Scheduler" button in the generation section that opens a full management panel.

**Tech Stack:** APScheduler 3.x, sqlite3, FastAPI, vanilla JS (ES modules), CSS design tokens

---

## Task 1: Install APScheduler Dependency

**Files:**
- Modify: `Orchestrator/venv/` (pip install)

**Step 1: Install APScheduler**

```bash
/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/pip install APScheduler==3.10.4
```

Expected: Successfully installed APScheduler and dependencies (tzlocal, six)

**Step 2: Verify installation**

```bash
/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Orchestrator/venv/bin/python -c "from apscheduler.schedulers.asyncio import AsyncIOScheduler; print('APScheduler OK')"
```

Expected: `APScheduler OK`

---

## Task 2: Create Scheduler Module - Database & Manager

**Files:**
- Create: `Orchestrator/scheduler/__init__.py`
- Create: `Orchestrator/scheduler/manager.py`

This task creates the core scheduler manager with SQLite persistence for cron jobs. The manager wraps APScheduler and provides CRUD operations.

**Step 1: Create `Orchestrator/scheduler/__init__.py`**

```python
"""
Cron Job Scheduler Module

Provides scheduled task management with APScheduler backend and SQLite persistence.
"""

from .manager import CronJobManager, get_scheduler_manager

__all__ = ["CronJobManager", "get_scheduler_manager"]
```

**Step 2: Create `Orchestrator/scheduler/manager.py`**

The manager needs to:
- Initialize APScheduler with AsyncIOScheduler
- Use SQLite for job metadata (prompt, model, delivery, operator, status, history)
- APScheduler stores its own job data internally; we store our metadata separately
- Provide CRUD: create, get, list, update, delete, pause, resume, run_now
- Track execution history (last_run, result, duration)

Database schema for `Orchestrator/cron_jobs.db`:

```sql
CREATE TABLE IF NOT EXISTS cron_jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    schedule TEXT NOT NULL,           -- cron expression (e.g., "0 7 * * *")
    frequency_hint TEXT,              -- human-readable (e.g., "Daily at 7 AM")
    model TEXT DEFAULT 'gemini',      -- which LLM runs this
    delivery TEXT DEFAULT 'snapshot', -- snapshot|sms|voice_call|notification
    delivery_target TEXT,             -- phone number, etc.
    operator TEXT NOT NULL,
    status TEXT DEFAULT 'active',     -- active|paused|completed
    one_shot INTEGER DEFAULT 0,       -- 1 = run once then auto-delete
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_run_at TEXT,
    last_run_result TEXT,
    last_run_duration_ms INTEGER,
    next_run_at TEXT,
    run_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cron_job_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    run_at TEXT NOT NULL,
    prompt TEXT NOT NULL,
    model TEXT NOT NULL,
    result TEXT,
    delivery_status TEXT,
    duration_ms INTEGER,
    error TEXT,
    FOREIGN KEY (job_id) REFERENCES cron_jobs(id) ON DELETE CASCADE
);
```

**Key implementation details for `manager.py`:**

```python
import sqlite3
import uuid
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger

DB_PATH = Path(__file__).parent.parent / "cron_jobs.db"

# Singleton
_manager_instance = None

def get_scheduler_manager() -> "CronJobManager":
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = CronJobManager()
    return _manager_instance

class CronJobManager:
    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self._init_db()

    def _init_db(self):
        """Create tables if they don't exist."""
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        # Create both tables (schema above)
        conn.commit()
        conn.close()

    def start(self):
        """Start the scheduler and reload persisted jobs."""
        # Load all active jobs from SQLite and register with APScheduler
        jobs = self.list_jobs(status="active")
        for job in jobs:
            self._register_job_with_scheduler(job)
        self.scheduler.start()
        print(f"[CRON] Scheduler started with {len(jobs)} active jobs")

    def shutdown(self):
        """Gracefully shut down the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            print("[CRON] Scheduler shut down")

    def create_job(self, name, prompt, schedule, operator, **kwargs) -> Dict:
        """Create a new cron job. Returns the job dict."""
        job_id = f"cron_{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()
        # Parse schedule to get next_run_at
        # Insert into SQLite
        # Register with APScheduler
        # Return job dict

    def get_job(self, job_id) -> Optional[Dict]:
        """Get a single job by ID."""

    def list_jobs(self, operator=None, status=None) -> List[Dict]:
        """List all jobs, optionally filtered by operator or status."""

    def update_job(self, job_id, **kwargs) -> Optional[Dict]:
        """Update job fields (name, prompt, schedule, model, delivery, etc.)."""
        # Update SQLite record
        # If schedule changed, remove and re-add APScheduler job
        # Return updated job dict

    def delete_job(self, job_id) -> bool:
        """Permanently delete a job."""
        # Remove from APScheduler
        # Delete from SQLite (cascades to history)

    def pause_job(self, job_id) -> Optional[Dict]:
        """Pause a job (keep it but stop executing)."""
        # Pause in APScheduler
        # Update status in SQLite

    def resume_job(self, job_id) -> Optional[Dict]:
        """Resume a paused job."""
        # Resume in APScheduler
        # Update status in SQLite

    async def run_job_now(self, job_id) -> Dict:
        """Manually trigger a job immediately."""
        # Execute the job's prompt against the chosen model
        # Record in history

    def get_job_history(self, job_id, limit=20) -> List[Dict]:
        """Get execution history for a job."""

    def _register_job_with_scheduler(self, job: Dict):
        """Register a job with APScheduler using its cron expression."""
        trigger = CronTrigger.from_crontab(job["schedule"])
        self.scheduler.add_job(
            self._execute_job,
            trigger=trigger,
            id=job["id"],
            args=[job["id"]],
            replace_existing=True
        )

    async def _execute_job(self, job_id: str):
        """Called by APScheduler when a job fires. Runs the prompt through the LLM."""
        # 1. Load job from SQLite
        # 2. Send prompt to chosen model via internal API call
        # 3. Route result through delivery channel
        # 4. Record execution in history
        # 5. Update last_run_at, run_count, etc.
        # 6. If one_shot, delete job after execution
```

**Step 3: Verify module imports**

```bash
cd /home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc && Orchestrator/venv/bin/python -c "from Orchestrator.scheduler import CronJobManager, get_scheduler_manager; print('Scheduler module OK')"
```

---

## Task 3: Create Scheduler Executor - Job Execution & Delivery

**Files:**
- Create: `Orchestrator/scheduler/executor.py`

The executor handles what happens when a cron job fires. It sends the prompt to the chosen LLM and routes the result to the delivery channel.

**Implementation details for `executor.py`:**

```python
import aiohttp
import asyncio
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional

BASE_URL = "http://localhost:9091"

async def execute_cron_job(job: Dict) -> Dict:
    """
    Execute a cron job by sending its prompt to the configured LLM.

    Returns: {"success": bool, "result": str, "duration_ms": int, "error": str|None}
    """
    start_time = time.time()
    prompt = job["prompt"]
    model = job.get("model", "gemini")
    operator = job["operator"]

    try:
        # Send prompt to the chat endpoint
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{BASE_URL}/chat",
                json={
                    "message": prompt,
                    "model": model,
                    "operator": operator,
                    "context": f"[Scheduled Task: {job.get('name', 'Unnamed')}]"
                },
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status == 200:
                    result_text = await resp.text()
                    duration_ms = int((time.time() - start_time) * 1000)

                    # Route delivery
                    delivery_result = await deliver_result(job, result_text)

                    return {
                        "success": True,
                        "result": result_text,
                        "duration_ms": duration_ms,
                        "delivery_status": delivery_result
                    }
                else:
                    error = await resp.text()
                    return {
                        "success": False,
                        "result": "",
                        "duration_ms": int((time.time() - start_time) * 1000),
                        "error": f"HTTP {resp.status}: {error[:200]}"
                    }
    except Exception as e:
        return {
            "success": False,
            "result": "",
            "duration_ms": int((time.time() - start_time) * 1000),
            "error": str(e)
        }

async def deliver_result(job: Dict, result: str) -> str:
    """Route the job result to the configured delivery channel."""
    delivery = job.get("delivery", "snapshot")
    target = job.get("delivery_target", "")
    operator = job["operator"]

    if delivery == "snapshot":
        # Mint a snapshot with the result
        async with aiohttp.ClientSession() as session:
            await session.post(f"{BASE_URL}/chat", json={
                "message": f"[Cron Job Result: {job.get('name', '')}]\n\n{result}",
                "operator": operator
            })
        return "snapshot_minted"

    elif delivery == "sms" and target:
        from Orchestrator.tools import execute_tool
        sms_result = await execute_tool("send_sms", {
            "phone_number": target,
            "message": f"[{job.get('name', 'Scheduled Task')}]\n{result[:1500]}"
        }, operator)
        return "sms_sent" if sms_result.success else f"sms_failed: {sms_result.result}"

    elif delivery == "voice_call" and target:
        from Orchestrator.tools import execute_tool
        call_result = await execute_tool("make_voice_call", {
            "phone_number": target,
            "message": result[:500]
        }, operator)
        return "call_made" if call_result.success else f"call_failed: {call_result.result}"

    elif delivery == "notification":
        # Push to Portal via WebSocket (future - for now log)
        print(f"[CRON NOTIFICATION] {job.get('name', '')}: {result[:200]}")
        return "notification_sent"

    return "no_delivery"
```

---

## Task 4: Wire Scheduler into FastAPI Startup/Shutdown

**Files:**
- Modify: `Orchestrator/startup.py` (~lines 49-100)

**Step 1: Add scheduler start to `startup_check_index()` or add a new startup event**

Add after the existing startup logic in `startup.py`:

```python
# At top of file, add import:
from Orchestrator.scheduler import get_scheduler_manager

# Add new startup event:
@app.on_event("startup")
def startup_scheduler():
    """Start the cron job scheduler."""
    try:
        manager = get_scheduler_manager()
        manager.start()
        print("[STARTUP] Cron scheduler started")
    except Exception as e:
        print(f"[STARTUP] Cron scheduler failed to start: {e}")

# Add shutdown handler - modify existing shutdown_handler or add new event:
@app.on_event("shutdown")
def shutdown_scheduler():
    """Stop the cron job scheduler."""
    try:
        manager = get_scheduler_manager()
        manager.shutdown()
    except Exception:
        pass
```

---

## Task 5: Create Cron API Routes

**Files:**
- Create: `Orchestrator/routes/cron_routes.py`
- Modify: `Orchestrator/app.py` (add import at line ~83)

**Step 1: Create `Orchestrator/routes/cron_routes.py`**

REST API endpoints for the Portal UI:

```python
from fastapi import HTTPException
from pydantic import BaseModel
from typing import Optional, List
from Orchestrator.checkpoint import app
from Orchestrator.scheduler import get_scheduler_manager

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
    one_shot: Optional[bool] = None

# List all jobs (optionally filter by operator)
@app.get("/api/cron/jobs")
async def list_cron_jobs(operator: Optional[str] = None, status: Optional[str] = None):
    manager = get_scheduler_manager()
    jobs = manager.list_jobs(operator=operator, status=status)
    return {"jobs": jobs, "count": len(jobs)}

# Create a new job
@app.post("/api/cron/jobs")
async def create_cron_job(body: CronJobCreate):
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

# Get a single job
@app.get("/api/cron/jobs/{job_id}")
async def get_cron_job(job_id: str):
    manager = get_scheduler_manager()
    job = manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": job}

# Update a job
@app.put("/api/cron/jobs/{job_id}")
async def update_cron_job(job_id: str, body: CronJobUpdate):
    manager = get_scheduler_manager()
    updates = {k: v for k, v in body.dict().items() if v is not None}
    job = manager.update_job(job_id, **updates)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": job}

# Delete a job
@app.delete("/api/cron/jobs/{job_id}")
async def delete_cron_job(job_id: str):
    manager = get_scheduler_manager()
    success = manager.delete_job(job_id)
    if not success:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"status": "deleted", "job_id": job_id}

# Pause a job
@app.post("/api/cron/jobs/{job_id}/pause")
async def pause_cron_job(job_id: str):
    manager = get_scheduler_manager()
    job = manager.pause_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": job}

# Resume a paused job
@app.post("/api/cron/jobs/{job_id}/resume")
async def resume_cron_job(job_id: str):
    manager = get_scheduler_manager()
    job = manager.resume_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job": job}

# Manually trigger a job
@app.post("/api/cron/jobs/{job_id}/run")
async def run_cron_job_now(job_id: str):
    manager = get_scheduler_manager()
    result = await manager.run_job_now(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"result": result}

# Get job execution history
@app.get("/api/cron/jobs/{job_id}/history")
async def get_cron_job_history(job_id: str, limit: int = 20):
    manager = get_scheduler_manager()
    history = manager.get_job_history(job_id, limit=limit)
    return {"history": history, "count": len(history)}
```

**Step 2: Register routes in `Orchestrator/app.py`**

Add after line 83 (`from Orchestrator.routes.twilio_routes import *`):

```python
from Orchestrator.routes.cron_routes import *
```

---

## Task 6: Add Cron Tools to blackbox_tools.py

**Files:**
- Modify: `Orchestrator/tools/blackbox_tools.py`

Add 3 new tools for all AI models: `create_cron_job`, `edit_cron_job`, `search_cron_jobs`.
**NOT delete** - per user requirement, models cannot delete jobs (only the UI can).

**Step 1: Add tool definitions to BLACKBOX_TOOLS_ANTHROPIC** (after `save_contact` tool, ~line 418)

```python
# --- Cron Job Tools ---
{
    "name": "create_cron_job",
    "description": "Create a scheduled task (cron job) that runs a prompt on a schedule. Use this when the user wants to set up recurring reminders, checks, or automated tasks. The job will fire on the specified schedule and can deliver results via SMS, voice call, or snapshot.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "A short, descriptive name for this task (e.g., 'Morning Server Check', 'Bitcoin Price Alert')"
            },
            "prompt": {
                "type": "string",
                "description": "The prompt/instruction that will be sent to the AI when the job fires"
            },
            "schedule": {
                "type": "string",
                "description": "Cron expression for the schedule (e.g., '0 7 * * *' for daily at 7 AM, '*/30 * * * *' for every 30 minutes, '0 17 * * 5' for Fridays at 5 PM)"
            },
            "frequency_hint": {
                "type": "string",
                "description": "Human-readable description of the schedule (e.g., 'Every day at 7 AM')"
            },
            "model": {
                "type": "string",
                "enum": ["gemini", "openai", "claude", "grok"],
                "description": "Which AI model should execute this job (default: gemini)"
            },
            "delivery": {
                "type": "string",
                "enum": ["snapshot", "sms", "voice_call", "notification"],
                "description": "How to deliver the result (default: snapshot)"
            },
            "delivery_target": {
                "type": "string",
                "description": "Phone number for SMS/voice delivery (E.164 format), not needed for snapshot/notification"
            },
            "one_shot": {
                "type": "boolean",
                "description": "If true, run once and auto-delete (for one-time reminders). Default: false"
            }
        },
        "required": ["name", "prompt", "schedule"]
    }
},
{
    "name": "edit_cron_job",
    "description": "Edit an existing scheduled task (cron job). Use this to change the prompt, schedule, delivery method, or other settings of a cron job.",
    "input_schema": {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "The ID of the cron job to edit (e.g., 'cron_abc123def456')"
            },
            "name": {
                "type": "string",
                "description": "New name for the job"
            },
            "prompt": {
                "type": "string",
                "description": "New prompt/instruction for the job"
            },
            "schedule": {
                "type": "string",
                "description": "New cron expression for the schedule"
            },
            "frequency_hint": {
                "type": "string",
                "description": "New human-readable schedule description"
            },
            "model": {
                "type": "string",
                "enum": ["gemini", "openai", "claude", "grok"],
                "description": "Change which AI model executes this job"
            },
            "delivery": {
                "type": "string",
                "enum": ["snapshot", "sms", "voice_call", "notification"],
                "description": "Change how results are delivered"
            },
            "delivery_target": {
                "type": "string",
                "description": "New phone number for SMS/voice delivery"
            },
            "pause": {
                "type": "boolean",
                "description": "Set to true to pause the job, false to resume it"
            }
        },
        "required": ["job_id"]
    }
},
{
    "name": "search_cron_jobs",
    "description": "Search and list scheduled tasks (cron jobs). Use this when the user asks about their scheduled tasks, reminders, or cron jobs.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Optional search query to filter jobs by name or prompt content"
            },
            "status": {
                "type": "string",
                "enum": ["active", "paused", "all"],
                "description": "Filter by status (default: all)"
            }
        }
    }
}
```

**Step 2: Add same 3 tools to BLACKBOX_TOOLS_OPENAI** (after `save_contact`, ~line 753)

Same schema but in OpenAI format: wrap with `"type": "function"` and use `"parameters"` instead of `"input_schema"`.

**Step 3: Add same 3 tools to BLACKBOX_TOOLS_GEMINI** (after `save_contact`, end of list)

Same schema in Gemini format: `"name"`, `"description"`, `"parameters"`.

**Step 4: Add executor methods to BlackBoxToolExecutor class**

Add after `_execute_save_contact` method (~line 1815):

```python
async def _execute_create_cron_job(self, params: Dict[str, Any]) -> ToolResult:
    """Create a new cron job."""
    try:
        from Orchestrator.scheduler import get_scheduler_manager
        manager = get_scheduler_manager()
        job = manager.create_job(
            name=params.get("name", "Unnamed Task"),
            prompt=params.get("prompt", ""),
            schedule=params.get("schedule", ""),
            operator=self.operator,
            frequency_hint=params.get("frequency_hint"),
            model=params.get("model", "gemini"),
            delivery=params.get("delivery", "snapshot"),
            delivery_target=params.get("delivery_target"),
            one_shot=params.get("one_shot", False)
        )
        hint = job.get("frequency_hint") or job["schedule"]
        return ToolResult(
            success=True,
            result=f"Cron job created: '{job['name']}' (ID: {job['id']}). Schedule: {hint}. Delivery: {job['delivery']}.",
            data={"job": job}
        )
    except ValueError as e:
        return ToolResult(False, f"Invalid cron job: {str(e)}")
    except Exception as e:
        return ToolResult(False, f"Create cron job error: {str(e)}")

async def _execute_edit_cron_job(self, params: Dict[str, Any]) -> ToolResult:
    """Edit an existing cron job."""
    try:
        from Orchestrator.scheduler import get_scheduler_manager
        manager = get_scheduler_manager()
        job_id = params.pop("job_id", None)
        if not job_id:
            return ToolResult(False, "job_id is required")

        # Handle pause/resume
        if "pause" in params:
            if params.pop("pause"):
                job = manager.pause_job(job_id)
                if job:
                    return ToolResult(True, f"Cron job '{job['name']}' paused.", data={"job": job})
            else:
                job = manager.resume_job(job_id)
                if job:
                    return ToolResult(True, f"Cron job '{job['name']}' resumed.", data={"job": job})
            return ToolResult(False, "Job not found")

        # Update other fields
        updates = {k: v for k, v in params.items() if v is not None}
        job = manager.update_job(job_id, **updates)
        if not job:
            return ToolResult(False, f"Job not found: {job_id}")
        return ToolResult(
            success=True,
            result=f"Cron job '{job['name']}' updated.",
            data={"job": job}
        )
    except Exception as e:
        return ToolResult(False, f"Edit cron job error: {str(e)}")

async def _execute_search_cron_jobs(self, params: Dict[str, Any]) -> ToolResult:
    """Search/list cron jobs."""
    try:
        from Orchestrator.scheduler import get_scheduler_manager
        manager = get_scheduler_manager()
        status_filter = params.get("status", "all")
        query = params.get("query", "")

        jobs = manager.list_jobs(
            operator=self.operator,
            status=None if status_filter == "all" else status_filter
        )

        # Filter by query if provided
        if query:
            query_lower = query.lower()
            jobs = [j for j in jobs if query_lower in j.get("name", "").lower()
                    or query_lower in j.get("prompt", "").lower()]

        if not jobs:
            return ToolResult(True, "No cron jobs found.", data={"jobs": []})

        # Format results
        lines = [f"Found {len(jobs)} cron job(s):\n"]
        for j in jobs:
            status_emoji = {"active": "🟢", "paused": "🟡"}.get(j["status"], "🔴")
            hint = j.get("frequency_hint") or j["schedule"]
            lines.append(f"{status_emoji} **{j['name']}** (ID: {j['id']})")
            lines.append(f"   Schedule: {hint} | Delivery: {j['delivery']}")
            lines.append(f"   Prompt: {j['prompt'][:100]}{'...' if len(j.get('prompt','')) > 100 else ''}")
            if j.get("last_run_at"):
                lines.append(f"   Last run: {j['last_run_at']}")
            lines.append("")

        return ToolResult(True, "\n".join(lines), data={"jobs": jobs})
    except Exception as e:
        return ToolResult(False, f"Search cron jobs error: {str(e)}")
```

---

## Task 7: Add Tool Handling to Route Files

**Files:**
- Modify: `Orchestrator/routes/chat_routes.py` (multiple tool handling sections)
- Modify: `Orchestrator/routes/realtime_routes.py` (OpenAI WebSocket tool handler)
- Modify: `Orchestrator/routes/gemini_live_routes.py` (Gemini WebSocket tool handler)
- Modify: `Orchestrator/routes/grok_live_routes.py` (Grok WebSocket tool handler)
- Modify: `Orchestrator/routes/twilio_routes.py` (SMS/phone tool handler)

For each route file, add handling for the 3 new tools (`create_cron_job`, `edit_cron_job`, `search_cron_jobs`) in the tool dispatch section, following the existing pattern (e.g., how `search_contacts` and `save_contact` are handled).

**Pattern to follow** (from chat_routes.py):

```python
elif tool_name == "create_cron_job":
    from Orchestrator.tools import BlackBoxToolExecutor
    executor = BlackBoxToolExecutor(operator=operator)
    result = await executor.execute("create_cron_job", tool_input)
    tool_output = result.result
    # ... return tool result in appropriate format for the backend
elif tool_name == "edit_cron_job":
    from Orchestrator.tools import BlackBoxToolExecutor
    executor = BlackBoxToolExecutor(operator=operator)
    result = await executor.execute("edit_cron_job", tool_input)
    tool_output = result.result
elif tool_name == "search_cron_jobs":
    from Orchestrator.tools import BlackBoxToolExecutor
    executor = BlackBoxToolExecutor(operator=operator)
    result = await executor.execute("search_cron_jobs", tool_input)
    tool_output = result.result
```

**IMPORTANT:** chat_routes.py has multiple model handler sections (Anthropic, Gemini, OpenAI streaming, etc.). Each section has its own tool dispatch. The cron tools must be added to ALL of them. Search for all `elif tool_name == "save_contact"` or `elif func_name == "save_contact"` occurrences and add the cron tools after each one.

The WebSocket routes (realtime_routes.py, gemini_live_routes.py, grok_live_routes.py) similarly have tool dispatch sections. Add after the existing tool handlers.

---

## Task 8: Create Portal UI - HTML Structure

**Files:**
- Modify: `Portal/index.html`

**Step 1: Add Scheduler button to generation section** (~line 576, after the Gemini Pro Audio button)

```html
<button id="btnCronManager" class="btn btn-generation">Scheduler</button>
```

**Step 2: Add the Scheduler Management Modal** (after the Gemini Pro TTS modal, before closing body tag)

This is the main UI element - a full management panel:

```html
<!-- Cron Job Scheduler Modal -->
<div id="cronManagerModal" class="modal hide">
    <div class="modal-card cron-manager-card">
        <div class="modal-head">
            <h3>Scheduler</h3>
            <button id="btnCloseCronManager" class="btn">✕</button>
        </div>
        <div class="cron-manager-body">
            <!-- Top Bar: Search + New Job -->
            <div class="cron-top-bar">
                <div class="cron-search-wrap">
                    <input type="text" id="cronSearchInput" placeholder="Search jobs..." class="cron-search-input">
                </div>
                <div class="cron-filter-wrap">
                    <select id="cronStatusFilter" class="cron-filter-select">
                        <option value="all">All</option>
                        <option value="active">Active</option>
                        <option value="paused">Paused</option>
                    </select>
                </div>
                <button id="btnNewCronJob" class="btn btn-confirm cron-new-btn">+ New Job</button>
            </div>

            <!-- Job List -->
            <div id="cronJobList" class="cron-job-list">
                <div class="cron-empty-state">
                    <p>No scheduled jobs yet</p>
                    <p class="cron-empty-hint">Create your first job to automate tasks on a schedule</p>
                </div>
            </div>
        </div>
    </div>
</div>

<!-- Cron Job Create/Edit Modal -->
<div id="cronEditModal" class="modal hide">
    <div class="modal-card cron-edit-card">
        <div class="modal-head">
            <h3 id="cronEditTitle">New Scheduled Job</h3>
            <button id="btnCloseCronEdit" class="btn">✕</button>
        </div>
        <div class="cron-edit-body">
            <input type="hidden" id="cronEditJobId" value="">

            <!-- Job Name -->
            <div class="cron-field">
                <label for="cronJobName">Name</label>
                <input type="text" id="cronJobName" placeholder="Morning Server Check" class="cron-input">
            </div>

            <!-- Prompt -->
            <div class="cron-field">
                <label for="cronJobPrompt">Prompt</label>
                <textarea id="cronJobPrompt" placeholder="What should the AI do when this job runs?" class="cron-textarea" rows="4"></textarea>
            </div>

            <!-- Schedule -->
            <div class="cron-field">
                <label>Schedule</label>
                <div class="cron-schedule-tabs">
                    <button class="cron-tab active" data-tab="simple">Simple</button>
                    <button class="cron-tab" data-tab="advanced">Advanced (Cron)</button>
                </div>
                <div id="cronSimpleSchedule" class="cron-schedule-panel">
                    <div class="cron-simple-row">
                        <select id="cronFrequency" class="cron-select">
                            <option value="hourly">Every Hour</option>
                            <option value="daily" selected>Daily</option>
                            <option value="weekly">Weekly</option>
                            <option value="custom_interval">Custom Interval</option>
                        </select>
                        <div id="cronTimeWrap" class="cron-time-wrap">
                            <label>at</label>
                            <input type="time" id="cronTime" value="07:00" class="cron-time-input">
                        </div>
                        <div id="cronDayWrap" class="cron-day-wrap hide">
                            <label>on</label>
                            <select id="cronDay" class="cron-select">
                                <option value="1">Monday</option>
                                <option value="2">Tuesday</option>
                                <option value="3">Wednesday</option>
                                <option value="4">Thursday</option>
                                <option value="5">Friday</option>
                                <option value="6">Saturday</option>
                                <option value="0">Sunday</option>
                            </select>
                        </div>
                        <div id="cronIntervalWrap" class="cron-interval-wrap hide">
                            <label>every</label>
                            <input type="number" id="cronIntervalValue" value="30" min="1" class="cron-interval-input">
                            <select id="cronIntervalUnit" class="cron-select">
                                <option value="minutes">minutes</option>
                                <option value="hours">hours</option>
                            </select>
                        </div>
                    </div>
                    <div class="cron-preview" id="cronPreviewText">Runs daily at 7:00 AM</div>
                </div>
                <div id="cronAdvancedSchedule" class="cron-schedule-panel hide">
                    <input type="text" id="cronExpression" placeholder="0 7 * * *" class="cron-input cron-expression-input">
                    <div class="cron-preview" id="cronExpressionPreview">Enter a cron expression</div>
                    <div class="cron-help-text">
                        Format: minute hour day-of-month month day-of-week<br>
                        Examples: <code>0 7 * * *</code> (daily 7 AM), <code>*/30 * * * *</code> (every 30 min), <code>0 17 * * 5</code> (Fri 5 PM)
                    </div>
                </div>
            </div>

            <!-- Model -->
            <div class="cron-field">
                <label for="cronModel">Model</label>
                <select id="cronModel" class="cron-select">
                    <option value="gemini">Gemini</option>
                    <option value="openai">OpenAI (GPT)</option>
                    <option value="claude">Claude</option>
                    <option value="grok">Grok</option>
                </select>
            </div>

            <!-- Delivery -->
            <div class="cron-field">
                <label for="cronDelivery">Delivery</label>
                <select id="cronDelivery" class="cron-select">
                    <option value="snapshot">Snapshot (silent log)</option>
                    <option value="sms">SMS Text</option>
                    <option value="voice_call">Voice Call</option>
                    <option value="notification">Notification</option>
                </select>
            </div>

            <!-- Delivery Target (phone number, shown for sms/voice) -->
            <div id="cronDeliveryTargetWrap" class="cron-field hide">
                <label for="cronDeliveryTarget">Phone Number</label>
                <input type="tel" id="cronDeliveryTarget" placeholder="+15555550112" class="cron-input">
            </div>

            <!-- One-shot toggle -->
            <div class="cron-field cron-toggle-field">
                <label for="cronOneShot">Run once and delete</label>
                <label class="cron-toggle">
                    <input type="checkbox" id="cronOneShot">
                    <span class="cron-toggle-slider"></span>
                </label>
            </div>

            <div class="cron-edit-actions">
                <button id="btnCancelCronEdit" class="btn">Cancel</button>
                <button id="btnSaveCronJob" class="btn btn-confirm">Save Job</button>
            </div>
        </div>
    </div>
</div>

<!-- Cron Job History Modal -->
<div id="cronHistoryModal" class="modal hide">
    <div class="modal-card cron-history-card">
        <div class="modal-head">
            <h3 id="cronHistoryTitle">Job History</h3>
            <button id="btnCloseCronHistory" class="btn">✕</button>
        </div>
        <div class="cron-history-body">
            <div id="cronHistoryList" class="cron-history-list">
                <div class="cron-empty-state">No execution history yet</div>
            </div>
        </div>
    </div>
</div>
```

---

## Task 9: Create Portal UI - JavaScript Module

**Files:**
- Create: `Portal/modules/cron-manager.js`
- Modify: `Portal/modules/app-init.js` (add import and init call)

**Step 1: Create `Portal/modules/cron-manager.js`**

This module handles:
- Opening/closing the manager modal
- Loading and rendering the job list from API
- Create/Edit/Delete job flows
- Pause/Resume/Run Now actions
- Job history display
- Schedule builder (simple mode -> cron expression conversion)
- Search and filter

Key functions:
```javascript
import { $, toast } from './core-utils.js';

// State
let allJobs = [];

// =============================================================================
// API Functions
// =============================================================================

async function fetchJobs(status = null) {
    const params = new URLSearchParams();
    if (status && status !== 'all') params.set('status', status);
    const res = await fetch(`/api/cron/jobs?${params}`);
    if (!res.ok) throw new Error('Failed to fetch jobs');
    const data = await res.json();
    return data.jobs;
}

async function createJob(jobData) {
    const res = await fetch('/api/cron/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(jobData)
    });
    if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'Failed to create job');
    }
    return (await res.json()).job;
}

async function updateJob(jobId, updates) {
    const res = await fetch(`/api/cron/jobs/${jobId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates)
    });
    if (!res.ok) throw new Error('Failed to update job');
    return (await res.json()).job;
}

async function deleteJob(jobId) {
    const res = await fetch(`/api/cron/jobs/${jobId}`, { method: 'DELETE' });
    if (!res.ok) throw new Error('Failed to delete job');
}

async function pauseJob(jobId) { /* POST /api/cron/jobs/{id}/pause */ }
async function resumeJob(jobId) { /* POST /api/cron/jobs/{id}/resume */ }
async function runJobNow(jobId) { /* POST /api/cron/jobs/{id}/run */ }
async function fetchHistory(jobId) { /* GET /api/cron/jobs/{id}/history */ }

// =============================================================================
// Rendering
// =============================================================================

function renderJobList(jobs) {
    const container = $('cronJobList');
    if (!container) return;

    if (jobs.length === 0) {
        container.innerHTML = `
            <div class="cron-empty-state">
                <p>No scheduled jobs yet</p>
                <p class="cron-empty-hint">Create your first job to automate tasks</p>
            </div>`;
        return;
    }

    container.innerHTML = jobs.map(job => {
        const statusClass = job.status === 'active' ? 'cron-status-active' : 'cron-status-paused';
        const statusDot = job.status === 'active' ? '🟢' : '🟡';
        const hint = job.frequency_hint || job.schedule;
        const deliveryLabel = { snapshot: 'Snapshot', sms: 'SMS', voice_call: 'Voice', notification: 'Alert' }[job.delivery] || job.delivery;

        return `
        <div class="cron-job-card ${statusClass}" data-job-id="${job.id}">
            <div class="cron-job-header">
                <span class="cron-job-status">${statusDot}</span>
                <span class="cron-job-name">${escapeHtml(job.name)}</span>
                <span class="cron-job-schedule">${escapeHtml(hint)}</span>
            </div>
            <div class="cron-job-prompt">${escapeHtml(job.prompt)}</div>
            <div class="cron-job-meta">
                <span class="cron-job-model">${job.model}</span>
                <span class="cron-job-delivery">${deliveryLabel}</span>
                ${job.last_run_at ? `<span class="cron-job-lastrun">Last: ${formatTime(job.last_run_at)}</span>` : ''}
                ${job.next_run_at ? `<span class="cron-job-nextrun">Next: ${formatTime(job.next_run_at)}</span>` : ''}
            </div>
            <div class="cron-job-actions">
                <button class="cron-action-btn" data-action="run" title="Run Now">▶</button>
                <button class="cron-action-btn" data-action="${job.status === 'active' ? 'pause' : 'resume'}" title="${job.status === 'active' ? 'Pause' : 'Resume'}">${job.status === 'active' ? '⏸' : '▶️'}</button>
                <button class="cron-action-btn" data-action="edit" title="Edit">✏️</button>
                <button class="cron-action-btn" data-action="history" title="History">📋</button>
                <button class="cron-action-btn cron-action-delete" data-action="delete" title="Delete">🗑️</button>
            </div>
        </div>`;
    }).join('');

    // Bind action buttons
    container.querySelectorAll('.cron-action-btn').forEach(btn => {
        btn.addEventListener('click', handleJobAction);
    });
}

// =============================================================================
// Schedule Builder (Simple -> Cron)
// =============================================================================

function simpleToCron() {
    const freq = $('cronFrequency')?.value;
    const time = $('cronTime')?.value || '07:00';
    const [hours, minutes] = time.split(':').map(Number);
    const day = $('cronDay')?.value;

    switch (freq) {
        case 'hourly': return { cron: `0 * * * *`, hint: 'Every hour' };
        case 'daily': return { cron: `${minutes} ${hours} * * *`, hint: `Daily at ${formatTimeStr(hours, minutes)}` };
        case 'weekly': return { cron: `${minutes} ${hours} * * ${day}`, hint: `Weekly on ${dayName(day)} at ${formatTimeStr(hours, minutes)}` };
        case 'custom_interval': {
            const val = parseInt($('cronIntervalValue')?.value || '30');
            const unit = $('cronIntervalUnit')?.value || 'minutes';
            if (unit === 'minutes') return { cron: `*/${val} * * * *`, hint: `Every ${val} minutes` };
            return { cron: `0 */${val} * * *`, hint: `Every ${val} hours` };
        }
    }
}

// =============================================================================
// Init & Export
// =============================================================================

export function initCronManager() {
    // Open manager modal
    const btnOpen = $('btnCronManager');
    if (btnOpen) {
        btnOpen.addEventListener('click', async () => {
            const modal = $('cronManagerModal');
            if (modal) modal.classList.remove('hide');
            await refreshJobList();
        });
    }

    // Close buttons
    ['btnCloseCronManager', 'btnCloseCronEdit', 'btnCloseCronHistory'].forEach(id => {
        const btn = $(id);
        if (btn) btn.addEventListener('click', () => btn.closest('.modal').classList.add('hide'));
    });

    // New Job button
    const btnNew = $('btnNewCronJob');
    if (btnNew) {
        btnNew.addEventListener('click', () => openEditModal(null)); // null = create mode
    }

    // Save Job button
    const btnSave = $('btnSaveCronJob');
    if (btnSave) {
        btnSave.addEventListener('click', handleSaveJob);
    }

    // Cancel edit
    const btnCancel = $('btnCancelCronEdit');
    if (btnCancel) {
        btnCancel.addEventListener('click', () => $('cronEditModal')?.classList.add('hide'));
    }

    // Search input
    const searchInput = $('cronSearchInput');
    if (searchInput) {
        searchInput.addEventListener('input', filterJobs);
    }

    // Status filter
    const statusFilter = $('cronStatusFilter');
    if (statusFilter) {
        statusFilter.addEventListener('change', () => refreshJobList());
    }

    // Schedule tabs
    document.querySelectorAll('.cron-tab').forEach(tab => {
        tab.addEventListener('click', handleScheduleTab);
    });

    // Schedule builder listeners
    setupScheduleBuilder();

    // Delivery type change (show/hide phone number)
    const deliverySelect = $('cronDelivery');
    if (deliverySelect) {
        deliverySelect.addEventListener('change', () => {
            const needsTarget = ['sms', 'voice_call'].includes(deliverySelect.value);
            $('cronDeliveryTargetWrap')?.classList.toggle('hide', !needsTarget);
        });
    }
}
```

**Step 2: Import and initialize in `Portal/modules/app-init.js`**

Add import:
```javascript
import { initCronManager } from './cron-manager.js';
```

Add to init function (after `initGenerationModals()`):
```javascript
initCronManager();
```

---

## Task 10: Create Portal UI - CSS Styling

**Files:**
- Create: `Portal/styles/features/_cron.css`
- Modify: `Portal/styles/main.css` (add import)

**Step 1: Create `Portal/styles/features/_cron.css`**

Use the design token system from `_variables.css`. Follow existing patterns in `Portal/styles/generation/` and `Portal/styles/components/`.

Key CSS classes to style:
- `.cron-manager-card` - Modal card (wider than default, ~min(700px, 95vw))
- `.cron-top-bar` - Flex row with search + filter + new button
- `.cron-job-list` - Scrollable container for job cards
- `.cron-job-card` - Individual job display card
- `.cron-job-header`, `.cron-job-prompt`, `.cron-job-meta`, `.cron-job-actions` - Card sections
- `.cron-edit-card` - Create/Edit modal
- `.cron-field`, `.cron-input`, `.cron-textarea`, `.cron-select` - Form elements
- `.cron-schedule-tabs`, `.cron-tab` - Simple/Advanced toggle
- `.cron-toggle` - Toggle switch for one-shot
- `.cron-history-card`, `.cron-history-list` - History display
- `.cron-status-active`, `.cron-status-paused` - Status-based card coloring
- `.cron-action-btn` - Action button row
- `.cron-empty-state` - Empty state placeholder

Use these design tokens:
- Background: `var(--neutral-50)` to `var(--neutral-200)` for cards
- Text: `var(--neutral-800)` to `var(--neutral-1000)`
- Borders: `1px solid rgba(255, 255, 255, 0.06)` (match existing pattern)
- Radius: `var(--radius-md)` for cards, `var(--radius-sm)` for inputs
- Transitions: `var(--transition-base)` for hover effects
- Shadows: `var(--shadow-sm)` for cards

**Step 2: Add import to `Portal/styles/main.css`**

```css
@import 'features/_cron.css';
```

---

## Task 11: Backend Verification & API Testing

**Step 1: Restart the service**

```bash
sudo systemctl restart blackbox.service
```

**Step 2: Verify scheduler started**

```bash
journalctl -u blackbox.service -n 20 --no-pager | grep -i cron
```

Expected: `[STARTUP] Cron scheduler started`

**Step 3: Test CRUD API**

```bash
# Create a job
curl -s -X POST http://localhost:9091/api/cron/jobs \
  -H "Content-Type: application/json" \
  -d '{"name":"Test Job","prompt":"Say hello","schedule":"0 7 * * *","operator":"Brandon","frequency_hint":"Daily at 7 AM"}' | python3 -m json.tool

# List jobs
curl -s http://localhost:9091/api/cron/jobs | python3 -m json.tool

# Get specific job (use the ID from create response)
curl -s http://localhost:9091/api/cron/jobs/{JOB_ID} | python3 -m json.tool

# Pause job
curl -s -X POST http://localhost:9091/api/cron/jobs/{JOB_ID}/pause | python3 -m json.tool

# Resume job
curl -s -X POST http://localhost:9091/api/cron/jobs/{JOB_ID}/resume | python3 -m json.tool

# Delete job
curl -s -X DELETE http://localhost:9091/api/cron/jobs/{JOB_ID} | python3 -m json.tool
```

---

## Task 12: Frontend Design Skill - Polish UI

**Files:**
- Modify: `Portal/styles/features/_cron.css`
- Potentially modify: HTML structure in `Portal/index.html`

Use the `frontend-design` skill to review and polish the Scheduler UI:
- Job cards should feel like dashboard items (clean, scannable)
- Active/paused status should be visually distinct
- The schedule builder should feel intuitive
- Action buttons should be compact but clear
- History view should be a clean timeline
- Mobile-responsive (the Portal is used on Android too)
- Match the existing Portal aesthetic (dark theme, design tokens)

---

## Task 13: Version Bump & Snapshot

**Step 1: Update version in index.html**

Find the `?v=genui` parameter in `<script>` and `<link>` tags in `Portal/index.html` and bump the version number.

**Step 2: Restart service**

```bash
sudo systemctl restart blackbox.service
```

**Step 3: Create development snapshot**

Use `/snapshot-dev` to document the implementation.

---

## Dependencies Between Tasks

```
Task 1 (Install APScheduler)
  └─→ Task 2 (Scheduler Manager)
       └─→ Task 3 (Executor)
            └─→ Task 4 (Wire into FastAPI)
                 └─→ Task 5 (Cron API Routes)
                      └─→ Task 6 (Add Tools to blackbox_tools.py)
                           └─→ Task 7 (Wire Tools into Route Files)

Task 8 (HTML) ─── can run in parallel with Tasks 2-7
Task 9 (JavaScript) ─── depends on Task 8 (HTML) and Task 5 (API)
Task 10 (CSS) ─── depends on Task 8 (HTML)
Task 12 (Polish) ─── depends on Tasks 8-10

Task 11 (API Testing) ─── depends on Tasks 1-7
Task 13 (Version Bump) ─── depends on all tasks
```

**Parallel opportunities:**
- Tasks 8, 9, 10 (frontend) can be developed in parallel with Tasks 2-7 (backend) since the API contract is defined
- Task 6 (tools) and Task 7 (route wiring) can be worked on after Task 5

---

## Key Decisions & Constraints

1. **Models get 3 tools only**: `create_cron_job`, `edit_cron_job`, `search_cron_jobs` — NO delete tool
2. **UI gets full CRUD**: Create, Read, Update, Delete, Pause, Resume, Run Now, History
3. **APScheduler 3.x** (not 4.x which has breaking API changes)
4. **Raw sqlite3** (matching existing codebase pattern, not SQLAlchemy)
5. **Scheduler runs in-process** with the Orchestrator (no separate service)
6. **Database file**: `Orchestrator/cron_jobs.db` (separate from existing DBs)
7. **UI location**: Generation section in the menu, same as image/video/music gen buttons
8. **Delivery channels**: snapshot (default), sms, voice_call, notification — executor uses existing tools
