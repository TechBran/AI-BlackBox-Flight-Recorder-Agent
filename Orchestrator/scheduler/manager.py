"""
Orchestrator/scheduler/manager.py - Cron Job Scheduler Manager

Core scheduling engine for the AI BlackBox Flight Recorder.
Uses APScheduler with CronTrigger for cron-expression-based scheduling
and SQLite for persistent job storage.

Jobs are persisted across restarts: on start(), all active jobs are
reloaded from SQLite and re-registered with APScheduler.
"""

import asyncio
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Box-local timezone — the single authoritative scheduling baseline (M1.1).
#
# The PC's local wall clock is the truth for every cron job: there is NO
# per-job timezone (box-local for ALL jobs is a locked decision). We resolve a
# real, DST-aware IANA zone so that "0 15 * * *" means 15:00 *local*, and keeps
# meaning 15:00 local across daylight-saving transitions.
#
# Resolution order (each step yields a real IANA zone where possible; a bare
# fixed-offset tzinfo is the last resort because it is WRONG across DST):
#   1. tzlocal.get_localzone()                         (preferred)
#   2. ZoneInfo from the /etc/localtime symlink target
#   3. ZoneInfo from /etc/timezone
#   4. datetime.now().astimezone().tzinfo              (fixed offset, last resort)
# ---------------------------------------------------------------------------


def _resolve_local_tz():
    """Resolve a DST-aware box-local timezone with graceful fallbacks."""
    # 1. Preferred: tzlocal gives a real IANA zone.
    try:
        from tzlocal import get_localzone

        tz = get_localzone()
        if tz is not None:
            return tz
    except Exception:  # pragma: no cover - import/availability dependent
        logger.debug("tzlocal unavailable; falling back to OS tz detection", exc_info=True)

    # 2. Derive the IANA name from the /etc/localtime symlink target.
    try:
        link = os.path.realpath("/etc/localtime")
        marker = "/zoneinfo/"
        if marker in link:
            zone_name = link.split(marker, 1)[1]
            return ZoneInfo(zone_name)
    except Exception:  # pragma: no cover - platform dependent
        logger.debug("Could not derive tz from /etc/localtime symlink", exc_info=True)

    # 3. /etc/timezone (Debian/Ubuntu) holds the IANA name directly.
    try:
        with open("/etc/timezone", "r", encoding="utf-8") as fh:
            zone_name = fh.read().strip()
        if zone_name:
            return ZoneInfo(zone_name)
    except Exception:  # pragma: no cover - platform dependent
        logger.debug("Could not read /etc/timezone", exc_info=True)

    # 4. Last resort: a fixed-offset tzinfo (NOT DST-aware).
    logger.warning(
        "Falling back to a fixed-offset local timezone; this is not DST-aware. "
        "Install tzlocal or ensure /etc/localtime is a valid zoneinfo symlink."
    )
    return datetime.now().astimezone().tzinfo


LOCAL_TZ = _resolve_local_tz()

# ---------------------------------------------------------------------------
# Database path - lives alongside other Orchestrator databases
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).resolve().parent.parent / "cron_jobs.db"

# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_manager_instance: Optional["CronJobManager"] = None


def get_scheduler_manager() -> "CronJobManager":
    """Return the singleton CronJobManager, creating it on first call."""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = CronJobManager()
    return _manager_instance


# ---------------------------------------------------------------------------
# Column order for cron_jobs table (used by _job_to_dict)
# ---------------------------------------------------------------------------
_CRON_JOBS_COLUMNS = [
    "id",
    "name",
    "prompt",
    "schedule",
    "frequency_hint",
    "model",
    "delivery",
    "delivery_target",
    "operator",
    "status",
    "one_shot",
    "created_at",
    "updated_at",
    "last_run_at",
    "last_run_result",
    "last_run_duration_ms",
    "next_run_at",
    "run_count",
    "error_count",
]

_HISTORY_COLUMNS = [
    "id",
    "job_id",
    "run_at",
    "prompt",
    "model",
    "result",
    "delivery_status",
    "duration_ms",
    "error",
]


