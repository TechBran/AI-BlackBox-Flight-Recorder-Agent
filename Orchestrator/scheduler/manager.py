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
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# The notification bus (MN.2). _execute_job is async, so it awaits notify(...)
# directly - no sync->async bridge. notify() never raises and always records a
# searchable snapshot; we still wrap our calls in try/except so a bus regression
# can never break a job's own error bookkeeping. Imported at module scope so
# tests can monkeypatch `manager.notify`.
from Orchestrator.notifications.bus import notify

logger = logging.getLogger(__name__)

# Operators we never push a notification to (pushing "to nobody"). 'system' is
# the shared, ownerless operator used by infrastructure jobs; a blank/None
# operator has no subscriber to route to. Consistent with other producers that
# guard the system/blank operator before fanning out.
_NO_NOTIFY_OPERATORS = {None, "", "system"}


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
# Retry-on-failure configuration (M2.8)
#
# A failed execution is retried up to MAX_RETRIES times (so MAX_RETRIES+1
# attempts total) WITHIN the single held per-job lock — no APScheduler
# re-enqueue. RETRY_BACKOFF_SECONDS[i] is the delay BEFORE retry i (it is
# indexed by retry number, so index 0 precedes the first retry). The list is
# clamped to its last value if there are more retries than entries. Tests
# monkeypatch RETRY_BACKOFF_SECONDS to zeros for speed.
# ---------------------------------------------------------------------------
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = [5, 30]

# ---------------------------------------------------------------------------
# Misfire grace window (M3.1)
#
# How long after a job's scheduled fire time APScheduler will still run it
# (rather than silently dropping it as "missed"). Deliberately WIDE — a fire
# missed during a brief alive-but-busy window (event loop saturated, a long
# prior run holding the per-job lock) must still count, not vanish. Six hours
# gives generous headroom for those in-process stalls.
#
# This is NOT the cold-restart catch-up mechanism: a fire missed because the
# whole process was DOWN is handled by start()'s persisted-next_run_at catch-up
# (M3.2), since misfire_grace_time only applies to a live scheduler.
# ---------------------------------------------------------------------------
MISFIRE_GRACE_SECONDS = 6 * 3600

# ---------------------------------------------------------------------------
# Single-run job defaults (M3.1)
#
# coalesce=True       — if multiple fires of one job pile up while the scheduler
#                       is alive, collapse them into a SINGLE run (never N).
# max_instances=1     — never run two instances of the same job concurrently
#                       (defence-in-depth alongside the per-job asyncio lock).
#
# Applied via job_defaults on the scheduler ctor AND per add_job so they hold
# for both the live scheduler and every registered job.
# ---------------------------------------------------------------------------
JOB_DEFAULTS = {
    "coalesce": True,
    "max_instances": 1,
    "misfire_grace_time": MISFIRE_GRACE_SECONDS,
}

# ---------------------------------------------------------------------------
# Cold-restart catch-up concurrency cap (M3.2 follow-up)
#
# After a long outage, many jobs can be past-due at once. start() enqueues one
# catch-up task PER such job, and each drives a full /chat round-trip (plus any
# SMS/voice delivery). The per-job lock only serialises a job against ITSELF —
# nothing throttles ACROSS jobs — so an unbounded burst would hit the LLM
# provider (rate-limit/429) and the SMS/voice path all at once on boot. This
# semaphore drains the catch-up burst a few at a time. It does NOT delay startup
# (tasks are still spawned immediately) and does NOT affect normal scheduled
# fires (those never go through the catch-up path).
# ---------------------------------------------------------------------------
CATCHUP_CONCURRENCY = 3

