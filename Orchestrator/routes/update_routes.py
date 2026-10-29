"""Update pipeline backend routes (T7).

Mounted at /update/* by Orchestrator/app.py.

Endpoints:
  GET  /update/status            cached git state + commits-behind + categories
  POST /update/preflight         force fresh git fetch + active-session check
  POST /update/start             begin update (returns task_id)
  GET  /update/log/stream        SSE stream of phase events + log lines
  POST /update/rollback          revert to last pre-update-<ts> tag + restart
                                 (returns a task_id; same SSE stream as start)
  GET  /update/state             persisted Manifest/update_state.json (recovery)

DESIGN NOTES:
  - Single-process mutex via UpdateManager.acquire_or_raise (flock). The
    rollback holds it too.
  - Local changes to tracked files (audit O16-01): status and preflight list
    them as dirty_files; start and rollback answer 409 with that list unless
    the body carries discard_local_changes: true, and the runner then stashes
    them before any reset. Untracked runtime files never count.
  - git calls inside the async handlers run in worker threads (O16-09); the
    sync handlers already run in FastAPI's threadpool.
  - /update/status caches the git_ops.fetch result for 60s to avoid
    GitHub rate-limiting (audit M7) when multiple Portal tabs auto-poll.
  - The pinned update origin (2026-10 audit X04-security-clients-infra-11,
    verify.check_origin) is checked before status, preflight or the
    confirm_sha race check fetch anything: an origin the pin does not list is
    reported as fetch_error (refused: "unverified_update") and never fetched
    from, so no panel lists its commits as an update. latest_sha is main as
    the origin itself reports it, the commit Install would install.
  - /update/log/stream uses the same SSE pattern as /onboarding/logs/stream
    (newline-delimited "data: <json>\\n\\n" frames).
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from Orchestrator.update import changes as changes_mod
from Orchestrator.update import git_ops
from Orchestrator.update import verify
from Orchestrator.update.active_sessions import (
    count_active_cli_sessions, list_active_cli_sessions,
)
from Orchestrator.update.manager import (
    UpdateManager, UpdateInProgressError, TERMINAL_PHASES,
)
from Orchestrator.update.runner import UpdateRunner, dirty_tree_message
from Orchestrator.utils.paths import blackbox_root

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/update", tags=["update"])

# Module-level singletons. blackbox_root() resolves via env var + sentinel
# detection (Orchestrator/utils/paths.py). UpdateManager is stateless wrt
# git state — only owns the flock + JSON state file location.
_ROOT = blackbox_root()
_MGR = UpdateManager(_ROOT)

# ── /update/status cache (audit M7) ─────────────────────────────────────
# Caches the git-fetch result. Bust on /update/preflight (explicit "Check
# for updates" button). Default TTL 60s — long enough to avoid github.com
# rate-limit when 10 customers' Portal tabs auto-poll every menu open.
_STATUS_CACHE: dict = {}
_STATUS_CACHE_TS: float = 0.0
_STATUS_CACHE_TTL_S: float = 60.0


# ── Request/response models ─────────────────────────────────────────────

class StartRequest(BaseModel):
    """Body for POST /update/start. confirm_sha is the latest SHA the UI
    showed to the user — if origin/main has moved since (race), refuse
    so the operator re-confirms against the actually-latest changes."""
    confirm_sha: Optional[str] = None
    # The operator's explicit consent to stash local changes to tracked files
    # (the UI showed them dirty_files first). Without it a dirty tree is a 409.
    discard_local_changes: bool = False


class RollbackRequest(BaseModel):
    """Optional body for POST /update/rollback (older clients post none)."""
    discard_local_changes: bool = False


def _dirty_files(root: Path) -> list[str]:
    """Tracked files with local changes, for the panels. Best effort: [] when
    git cannot answer; the runner checks again, authoritatively, under the
    lock before it resets anything."""
    try:
        return git_ops.tracked_changes(root)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return []


def _dirty_refusal(dirty: list[str], action: str) -> JSONResponse:
    """409 for a dirty tree. `detail` stays a sentence for clients that only
    show it; dirty_files lets the Portal and Android ask to stash and retry."""
    return JSONResponse(status_code=409, content={
        "detail": dirty_tree_message(dirty, action),
        "refused": "dirty_tree",
        "dirty_files": dirty,
    })


# ── GET /update/status ──────────────────────────────────────────────────

@router.get("/status")
def update_status() -> dict:
    """Return current SHA, latest SHA, commits behind, changed files,
    categories, and "in progress" mutex state.

    Uses a 60s cache for the git-fetch result to avoid GitHub rate limit
    when multiple Portal tabs auto-poll. The "Check for updates" button
    (POST /update/preflight) busts the cache.
    """
    if not git_ops.is_initialized(_ROOT):
        return {
            "git_initialized": False,
            "in_progress": _MGR.is_locked(),
            "last_state": _MGR.read_state(),
            "message": "$BLACKBOX_ROOT is not a git checkout. "
                       "Run install.sh Step 0a (or use 'Initialize updates' button).",
        }

    global _STATUS_CACHE, _STATUS_CACHE_TS
    now = time.time()
    age = now - _STATUS_CACHE_TS

    if _STATUS_CACHE and age < _STATUS_CACHE_TTL_S:
        # Augment cached data with live mutex state (cheap; no git call).
        cached = dict(_STATUS_CACHE)
        cached["in_progress"] = _MGR.is_locked()
        cached["last_state"] = _MGR.read_state()
        cached["last_fetch_age_s"] = int(age)
        cached["dirty_files"] = _dirty_files(_ROOT)  # local, cheap: never cached
        return cached

    return _compute_status_uncached(_ROOT)


def _compute_status_uncached(root: Path) -> dict:
    """Do the actual git-fetch + categorize work. Updates the cache."""
    global _STATUS_CACHE, _STATUS_CACHE_TS
    try:
        verify.check_origin(root)
    except verify.UpdateRefused as e:
        # Never fetched from: Install would be refused for the same reason.
        return {
            "git_initialized": True,
            "fetch_error": str(e),
            "refused": "unverified_update",
            "in_progress": _MGR.is_locked(),
            "last_state": _MGR.read_state(),
        }
    try:
        # Main as the origin reports it, asked before the fetch brings it.
        latest = git_ops.latest_origin_sha(root)
        git_ops.fetch_origin_main(root)
    except subprocess.CalledProcessError as e:
        return {
            "git_initialized": True,
            "fetch_error": e.stderr.strip()[:300] or "git fetch failed",
            "in_progress": _MGR.is_locked(),
            "last_state": _MGR.read_state(),
        }
    except subprocess.TimeoutExpired:
        return {
            "git_initialized": True,
            "fetch_error": "github.com unreachable (timeout)",
            "in_progress": _MGR.is_locked(),
            "last_state": _MGR.read_state(),
        }

    if not git_ops.has_commit(root, latest):
        return {
            "git_initialized": True,
            "fetch_error": f"the fetch did not bring {latest[:7]} (origin/main moved "
                           f"while checking); check again",
            "in_progress": _MGR.is_locked(),
            "last_state": _MGR.read_state(),
        }
    current = git_ops.current_sha(root)
    current_short = git_ops.current_short(root)
    latest_short = latest[:7]
    behind = git_ops.commits_behind(root, target=latest)
    ahead = git_ops.commits_ahead(root, target=latest)

    commits = []
    changed_files: list[str] = []
    categories: dict = changes_mod.categorize([])
    if behind > 0:
        commits = git_ops.commits_between(root, current, latest)
        changed_files = git_ops.diff_files(root, current, latest)
        categories = changes_mod.categorize(changed_files)

    result = {
        "git_initialized": True,
        "current_sha": current,
        "current_short": current_short,
        "latest_sha": latest,
        "latest_short": latest_short,
        "commits_behind": behind,
        "commits_ahead": ahead,
        "commits": commits,
        "changed_files": changed_files,
        "categories": categories,
        "in_progress": _MGR.is_locked(),
        "last_state": _MGR.read_state(),
        "last_fetch_age_s": 0,
        "active_cli_sessions": count_active_cli_sessions(),
        # Tracked files an Install or Rollback would overwrite (O16-01).
        "dirty_files": _dirty_files(root),
    }
    _STATUS_CACHE = dict(result)
    _STATUS_CACHE_TS = time.time()
    return result


# ── POST /update/preflight ──────────────────────────────────────────────

@router.post("/preflight")
def update_preflight() -> dict:
    """Force a fresh git-fetch + return current status. Used by the
    "Check for updates" button. Bypasses the /status cache.

    Also clears any persisted FAILED state from a prior attempt — the
    user clicking Check is implicit acknowledgement that they want to
    retry. Without this, a one-time failure (sudo denied, network
    blip, etc.) leaves update_state.json with phase=failed, which the
    Portal UI uses to gate the Install button. Customer would need
    SSH to manually rm the state file. Auto-clearing on Check breaks
    that trap — they hit Check, the stale failed state goes away, the
    Install button re-enables, retry proceeds. Brandon hit this on
    MSO2 2026-05-22 when install.sh re-run failed sudo.
    `clear_failed_state()` is idempotent + only touches `failed`
    phase (complete/in-progress phases untouched).
    """
    global _STATUS_CACHE, _STATUS_CACHE_TS
    _STATUS_CACHE = {}
    _STATUS_CACHE_TS = 0.0
    if not git_ops.is_initialized(_ROOT):
        return {"ok": False, "git_initialized": False,
                "message": "Run install.sh first to initialize git."}
    cleared_failed = _MGR.clear_failed_state()
    status = _compute_status_uncached(_ROOT)
    return {
        "ok": "fetch_error" not in status,
        **status,
        "active_sessions": list_active_cli_sessions(),
        "cleared_failed_state": cleared_failed,
    }


# ── POST /update/start ──────────────────────────────────────────────────

# In-process registry of running update tasks. Keyed by task_id. Values
# are the asyncio Queue that the runner writes events into and the SSE
# stream reads from. Survives only the lifetime of the uvicorn process —
# if the service restarts mid-update, the persisted update_state.json
# is the canonical source of truth (manager.is_interrupted() detects it).
_TASKS: dict[str, asyncio.Queue] = {}
_RUNNING: set[asyncio.Task] = set()


@router.post("/start")
async def update_start(req: StartRequest):
    """Begin an update. Returns task_id; client opens /update/log/stream
    with that task_id to receive SSE events."""
    if not git_ops.is_initialized(_ROOT):
        raise HTTPException(400, "Repository not initialized. Use POST /update/preflight first.")

    if _MGR.is_locked():
        raise HTTPException(409, "Another update is already in progress.")

    # Refuse a dirty tree up front so the client can show the files and ask.
    # The runner re-checks under the lock; this answer is for the UI.
    dirty = await asyncio.to_thread(_dirty_files, _ROOT)
    if dirty and not req.discard_local_changes:
        return _dirty_refusal(dirty, "Installing the update")

    # confirm_sha race-check: if origin moved since UI fetched status,
    # refuse so user re-reads the (now different) changelog. Asked of the
    # pinned origin itself (an unpinned one is never contacted: the runner
    # refuses it with the reason).
    latest = ""
    if req.confirm_sha:
        try:
            await asyncio.to_thread(verify.check_origin, _ROOT)
            latest = await asyncio.to_thread(git_ops.latest_origin_sha, _ROOT)
        except (verify.UpdateRefused, subprocess.CalledProcessError,
                subprocess.TimeoutExpired, OSError):
            pass  # fall through — runner will fail with clearer error
        if latest and latest != req.confirm_sha:
            raise HTTPException(
                409,
                f"origin/main moved since you confirmed. "
                f"Expected {req.confirm_sha[:7]}, got {latest[:7]}. "
                f"Re-check updates and try again.",
            )

    runner = UpdateRunner(_ROOT, _MGR, discard_local_changes=req.discard_local_changes)
    _spawn(runner.task_id, runner.run(), "Update runner")
    return {"task_id": runner.task_id, "status": "started"}


def _spawn(task_id: str, events, label: str) -> None:
    """Drive a runner's event generator as a background task. The
    /log/stream endpoint tails the queue it writes into."""
    queue: asyncio.Queue = asyncio.Queue()
    _TASKS[task_id] = queue

    async def drive():
        try:
            async for event in events:
                await queue.put(event)
        except Exception as e:
            logger.exception("%s crashed", label)
            await queue.put({"type": "complete", "succeeded": False,
                             "error": f"{label.lower()} crashed: {e}",
                             "restart_scheduled": False})
        finally:
            # Sentinel for the SSE stream to detect end-of-stream.
            await queue.put(None)

    # Hold a reference: the loop keeps only a weak one to a running task.
    task = asyncio.create_task(drive())
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)


# ── GET /update/log/stream ──────────────────────────────────────────────

@router.get("/log/stream")
async def update_log_stream(task_id: str):
    """SSE stream of phase events + log lines for a running update.
    Polled by the Portal's Updates panel modal. Closes when the runner
    emits the 'complete' event sentinel."""
    if task_id not in _TASKS:
        raise HTTPException(404, f"task_id {task_id} not found")
    queue = _TASKS[task_id]

    async def event_source():
        last_heartbeat = time.time()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    # Heartbeat keeps the connection alive across long
                    # subprocess phases (pip install can take minutes).
                    now = time.time()
                    yield f"data: {json.dumps({'type': 'heartbeat', 'iso': _now_iso()})}\n\n"
                    last_heartbeat = now
                    continue
                if event is None:
                    # End-of-stream sentinel
                    break
                yield f"data: {json.dumps(event)}\n\n"
                # If this was the complete event, we're done — but emit
                # one final flush to make sure the browser receives it.
                if event.get("type") == "complete":
                    break
        finally:
            _TASKS.pop(task_id, None)

    return StreamingResponse(event_source(), media_type="text/event-stream")


# ── POST /update/rollback ───────────────────────────────────────────────

@router.post("/rollback")
async def update_rollback(req: Optional[RollbackRequest] = Body(default=None)):
    """Revert the venvs and the code to the most recent pre-update-<ts> tag
    and schedule a service restart. Used when an update succeeded but the
    customer sees a regression.

    Runs as a task under the update lock, like an install (O16-08): the reply
    carries task_id for /update/log/stream, whose complete event says whether
    the restart is coming. reverted_to and restart_scheduled stay for clients
    that predate the stream; the restart follows only a successful rollback."""
    discard = bool(req and req.discard_local_changes)
    state = _MGR.read_state() or {}
    tag = state.get("pre_update_tag") or ""
    # The tag must name a real tag: a failed fetch persists "" and a reset to
    # anything else (or to an option-shaped name) must never run.
    if not tag or not await asyncio.to_thread(git_ops.tag_commit, _ROOT, tag):
        raise HTTPException(404, "No prior update found to roll back.")
    if _MGR.is_locked():
        raise HTTPException(409, "An update or rollback is already in progress.")
    dirty = await asyncio.to_thread(_dirty_files, _ROOT)
    if dirty and not discard:
        return _dirty_refusal(dirty, "Rolling back")

    runner = UpdateRunner(_ROOT, _MGR, discard_local_changes=discard, kind="rollback")
    _spawn(runner.task_id, runner.rollback(tag), "Rollback")
    return {"task_id": runner.task_id, "status": "started", "reverted_to": tag,
            "restart_scheduled": True}


# ── GET /update/state ───────────────────────────────────────────────────

@router.get("/state")
def update_state() -> dict:
    """Raw persisted state from Manifest/update_state.json. Used by the
    startup banner to detect interrupted updates."""
    state = _MGR.read_state()
    return {
        "has_state": state is not None,
        "state": state,
        "is_interrupted": _MGR.is_interrupted(),
        "in_progress": _MGR.is_locked(),
    }


# ── Helpers ─────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
