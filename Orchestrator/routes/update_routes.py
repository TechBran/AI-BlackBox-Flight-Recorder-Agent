"""Update pipeline backend routes (T7).

Mounted at /update/* by Orchestrator/app.py.

Endpoints:
  GET  /update/status            cached git state + commits-behind + categories
  POST /update/preflight         force fresh git fetch + active-session check
  POST /update/start             begin update (returns task_id)
  GET  /update/log/stream        SSE stream of phase events + log lines
  POST /update/rollback          revert to last pre-update-<ts> tag + restart
  GET  /update/state             persisted Manifest/update_state.json (recovery)

DESIGN NOTES:
  - Single-process mutex via UpdateManager.acquire_or_raise (flock).
  - /update/status caches the git_ops.fetch result for 60s to avoid
    GitHub rate-limiting (audit M7) when multiple Portal tabs auto-poll.
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

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from Orchestrator.update import changes as changes_mod
from Orchestrator.update import git_ops
from Orchestrator.update.active_sessions import (
    count_active_cli_sessions, list_active_cli_sessions,
)
from Orchestrator.update.manager import (
    UpdateManager, UpdateInProgressError, TERMINAL_PHASES,
)
from Orchestrator.update.runner import UpdateRunner
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
        return cached

    return _compute_status_uncached(_ROOT)


def _compute_status_uncached(root: Path) -> dict:
    """Do the actual git-fetch + categorize work. Updates the cache."""
    global _STATUS_CACHE, _STATUS_CACHE_TS
    try:
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

    current = git_ops.current_sha(root)
    current_short = git_ops.current_short(root)
    latest = git_ops.latest_origin_sha(root)
    latest_short = latest[:7]
    behind = git_ops.commits_behind(root)
    ahead = git_ops.commits_ahead(root)

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


@router.post("/start")
async def update_start(req: StartRequest) -> dict:
    """Begin an update. Returns task_id; client opens /update/log/stream
    with that task_id to receive SSE events."""
    if not git_ops.is_initialized(_ROOT):
        raise HTTPException(400, "Repository not initialized. Use POST /update/preflight first.")

    if _MGR.is_locked():
        raise HTTPException(409, "Another update is already in progress.")

    # confirm_sha race-check: if origin moved since UI fetched status,
    # refuse so user re-reads the (now different) changelog.
    if req.confirm_sha:
        try:
            git_ops.fetch_origin_main(_ROOT)
        except subprocess.CalledProcessError:
            pass  # fall through — runner will fail with clearer error
        latest = git_ops.latest_origin_sha(_ROOT)
        if latest != req.confirm_sha:
            raise HTTPException(
                409,
                f"origin/main moved since you confirmed. "
                f"Expected {req.confirm_sha[:7]}, got {latest[:7]}. "
                f"Re-check updates and try again.",
            )

    # Spawn the runner as a background task. The /log/stream endpoint
    # tails the queue it writes into.
    runner = UpdateRunner(_ROOT, _MGR)
    task_id = runner.task_id
    queue: asyncio.Queue = asyncio.Queue()
    _TASKS[task_id] = queue

    async def drive():
        try:
            async for event in runner.run():
                await queue.put(event)
        except Exception as e:
            logger.exception("Update runner crashed")
            await queue.put({"type": "complete", "succeeded": False,
                             "error": f"runner crashed: {e}"})
        finally:
            # Sentinel for the SSE stream to detect end-of-stream.
            await queue.put(None)

    asyncio.create_task(drive())
    return {"task_id": task_id, "status": "started"}


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
async def update_rollback() -> dict:
    """Revert code to the most recent pre-update-<ts> tag and schedule
    a service restart. Used when an update succeeded but the customer
    sees a regression."""
    state = _MGR.read_state()
    if not state or "pre_update_tag" not in state:
        raise HTTPException(404, "No prior update found to roll back.")
    tag = state["pre_update_tag"]
    try:
        git_ops.reset_hard(_ROOT, tag)
    except subprocess.CalledProcessError as e:
        raise HTTPException(500, f"git reset --hard {tag} failed: {e.stderr[:300]}")
    _MGR.write_state(
        task_id=f"rollback-{int(time.time())}", phase="rolled_back",
        target_sha=git_ops.current_sha(_ROOT), from_sha=state.get("target_sha", ""),
        pre_update_tag=tag,
    )
    # Schedule detached restart (same pattern as runner.py)
    loop = asyncio.get_running_loop()
    loop.call_later(2.0, _fire_detached_restart)
    return {"reverted_to": tag, "current_sha": git_ops.current_short(_ROOT),
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

def _fire_detached_restart() -> None:
    """Spawn `sudo systemctl restart blackbox.service` as detached process.
    Mirrors the pattern in onboarding_routes.py:511 + update/runner.py."""
    subprocess.Popen(
        ["sudo", "-n", "systemctl", "restart", "blackbox.service"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