class CronJobManager:
    """
    Manages cron-scheduled jobs backed by SQLite persistence and APScheduler.

    Lifecycle:
        manager = get_scheduler_manager()
        await manager.start()    # loads jobs, starts scheduler
        ...
        await manager.shutdown() # graceful stop
    """

    def __init__(self) -> None:
        self.db_path = str(DB_PATH)
        self.scheduler = AsyncIOScheduler(timezone=LOCAL_TZ)
        self._init_db()

    # ------------------------------------------------------------------
    # Trigger construction
    # ------------------------------------------------------------------

    def _build_trigger(self, schedule: str) -> CronTrigger:
        """
        Build a CronTrigger bound to the box-local timezone.

        Single chokepoint for cron-expression parsing so that every trigger
        the manager constructs interprets the schedule against the same
        authoritative box-local wall clock (M1.1).
        """
        return CronTrigger.from_crontab(schedule, timezone=LOCAL_TZ)

    # ------------------------------------------------------------------
    # Database initialisation
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Create tables if they do not exist."""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS cron_jobs (
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
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS cron_job_history (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id          TEXT NOT NULL,
                    run_at          TEXT NOT NULL,
                    prompt          TEXT,
                    model           TEXT,
                    result          TEXT,
                    delivery_status TEXT,
                    duration_ms     INTEGER,
                    error           TEXT,
                    FOREIGN KEY (job_id) REFERENCES cron_jobs(id)
                        ON DELETE CASCADE
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_cron_job_history_job_id
                    ON cron_job_history(job_id)
            """)
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load persisted active jobs into APScheduler and start the scheduler."""
        # Capture the main event loop so _execute_job_sync_wrapper can
        # schedule coroutines from APScheduler's thread-pool threads.
        self._loop = asyncio.get_running_loop()

        # Start the scheduler BEFORE registering jobs so that each add_job
        # computes a live next_run_time, which _register_job_with_scheduler
        # then persists — recomputing any next_run_at frozen by a prior
        # process against the current box-local clock (M1.2).
        self.scheduler.start()

        jobs = self.list_jobs(status="active")
        loaded = 0
        for job in jobs:
            try:
                self._register_job_with_scheduler(job)
                loaded += 1
            except Exception:
                logger.exception("Failed to register job %s on startup", job["id"])

        logger.info(
            "CronJobManager started – %d active job(s) loaded from %d total",
            loaded,
            len(jobs),
        )

    async def shutdown(self) -> None:
        """Gracefully shut down the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)
            logger.info("CronJobManager shut down gracefully")

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    def create_job(
        self,
        name: str,
        prompt: str,
        schedule: str,
        operator: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Create a new cron job.

        Args:
            name: Human-readable job name.
            prompt: The prompt/instruction to execute on each run.
            schedule: Cron expression (5-field: min hour dom month dow).
            operator: Owning operator name.
            **kwargs: Optional overrides for model, delivery, delivery_target,
                      frequency_hint, one_shot.

        Returns:
            The newly created job as a dict.

        Raises:
            ValueError: If the cron expression is invalid.
        """
        # Validate cron expression early
        try:
            trigger = self._build_trigger(schedule)
        except (ValueError, KeyError) as exc:
            raise ValueError(f"Invalid cron expression '{schedule}': {exc}") from exc

        job_id = "cron_" + uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()

        # Compute next fire time
        next_fire = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
        next_run_at = next_fire.isoformat() if next_fire else None

        model = kwargs.get("model", "gemini")
        delivery = kwargs.get("delivery", "snapshot")
        delivery_target = kwargs.get("delivery_target")
        # The cron is the single source of truth for the label: derive the
        # frequency_hint from the schedule and ignore any client-supplied hint
        # so the label can never contradict the actual schedule (M1.3).
        frequency_hint = self._hint_from_cron(schedule)
        one_shot = 1 if kwargs.get("one_shot") else 0

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO cron_jobs (
                    id, name, prompt, schedule, frequency_hint,
                    model, delivery, delivery_target, operator,
                    status, one_shot, created_at, updated_at, next_run_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    name,
                    prompt,
                    schedule,
                    frequency_hint,
                    model,
                    delivery,
                    delivery_target,
                    operator,
                    one_shot,
                    now,
                    now,
                    next_run_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        job = self.get_job(job_id)
        assert job is not None, "Job was just inserted but could not be read back"

        # Register with live scheduler (if running)
        try:
            self._register_job_with_scheduler(job)
        except Exception:
            logger.exception("Created job %s but failed to register with scheduler", job_id)

        logger.info("Created cron job %s (%s) schedule=%s operator=%s", job_id, name, schedule, operator)
        return job

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Return a single job dict by ID, or None if not found."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM cron_jobs WHERE id = ?", (job_id,))
            row = cursor.fetchone()
            return self._job_to_dict(row) if row else None
        finally:
            conn.close()

    def list_jobs(
        self,
        operator: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        List jobs, optionally filtered by operator and/or status.

        Returns:
            List of job dicts ordered by created_at descending.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            clauses: List[str] = []
            params: List[Any] = []

            if operator is not None:
                clauses.append("operator = ?")
                params.append(operator)
            if status is not None:
                clauses.append("status = ?")
                params.append(status)

            where = ""
            if clauses:
                where = "WHERE " + " AND ".join(clauses)

            cursor.execute(
                f"SELECT * FROM cron_jobs {where} ORDER BY created_at DESC",
                params,
            )
            return [self._job_to_dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def update_job(self, job_id: str, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """
        Update specified fields on a job.

        Supported fields: name, prompt, schedule, frequency_hint, model,
        delivery, delivery_target, operator, status, one_shot.

        If the schedule is changed the job is re-registered with APScheduler
        and next_run_at is recalculated.

        Returns:
            Updated job dict, or None if job_id not found.
        """
        allowed_fields = {
            "name",
            "prompt",
            "schedule",
            "frequency_hint",
            "model",
            "delivery",
            "delivery_target",
            "operator",
            "status",
            "one_shot",
        }

        updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not updates:
            return self.get_job(job_id)

        # Validate new schedule if provided
        schedule_changed = False
        if "schedule" in updates:
            try:
                trigger = self._build_trigger(updates["schedule"])
            except (ValueError, KeyError) as exc:
                raise ValueError(
                    f"Invalid cron expression '{updates['schedule']}': {exc}"
                ) from exc
            next_fire = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
            updates["next_run_at"] = next_fire.isoformat() if next_fire else None
            # Regenerate the label from the new cron — the cron is authoritative,
            # so any client-supplied frequency_hint is overridden (M1.3).
            updates["frequency_hint"] = self._hint_from_cron(updates["schedule"])
            schedule_changed = True

        # Normalise one_shot to integer
        if "one_shot" in updates:
            updates["one_shot"] = 1 if updates["one_shot"] else 0

        updates["updated_at"] = datetime.now(timezone.utc).isoformat()

        set_clause = ", ".join(f"{col} = ?" for col in updates)
        params = list(updates.values()) + [job_id]

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE cron_jobs SET {set_clause} WHERE id = ?",
                params,
            )
            if cursor.rowcount == 0:
                return None
            conn.commit()
        finally:
            conn.close()

        job = self.get_job(job_id)

        # Re-register with scheduler if schedule or status changed
        if job and (schedule_changed or "status" in kwargs):
            try:
                # Remove old trigger
                existing = self.scheduler.get_job(job_id)
                if existing:
                    self.scheduler.remove_job(job_id)

                if job["status"] == "active":
                    self._register_job_with_scheduler(job)
            except Exception:
                logger.exception("Failed to re-register job %s after update", job_id)

        logger.info("Updated cron job %s fields=%s", job_id, list(kwargs.keys()))
        return job

    def delete_job(self, job_id: str) -> bool:
        """
        Delete a job from both APScheduler and SQLite.

        Returns:
            True if a job was deleted, False if not found.
        """
        # Remove from scheduler first
        try:
            existing = self.scheduler.get_job(job_id)
            if existing:
                self.scheduler.remove_job(job_id)
        except Exception:
            logger.debug("Job %s not found in scheduler during delete", job_id)

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            # Delete history first (FK)
            cursor.execute("DELETE FROM cron_job_history WHERE job_id = ?", (job_id,))
            cursor.execute("DELETE FROM cron_jobs WHERE id = ?", (job_id,))
            deleted = cursor.rowcount > 0
            conn.commit()
        finally:
            conn.close()

        if deleted:
            logger.info("Deleted cron job %s", job_id)
        return deleted

    def pause_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Pause a job: sets status to 'paused' and pauses in APScheduler.

        Returns:
            Updated job dict, or None if not found.
        """
        job = self.get_job(job_id)
        if job is None:
            return None

        # Update status in SQLite. A paused job has no next run, so clear the
        # cached next_run_at — leaving a stale value would lie about a fire
        # time that will never happen (M1.2).
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE cron_jobs SET status = 'paused', next_run_at = NULL, "
                "updated_at = ? WHERE id = ?",
                (now, job_id),
            )
            conn.commit()
        finally:
            conn.close()

        # Pause in APScheduler
        try:
            existing = self.scheduler.get_job(job_id)
            if existing:
                self.scheduler.pause_job(job_id)
        except Exception:
            logger.debug("Job %s not found in scheduler during pause", job_id)

        logger.info("Paused cron job %s", job_id)
        return self.get_job(job_id)

    def resume_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Resume a paused job: sets status to 'active' and resumes in APScheduler.

        Returns:
            Updated job dict, or None if not found.
        """
        job = self.get_job(job_id)
        if job is None:
            return None

        # Re-read schedule for next_run_at recalculation
        try:
            trigger = self._build_trigger(job["schedule"])
            next_fire = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
            next_run_at = next_fire.isoformat() if next_fire else None
        except Exception:
            next_run_at = None

        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE cron_jobs SET status = 'active', updated_at = ?, next_run_at = ? WHERE id = ?",
                (now, next_run_at, job_id),
            )
            conn.commit()
        finally:
            conn.close()

        # Resume or re-register in APScheduler
        try:
            existing = self.scheduler.get_job(job_id)
            if existing:
                self.scheduler.resume_job(job_id)
            else:
                refreshed = self.get_job(job_id)
                if refreshed:
                    self._register_job_with_scheduler(refreshed)
        except Exception:
            logger.exception("Failed to resume job %s in scheduler", job_id)

        logger.info("Resumed cron job %s", job_id)
        return self.get_job(job_id)

    async def run_job_now(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Immediately execute a job outside of its normal schedule.

        Returns:
            The job dict after execution, or None if not found.
        """
        job = self.get_job(job_id)
        if job is None:
            return None

        logger.info("Manually triggering cron job %s (%s)", job_id, job["name"])
        await self._execute_job(job_id)
        return self.get_job(job_id)

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def get_job_history(
        self,
        job_id: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Return execution history for a job, most recent first."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM cron_job_history WHERE job_id = ? ORDER BY run_at DESC LIMIT ?",
                (job_id, limit),
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Scheduler integration
    # ------------------------------------------------------------------

    def _register_job_with_scheduler(self, job: Dict[str, Any]) -> None:
        """
        Register (or replace) a job in APScheduler using its cron schedule.
        """
        trigger = self._build_trigger(job["schedule"])

        # Remove existing entry if present (idempotent re-register)
        try:
            existing = self.scheduler.get_job(job["id"])
            if existing:
                self.scheduler.remove_job(job["id"])
        except Exception:
            pass

        self.scheduler.add_job(
            func=self._execute_job_sync_wrapper,
            trigger=trigger,
            id=job["id"],
            name=job["name"],
            args=[job["id"]],
            replace_existing=True,
            misfire_grace_time=30,  # 30 seconds grace — prevents double-fire on restart
        )

        # Persist the live next fire time so the row reflects the real clock,
        # never a frozen cache. When the scheduler is running, add_job populates
        # next_run_time immediately; persist it on create AND on every startup
        # re-register so a stale value from a prior process is overwritten (M1.2).
        #
        # Only overwrite when the scheduler actually produced a next_run_time:
        # if it is not running yet (e.g. create_job before start()), add_job
        # leaves next_run_time unset and the value computed at INSERT time stands.
        try:
            scheduled = self.scheduler.get_job(job["id"])
            next_run_time = getattr(scheduled, "next_run_time", None)
            if next_run_time is not None:
                conn = sqlite3.connect(self.db_path)
                try:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE cron_jobs SET next_run_at = ? WHERE id = ?",
                        (next_run_time.isoformat(), job["id"]),
                    )
                    conn.commit()
                finally:
                    conn.close()
        except Exception:
            logger.exception(
                "Failed to persist live next_run_at for job %s after register", job["id"]
            )

        logger.debug("Registered job %s with scheduler (schedule=%s)", job["id"], job["schedule"])

    def _execute_job_sync_wrapper(self, job_id: str) -> None:
        """
        Synchronous wrapper invoked by APScheduler from its thread-pool.

        Uses the main event loop reference captured during start() to
        safely schedule the async coroutine from a worker thread.
        """
        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed():
            logger.error("Main event loop not available; cannot execute job %s", job_id)
            return

        # CU jobs need longer timeout (10 min + buffer vs 5 min default)
        job = self.get_job(job_id)
        timeout = 660 if job and job.get("model", "").lower() in ("computer-use", "cu") else 300

        future = asyncio.run_coroutine_threadsafe(self._execute_job(job_id), loop)
        try:
            future.result(timeout=timeout)
        except Exception:
            logger.exception("Job %s execution raised an exception", job_id)

    async def _execute_job(self, job_id: str) -> None:
        """
        Core execution callback.

        Loads the job from SQLite, invokes the executor, records history,
        and updates run statistics.  If the job is marked as one_shot,
        deletes it after execution.
        """
        job = self.get_job(job_id)
        if job is None:
            logger.warning("_execute_job called for non-existent job %s", job_id)
            return

        run_at = datetime.now(timezone.utc)
        start_ms = int(run_at.timestamp() * 1000)
        result_text: Optional[str] = None
        error_text: Optional[str] = None
        delivery_status: str = "pending"

        try:
            # Lazy import to avoid circular dependencies.
            # executor.py is created in a later task.
            # TODO: Replace stub with real executor call once executor.py exists.
            try:
                from Orchestrator.scheduler.executor import execute_cron_job
                result_text = await execute_cron_job(job)
                delivery_status = "delivered"
            except ImportError:
                logger.warning(
                    "Executor module not yet available; stub-executing job %s (%s)",
                    job_id,
                    job["name"],
                )
                result_text = f"[stub] Job '{job['name']}' executed (executor not implemented)"
                delivery_status = "stub"

        except Exception as exc:
            error_text = str(exc)
            delivery_status = "error"
            logger.exception("Error executing cron job %s", job_id)

        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        duration_ms = end_ms - start_ms

        # ----- Record history row -----
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO cron_job_history
                    (job_id, run_at, prompt, model, result, delivery_status, duration_ms, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    run_at.isoformat(),
                    job["prompt"],
                    job["model"],
                    result_text,
                    delivery_status,
                    duration_ms,
                    error_text,
                ),
            )

            # ----- Update job stats -----
            # Recalculate next_run_at
            try:
                trigger = self._build_trigger(job["schedule"])
                next_fire = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
                next_run_at = next_fire.isoformat() if next_fire else None
            except Exception:
                next_run_at = None

            last_run_result = "success" if error_text is None else "error"

            if error_text is None:
                cursor.execute(
                    """
                    UPDATE cron_jobs
                    SET last_run_at         = ?,
                        last_run_result     = ?,
                        last_run_duration_ms = ?,
                        next_run_at         = ?,
                        run_count           = run_count + 1,
                        updated_at          = ?
                    WHERE id = ?
                    """,
                    (
                        run_at.isoformat(),
                        last_run_result,
                        duration_ms,
                        next_run_at,
                        datetime.now(timezone.utc).isoformat(),
                        job_id,
                    ),
                )
            else:
                cursor.execute(
                    """
                    UPDATE cron_jobs
                    SET last_run_at         = ?,
                        last_run_result     = ?,
                        last_run_duration_ms = ?,
                        next_run_at         = ?,
                        run_count           = run_count + 1,
                        error_count         = error_count + 1,
                        updated_at          = ?
                    WHERE id = ?
                    """,
                    (
                        run_at.isoformat(),
                        last_run_result,
                        duration_ms,
                        next_run_at,
                        datetime.now(timezone.utc).isoformat(),
                        job_id,
                    ),
                )

            conn.commit()
        finally:
            conn.close()

        logger.info(
            "Executed cron job %s (%s) result=%s duration=%dms",
            job_id,
            job["name"],
            last_run_result,
            duration_ms,
        )

        # ----- One-shot cleanup -----
        if job.get("one_shot"):
            logger.info("One-shot job %s completed, deleting", job_id)
            self.delete_job(job_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Human-readable schedule label (cron is the single source of truth)
    # ------------------------------------------------------------------

    # Day-of-week names (cron: 0/7=Sun .. 6=Sat).
    _DOW_NAMES = {
        0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed",
        4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun",
    }

    # Named day-of-week tokens -> cron numbers (so "mon-fri"/"sun" are
    # understood identically to "1-5"/"0"). APScheduler accepts the named forms.
    _DOW_NAME_TO_NUM = {
        "sun": "0", "mon": "1", "tue": "2", "wed": "3",
        "thu": "4", "fri": "5", "sat": "6",
    }

    @classmethod
    def _hint_from_cron(cls, schedule: str) -> str:
        """
        Derive a compact, human-readable frequency hint from a 5-field cron
        expression. The cron is the single source of truth, so this label can
        never contradict the actual schedule. All times are box-local, marked
        with a trailing "(local)".

        Covers the common minute / hour / day-of-week cases; anything it does
        not specifically recognise degrades to echoing the raw cron, still
        suffixed "(local)".
        """
        suffix = " (local)"
        raw = (schedule or "").strip()
        parts = raw.split()
        if len(parts) != 5:
            return (raw or "custom schedule") + suffix

        minute, hour, dom, month, dow = parts

        def _is_every(field: str) -> bool:
            return field == "*"

        # --- Sub-hour / sub-day rates ---------------------------------------
        # "*/N * * * *"  -> every N minutes
        if hour == "*" and dom == "*" and month == "*" and dow == "*":
            if minute == "*":
                return "Every minute" + suffix
            if minute.startswith("*/"):
                step = minute[2:]
                return f"Every {step} minutes" + suffix
            if minute.isdigit():
                return f"Hourly at :{int(minute):02d}" + suffix

        # "M */N * * *" -> every N hours at minute M
        if (
            minute.isdigit()
            and hour.startswith("*/")
            and dom == "*"
            and month == "*"
            and dow == "*"
        ):
            step = hour[2:]
            return f"Every {step} hours at :{int(minute):02d}" + suffix

        # --- Fixed time-of-day cases ----------------------------------------
        time_label = None
        if minute.isdigit() and hour.isdigit():
            time_label = f"{int(hour):02d}:{int(minute):02d}"

        if time_label is not None:
            dow_label = cls._describe_dow(dow)
            dom_is_every = _is_every(dom)
            month_is_every = _is_every(month)

            if dow_label is not None:
                # Day-of-week constrained (named or numeric).
                return f"{dow_label} at {time_label}" + suffix

            # Date-based phrasing applies ONLY when the day-of-week field is
            # unconstrained ("*"). A constrained-but-unrecognised dow must never
            # be mislabelled "Daily" (that would re-introduce the very label vs
            # schedule contradiction this method exists to prevent) — it falls
            # through to the raw-cron echo instead.
            if _is_every(dow):
                if dom_is_every and month_is_every:
                    return f"Daily at {time_label}" + suffix

                if dom.isdigit() and month_is_every:
                    return f"Monthly on day {int(dom)} at {time_label}" + suffix

                if dom.isdigit() and month.isdigit():
                    return (
                        f"Yearly on {int(month):02d}-{int(dom):02d} at {time_label}"
                        + suffix
                    )

        # --- Fallback: echo the cron, still box-local -----------------------
        return raw + suffix

    @classmethod
    def _normalize_dow_names(cls, dow: str) -> str:
        """Replace named day tokens (mon, fri, sun, ...) with cron numbers, so
        the digit-based logic in _describe_dow handles named and numeric forms
        identically (e.g. 'mon-fri' -> '1-5', 'sat,sun' -> '6,0')."""
        import re
        return re.sub(
            r"[A-Za-z]+",
            lambda m: cls._DOW_NAME_TO_NUM.get(m.group(0).lower()[:3], m.group(0)),
            dow,
        )

    @classmethod
    def _describe_dow(cls, dow: str) -> Optional[str]:
        """
        Describe a cron day-of-week field, or None if it means 'every day'
        (dow == '*') OR the field is constrained but unrecognised (the caller
        then echoes the raw cron rather than mislabelling it).

        Handles '*', ranges ('1-5'/'mon-fri'), lists ('1,3,5'/'sat,sun') and
        single days, in both numeric and named (mon..sun) forms.
        """
        if dow == "*" or dow == "?":
            return None

        # Normalise named days to cron numbers up front.
        dow = cls._normalize_dow_names(dow)

        # Weekday / weekend shorthands.
        if dow == "1-5":
            return "Weekdays"
        if dow in ("0,6", "6,0", "6,7"):
            return "Weekends"

        def _name(token: str) -> Optional[str]:
            if token.isdigit():
                return cls._DOW_NAMES.get(int(token) % 8 if int(token) == 7 else int(token))
            return None

        # Range a-b.
        if "-" in dow and "," not in dow:
            lo, _, hi = dow.partition("-")
            lo_name, hi_name = _name(lo), _name(hi)
            if lo_name and hi_name:
                return f"{lo_name}-{hi_name}"

        # Explicit list a,b,c (and single day, which has no comma).
        tokens = dow.split(",")
        names = [_name(t) for t in tokens]
        if all(names):
            return ", ".join(names)  # type: ignore[arg-type]

        # Unrecognised — let the caller fall back to echoing the cron.
        return None

    @staticmethod
    def _job_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        """Convert a sqlite3.Row from cron_jobs into a plain dict."""
        d = dict(row)
        # Normalise one_shot from int to bool for API consumers
        d["one_shot"] = bool(d.get("one_shot", 0))
        return d