# ---------------------------------------------------------------------------
# Field validation allow-lists (M2.1)
#
# Validation lives at ONE sink (CronJobManager._validate_job_fields, called by
# both create_job and update_job) so all four surfaces — HTTP API, Portal,
# ToolVault tool, Android — inherit it. The most dangerous bug this closes: a
# job with delivery='sms'/'voice_call' but a blank delivery_target reported
# success then silently never delivered (the executor's _build_prompt fell
# through to snapshot mode).
# ---------------------------------------------------------------------------
VALID_DELIVERY = {"snapshot", "sms", "voice_call", "notification"}
# 'error' (M3.2): a job whose cron failed to register with the live scheduler
# on startup. It is a recognised terminal-ish state — the job stays in the DB
# and surfaces to operators rather than being silently dropped while it sits
# 'active' but never fires. It is written by a direct UPDATE (not update_job),
# which would re-validate and re-register it.
VALID_STATUS = {"active", "paused", "error"}
# Deliveries that require a real phone number.
_TARGETED_DELIVERY = {"sms", "voice_call"}
# E.164: a leading '+', a non-zero leading digit, then 6-14 more digits.
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")

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
    "provider",
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
        # job_defaults enforce the locked single-run policy (M3.1): coalesce
        # piled-up fires into one + never run two instances concurrently.
        self.scheduler = AsyncIOScheduler(timezone=LOCAL_TZ, job_defaults=JOB_DEFAULTS)
        # Per-job execution locks (M2.6): serialise runs of the SAME job so a
        # manual run-now can never collide with a scheduled fire and double-
        # execute. A run that finds the lock already held SKIPS (it does not
        # queue/block). Created lazily per job_id on first execution.
        self._job_locks: Dict[str, asyncio.Lock] = {}
        # Strong refs to in-flight cold-restart catch-up tasks (M3.2). The event
        # loop only weakly references a bare asyncio.Task, so without retaining
        # it the GC could collect a catch-up run mid-flight and it would silently
        # never complete (the same footgun as the run-now route). Each task
        # self-removes via a done-callback.
        self._catchup_tasks: "set[asyncio.Task]" = set()
        # Caps how many cold-restart catch-up runs execute concurrently so a
        # big post-outage burst drains a few at a time instead of detonating
        # all at once against the LLM/SMS/voice path (M3.2 follow-up).
        self._catchup_semaphore = asyncio.Semaphore(CATCHUP_CONCURRENCY)
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
            # M3.3: WAL persists at the DB-file level, so concurrent readers
            # (e.g. GET /api/cron/health) never block the scheduler's writes,
            # and a writer never blocks readers. busy_timeout makes a connection
            # that hits a transient lock WAIT (up to 5s) rather than immediately
            # raising "database is locked". WAL is a one-time durable switch;
            # busy_timeout is per-connection, so it is re-applied on each connect.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
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
            # M4.1a: explicit `provider` column (migration-safe). A cron job
            # carries a SPECIFIC model id for ANY provider, so the row needs to
            # remember WHICH provider that id belongs to (an empty/Auto model
            # has no substring to guess from). ALTER it in only when absent so
            # an existing DB created before this column upgrades cleanly and a
            # second construction is a no-op. Newly stored rows still derive
            # nothing here — defaults stay NULL and _job_to_dict backfills.
            existing_cols = {
                row[1] for row in cursor.execute("PRAGMA table_info(cron_jobs)")
            }
            if "provider" not in existing_cols:
                cursor.execute("ALTER TABLE cron_jobs ADD COLUMN provider TEXT")
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
        """Load persisted active jobs into APScheduler and start the scheduler.

        Cold-restart catch-up (M3.2): the persisted next_run_at is the durable
        record of each job's next due fire. For every active job we CAPTURE
        that stored value BEFORE _register_job_with_scheduler overwrites it with
        the next FUTURE fire. If the captured value is in the PAST, the job was
        due while the process was DOWN → we enqueue EXACTLY ONE coalesced
        catch-up run (never N, even if several fires were missed). The catch-up
        goes through _execute_job, so the per-job lock prevents it from
        colliding with the job's normal next fire.
        """
        # Capture the main event loop so _execute_job_sync_wrapper can
        # schedule coroutines from APScheduler's thread-pool threads.
        self._loop = asyncio.get_running_loop()

        # Start the scheduler BEFORE registering jobs so that each add_job
        # computes a live next_run_time, which _register_job_with_scheduler
        # then persists — recomputing any next_run_at frozen by a prior
        # process against the current box-local clock (M1.2).
        self.scheduler.start()

        now = datetime.now(LOCAL_TZ)
        jobs = self.list_jobs(status="active")
        loaded = 0
        caught_up = 0
        for job in jobs:
            # Capture the PRE-restart next_run_at BEFORE re-registering, which
            # overwrites it with the next future fire (M3.2 ordering).
            captured_next_run_at = job.get("next_run_at")
            try:
                self._register_job_with_scheduler(job)
                loaded += 1
            except Exception:
                logger.warning(
                    "Failed to register job %s on startup; marking status='error'",
                    job["id"],
                    exc_info=True,
                )
                self._mark_job_status(job["id"], "error")
                # A job that won't register also won't be caught up — skip it.
                continue

            # Cold-restart catch-up: a captured next_run_at in the past means
            # the job was due during downtime. Enqueue ONE catch-up. Even if
            # several fires were missed, this is the FIRST missed fire; we run
            # it once and the re-register already set the next future fire.
            if self._is_past(captured_next_run_at, now):
                self._enqueue_catchup(job["id"])
                caught_up += 1

        logger.info(
            "CronJobManager started – %d active job(s) loaded from %d total",
            loaded,
            len(jobs),
        )
        logger.info(
            "[CRON] startup: caught up %d missed job(s), %d up-to-date",
            caught_up,
            loaded - caught_up,
        )

    @staticmethod
    def _is_past(next_run_at: Optional[str], now: datetime) -> bool:
        """True if a persisted next_run_at parses to a time strictly before now.

        Returns False for a null/blank/unparseable value (no spurious catch-up
        on a job that never had a next fire). The comparison is timezone-aware:
        a naive stored timestamp is interpreted as box-local.
        """
        if not next_run_at:
            return False
        try:
            parsed = datetime.fromisoformat(next_run_at)
        except (ValueError, TypeError):
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TZ)
        return parsed < now

    def _enqueue_catchup(self, job_id: str) -> None:
        """Enqueue exactly ONE catch-up run for a job missed during downtime.

        Runs WITHOUT blocking startup: a tracked asyncio.Task drives the single
        run through _execute_job (whose per-job lock prevents it from colliding
        with the job's normal next fire). The task ref is retained in
        self._catchup_tasks (with a done-callback that discards it) so the GC
        can't collect a long-running catch-up mid-flight.
        """
        logger.info("[CRON] startup: enqueueing catch-up run for missed job %s", job_id)

        async def _run() -> None:
            try:
                # Throttle the post-outage burst: at most CATCHUP_CONCURRENCY
                # catch-up runs execute at once (normal scheduled fires are
                # unaffected — they never reach here).
                async with self._catchup_semaphore:
                    await self._execute_job(job_id)
            except Exception:
                logger.exception("Catch-up run failed for cron job %s", job_id)

        task = asyncio.ensure_future(_run())
        self._catchup_tasks.add(task)
        task.add_done_callback(self._catchup_tasks.discard)

    def _mark_job_status(self, job_id: str, status: str) -> None:
        """Write a job's status via a direct UPDATE (M3.2 reload hardening).

        Used to flag a job 'error' when it fails to register on startup. A
        direct UPDATE is deliberate: update_job would re-validate and (for an
        active status) re-register — exactly what just failed. This only stamps
        the status + updated_at so the broken job surfaces to operators.
        """
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE cron_jobs SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, job_id),
            )
            conn.commit()
        finally:
            conn.close()

    async def shutdown(self) -> None:
        """Gracefully shut down the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)
            logger.info("CronJobManager shut down gracefully")

    # ------------------------------------------------------------------
    # Central field validation (M2.1)
    # ------------------------------------------------------------------

    def _validate_job_fields(self, fields: Dict[str, Any], *, partial: bool) -> None:
        """Validate cron-job fields at the single manager sink.

        Called by both create_job (partial=False) and update_job
        (partial=True). Raises ValueError with a clear message on any bad
        value so every surface that goes through the manager — HTTP API,
        Portal, ToolVault tool, Android — inherits identical rules.

        On create (partial=False) ``operator`` is always required. On a
        partial update only the fields actually present are checked, EXCEPT
        the delivery/delivery_target coupling: callers that transition delivery
        to sms/voice_call resolve the *effective* delivery+target (existing job
        merged with the update) before calling, so a valid E.164 target is
        always required when the effective delivery is targeted.

        Args:
            fields: The field values to validate (effective values for the
                delivery coupling on update).
            partial: True for update_job (only validate present fields), False
                for create_job (operator required).
        """
        # --- delivery enum ---
        if "delivery" in fields and fields["delivery"] is not None:
            delivery = fields["delivery"]
            if delivery not in VALID_DELIVERY:
                raise ValueError(
                    f"Invalid delivery '{delivery}'. Must be one of: "
                    f"{', '.join(sorted(VALID_DELIVERY))}."
                )

        # --- status enum ---
        if "status" in fields and fields["status"] is not None:
            status = fields["status"]
            if status not in VALID_STATUS:
                raise ValueError(
                    f"Invalid status '{status}'. Must be one of: "
                    f"{', '.join(sorted(VALID_STATUS))}."
                )

        # --- operator: required on create, non-blank whenever present ---
        operator_present = "operator" in fields
        if not partial or operator_present:
            operator = fields.get("operator")
            if not operator_present or operator is None or not str(operator).strip():
                raise ValueError("operator is required and must be non-empty.")

        # --- delivery_target coupling: sms/voice_call need a valid E.164 ---
        # Only enforce when the (effective) delivery is a targeted kind. The
        # caller passes the effective delivery+target so transitions are caught.
        delivery = fields.get("delivery")
        if delivery in _TARGETED_DELIVERY:
            target = fields.get("delivery_target")
            if target is None or not str(target).strip():
                raise ValueError(
                    f"delivery '{delivery}' requires a delivery_target "
                    f"(phone number in E.164 format, e.g. +15551234567)."
                )
            if not _E164_RE.match(str(target).strip()):
                raise ValueError(
                    f"delivery_target '{target}' is not a valid E.164 phone "
                    f"number (expected +<country><number>, e.g. +15551234567)."
                )

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
        # M4.1a: explicit provider. Stored verbatim when given; left None
        # otherwise so _job_to_dict backfills it from the model on read (legacy
        # rows and callers that only know a model still report a provider).
        provider = kwargs.get("provider")
        delivery = kwargs.get("delivery") or "snapshot"
        delivery_target = kwargs.get("delivery_target")

        # Central field validation (M2.1). delivery has already defaulted to
        # 'snapshot' above, so we only ever validate constrained values.
        self._validate_job_fields(
            {
                "operator": operator,
                "delivery": delivery,
                "delivery_target": delivery_target,
                "status": "active",
            },
            partial=False,
        )
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
                    model, provider, delivery, delivery_target, operator,
                    status, one_shot, created_at, updated_at, next_run_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    name,
                    prompt,
                    schedule,
                    frequency_hint,
                    model,
                    provider,
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
        provider, delivery, delivery_target, operator, status, one_shot.

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
            "provider",
            "delivery",
            "delivery_target",
            "operator",
            "status",
            "one_shot",
        }

        updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not updates:
            return self.get_job(job_id)

        # Central field validation (M2.1). Only validate the fields actually
        # being updated (partial=True) — but resolve the *effective* delivery
        # and delivery_target by merging the update over the existing job, so a
        # transition INTO sms/voice_call is caught even when the update only
        # flips `delivery` and the target already lives on the row (or vice
        # versa). The existing row is only fetched when a delivery-related
        # field is in play, keeping the common no-delivery update cheap.
        validation_fields: Dict[str, Any] = {
            k: updates[k] for k in ("operator", "status") if k in updates
        }
        if "delivery" in updates or "delivery_target" in updates:
            existing = self.get_job(job_id)
            if existing is None:
                return None
            effective_delivery = (
                updates["delivery"] if "delivery" in updates else existing.get("delivery")
            )
            effective_target = (
                updates["delivery_target"]
                if "delivery_target" in updates
                else existing.get("delivery_target")
            )
            validation_fields["delivery"] = effective_delivery
            validation_fields["delivery_target"] = effective_target
        self._validate_job_fields(validation_fields, partial=True)

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
            # Drop the per-job execution lock so deleted jobs don't accumulate
            # stale Lock objects (and a reused id can't inherit an old lock).
            self._job_locks.pop(job_id, None)
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
            # M3.1: collapse piled-up fires into one run + never run two
            # instances of the same job at once. Set explicitly here (not only
            # via the scheduler's job_defaults) so the policy is unambiguous on
            # every registered job. The wide MISFIRE_GRACE_SECONDS keeps a fire
            # missed during a brief alive-but-busy window eligible to run.
            coalesce=True,
            max_instances=1,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
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

        # CU jobs need a longer timeout (10 min + buffer vs 5 min default).
        # Key off the authoritative provider (M4.1a) — a CU job now carries a
        # SPECIFIC CU model id (e.g. a "gemini-...computer-use" id), so the old
        # model-string check ("computer-use"/"cu") would miss it and wrongly
        # apply the short 300s budget to a CU run. _job_to_dict backfills the
        # provider for legacy rows, so this stays correct for old CU jobs too.
        job = self.get_job(job_id)
        is_cu = bool(
            job
            and (
                (job.get("provider") or "").lower() == "computer-use"
                or job.get("model", "").lower() in ("computer-use", "cu")
            )
        )
        timeout = 660 if is_cu else 300

        future = asyncio.run_coroutine_threadsafe(self._execute_job(job_id), loop)
        try:
            future.result(timeout=timeout)
        except Exception:
            logger.exception("Job %s execution raised an exception", job_id)

    async def _execute_job(self, job_id: str) -> None:
        """
        Core execution entry point with a per-job execution lock (M2.6).

        Serialises runs of the SAME job: if this job's lock is already held
        (a scheduled fire or another manual run-now is in flight), this call
        SKIPS — it records a "skipped: already running" history note and
        returns immediately rather than queueing/blocking and double-running.
        Otherwise the real execution body runs inside the lock.

        The check-then-acquire below is safe because the event loop is single
        threaded and there is no ``await`` between ``lock.locked()`` and
        ``async with lock`` — no other coroutine can interleave and grab the
        lock in that window.
        """
        lock = self._job_locks.setdefault(job_id, asyncio.Lock())
        if lock.locked():
            logger.info(
                "Skipping cron job %s: a run is already in progress", job_id
            )
            self._record_skipped_run(job_id)
            return

        async with lock:
            await self._run_job_body(job_id)

    async def _run_job_body(self, job_id: str) -> None:
        """
        Core execution body (runs INSIDE the per-job lock).

        Loads the job from SQLite and runs it with bounded retry-on-failure
        (M2.8): up to MAX_RETRIES retries (MAX_RETRIES+1 attempts total), each
        ATTEMPT writing its own history row, with a backoff sleep between
        attempts. A succeeding attempt stops further retries. All retries run
        within this single held lock — no APScheduler re-enqueue.

        After the attempts resolve, run statistics are updated EXACTLY once:
        run_count is incremented for the run, and error_count is incremented
        once (not per attempt) only if the run ultimately failed; the job is
        left scheduled/active on failure. A one-shot is deleted only on final
        success (a failed one-shot survives — M2.6).
        """
        job = self.get_job(job_id)
        if job is None:
            logger.warning("_execute_job called for non-existent job %s", job_id)
            return

        # --- Bounded retry loop: each attempt writes its own history row. ---
        final_error: Optional[str] = None
        last_duration_ms = 0
        last_run_at = datetime.now(timezone.utc)
        succeeded = False
        # The successful attempt's reply text — captured for a
        # delivery='notification' push after stats are written (M5.1).
        success_result_text: Optional[str] = None

        for attempt in range(MAX_RETRIES + 1):
            result_text, error_text, duration_ms, attempt_run_at = (
                await self._attempt_once(job)
            )
            last_duration_ms = duration_ms
            last_run_at = attempt_run_at

            if error_text is None:
                final_error = None
                succeeded = True
                success_result_text = result_text
                if attempt > 0:
                    logger.info(
                        "Cron job %s succeeded on retry %d/%d",
                        job_id, attempt, MAX_RETRIES,
                    )
                break

            final_error = error_text
            # More attempts remain → back off, then retry within the lock.
            if attempt < MAX_RETRIES:
                backoff = self._retry_backoff_for(attempt)
                logger.warning(
                    "Cron job %s attempt %d/%d failed (%s); retrying in %ss",
                    job_id, attempt + 1, MAX_RETRIES + 1, error_text, backoff,
                )
                if backoff:
                    await asyncio.sleep(backoff)

        # --- Update job stats ONCE for the whole run (not per attempt). ---
        try:
            trigger = self._build_trigger(job["schedule"])
            next_fire = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
            next_run_at = next_fire.isoformat() if next_fire else None
        except Exception:
            next_run_at = None

        last_run_result = "success" if succeeded else "error"
        now_iso = datetime.now(timezone.utc).isoformat()

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            if succeeded:
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
                        last_run_at.isoformat(),
                        last_run_result,
                        last_duration_ms,
                        next_run_at,
                        now_iso,
                        job_id,
                    ),
                )
            else:
                # error_count increments ONCE for the failed run as a whole,
                # not once per failed attempt.
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
                        last_run_at.isoformat(),
                        last_run_result,
                        last_duration_ms,
                        next_run_at,
                        now_iso,
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
            last_duration_ms,
        )

        # ----- One-shot cleanup (only on success) -----
        # A one-shot whose run FAILED (all retries exhausted) is left in place
        # so it is not silently destroyed; it survives for inspection (M2.6).
        if job.get("one_shot") and succeeded:
            logger.info("One-shot job %s completed successfully, deleting", job_id)
            self.delete_job(job_id)

        # ----- Notification bus hooks (M5.1) -----
        # Run LAST, after stats are durably written above, so a notify
        # failure can never undo the job's own bookkeeping. fire_ts pins the
        # dedup_key to THIS run so a retry-storm collapses to one alert.
        await self._notify_run_outcome(
            job,
            succeeded=succeeded,
            error_text=final_error,
            result_text=success_result_text,
            fire_ts=last_run_at.isoformat(),
        )

    async def _notify_run_outcome(
        self,
        job: Dict[str, Any],
        *,
        succeeded: bool,
        error_text: Optional[str],
        result_text: Optional[str],
        fire_ts: str,
    ) -> None:
        """Push the run's outcome to the notification bus (M5.1).

        Two cases, each wrapped so a bus failure NEVER propagates back into
        _run_job_body (the job's status/stats are already durably written by
        the time we get here):

          * TERMINAL failure (all retries exhausted) -> notify(category='alert')
            EXACTLY once. The dedup_key (cronfail:<job_id>:<fire_ts>) is stable
            for this run, so the bus collapses any duplicate into one logical
            alert.
          * SUCCESS with delivery='notification' -> notify(category='cron')
            with the reply text. This finally realizes the long-dead
            'notification' delivery mode (it used to silently fall through to
            snapshot). 'snapshot' delivery does NOT notify — auto-mint in the
            /chat pipeline already persisted it.

        Both cases suppress the 'system'/blank operator (we never push to
        nobody), consistent with the other notification producers.
        """
        operator = job.get("operator")
        if operator in _NO_NOTIFY_OPERATORS:
            return

        job_id = job["id"]
        job_name = job.get("name") or job_id

        if not succeeded:
            # Terminal failure alert — idempotent per terminal failure via
            # the dedup_key. notify() itself never raises, but we still guard
            # so even an import/monkeypatch-level error stays contained.
            try:
                await notify(
                    operator=operator,
                    category="alert",
                    title=f"Cron job failed: {job_name}",
                    body=error_text or "Unknown error",
                    dedup_key=f"cronfail:{job_id}:{fire_ts}",
                )
            except Exception:
                logger.exception(
                    "Failed to send failure alert for cron job %s", job_id
                )
            return

        # Success: realize delivery='notification' by actually delivering.
        if (job.get("delivery") or "snapshot") == "notification":
            try:
                await notify(
                    operator=operator,
                    category="cron",
                    title=job_name,
                    body=result_text or "",
                )
            except Exception:
                logger.exception(
                    "Failed to deliver notification for cron job %s", job_id
                )

    @staticmethod
    def _retry_backoff_for(attempt: int) -> float:
        """Backoff (seconds) to wait BEFORE the retry following ``attempt``.

        Indexed by attempt number; clamps to the last configured value when
        there are more retries than entries. An empty list means no backoff.
        """
        if not RETRY_BACKOFF_SECONDS:
            return 0
        idx = min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)
        return RETRY_BACKOFF_SECONDS[idx]

    async def _attempt_once(self, job: Dict[str, Any]):
        """Run the executor once and write THIS attempt's history row.

        Returns ``(result_text, error_text, duration_ms, run_at)``. A non-None
        error_text means the attempt failed.
        """
        job_id = job["id"]
        run_at = datetime.now(timezone.utc)
        start_ms = int(run_at.timestamp() * 1000)
        result_text: Optional[str] = None
        error_text: Optional[str] = None
        delivery_status: str = "pending"

        try:
            # Lazy import to avoid circular dependencies. executor.py always
            # exists and is importable, so any failure surfaces as an error.
            from Orchestrator.scheduler.executor import execute_cron_job
            result_text = await execute_cron_job(job)
            delivery_status = "delivered"
        except Exception as exc:
            error_text = str(exc)
            delivery_status = "error"
            logger.exception("Error executing cron job %s", job_id)

        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        duration_ms = end_ms - start_ms

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
            conn.commit()
        finally:
            conn.close()

        return result_text, error_text, duration_ms, run_at

    def _record_skipped_run(self, job_id: str) -> None:
        """Write a history row noting a run was skipped because the job was
        already running (M2.6). This does NOT touch run_count/error_count —
        no work was performed; it is purely an audit trail of the skip."""
        job = self.get_job(job_id)
        if job is None:
            return
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO cron_job_history
                    (job_id, run_at, prompt, model, result, delivery_status,
                     duration_ms, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    now,
                    job["prompt"],
                    job["model"],
                    None,
                    "skipped",
                    0,
                    "skipped: a run for this job was already running",
                ),
            )
            conn.commit()
        finally:
            conn.close()

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
        # M4.1a: backfill provider from the model when the row has none. Legacy
        # rows (written before the provider column) and callers that only set a
        # model still report a sensible provider derived from the stored model.
        # Lazy import keeps the executor<->manager edge out of import time.
        if not d.get("provider"):
            from Orchestrator.scheduler.executor import _model_to_provider
            d["provider"] = _model_to_provider(d.get("model") or "")
        return d
