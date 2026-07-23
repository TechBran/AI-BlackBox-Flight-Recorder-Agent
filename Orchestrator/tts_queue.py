#!/usr/bin/env python3
"""Async on-box TTS queue (B1, 2026-07-22) — the permanent transport fix for
multi-minute Qwen3-TTS jobs.

POST /tts/queue returns a task_id IMMEDIATELY; ONE asyncio worker drains a
FIFO so a long reply can never tie a synchronous HTTP request to whichever
client timeout is tightest (server chunk timeout -> GPU 429 pileup -> OkHttp
readTimeout — the three layers whack-a-moled before this). On-box (qwen)
only: cloud providers stay synchronous and byte-for-byte untouched.

Each job: sanitize_for_speech -> chunk_text_for_tts (300-char chunks on the
native-batch path, mirroring /tts/batch) -> the chunks go to the member in
SUB-BATCHES of QWEN_TTS_MAX_BATCH (8) via qwen_tts.synthesize_batch — the
member caps at 8 internally anyway, so orchestrator-side sub-batching costs
nothing in perf and buys a progress tick (subbatch/subbatches_total) per
sub-batch plus a cooperative-cancel checkpoint between sub-batches ->
stitch_wav_chunks -> saved under UPLOADS_DIR (same lifecycle as the Gemini
TTS task audio: `{task_id}.wav` -> /ui/uploads/{task_id}.wav).

SERIALIZATION UNIFICATION (audit): QWEN_SYNTH_LOCK below is THE single-flight
lock for every on-box synth path — this worker, /tts/batch's qwen branch, and
the /tts single-shot qwen branch (via run_locked_sync, since that route is a
sync-def running in the threadpool). No on-box synth may bypass it: the
qwen-tts member is one GPU; overlapping submissions only contend and 429.

Reliability: a job auto-retries ONCE on transient failure (RuntimeError /
timeout / 429-exhausted); failed jobs keep {error, retryable} and can be
requeued via POST /tts/task/{id}/retry; POST /tts/task/{id}/cancel uses the
tasks.py cooperative-cancel flag, checked between sub-batches.

V1 PERSISTENCE: jobs and results are IN-MEMORY (the WAV itself is on disk).
A service restart DROPS the queue — queued/generating jobs vanish and their
task_ids 404; clients re-submit. Accepted for v1 (the Portal/Android pollers
surface the 404 as a retryable state).

Fail-open when the stack is off: the submit route 503s cleanly (see
tts_routes) and nothing here ever wakes the GPU on its own.
"""
from __future__ import annotations

import asyncio
import io
import os
import time
import uuid
import wave
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# The single-flight lock for EVERY on-box (qwen) synth path. tts_routes
# imports this as its `_qwen_batch_lock`; the worker below acquires it per
# sub-batch (so an interactive /tts/batch can interleave between a queued
# job's sub-batches instead of starving for the whole job).
# ---------------------------------------------------------------------------
QWEN_SYNTH_LOCK = asyncio.Lock()

# ETA model: est audio seconds = chars/15 (natural speech ~15 chars/s), synth
# time = est_audio_s * 0.2 (measured warm RTF 0.178 on the RTX 2000 Ada with
# A2/A3 landed, rounded up).
_CHARS_PER_AUDIO_SECOND = 15.0
_MEASURED_WARM_RTF = 0.45  # c1000 chunks measure RTF 0.405 (consistency eval 2026-07-23); rounded up

_TERMINAL = ("done", "failed", "cancelled")

# module state (in-memory v1 — see module docstring)
_jobs: Dict[str, Dict[str, Any]] = {}
_pending: Optional[asyncio.Queue] = None
_worker_task: Optional[asyncio.Task] = None
_worker_loop: Optional[asyncio.AbstractEventLoop] = None
_main_loop: Optional[asyncio.AbstractEventLoop] = None
_seq = 0


def est_synth_seconds(chars: int) -> float:
    """Whole-job synth estimate for the ETA fields."""
    return (max(0, chars) / _CHARS_PER_AUDIO_SECOND) * _MEASURED_WARM_RTF


def _sub_batch_size() -> int:
    """Orchestrator-side sub-batch cap. Mirrors the member's
    settings.max_batch (QWEN_TTS_MAX_BATCH, default 8) — the member splits at
    8 internally anyway, so matching it here is free and gives us the
    per-sub-batch progress tick + cancel checkpoint."""
    try:
        return max(1, int(os.environ.get("QWEN_TTS_MAX_BATCH", "8")))
    except ValueError:
        return 8


def _uploads_dir():
    """Seam for tests; prod = Orchestrator.config.UPLOADS_DIR (Portal/uploads,
    same place the Gemini TTS task saves — served at /ui/uploads/…)."""
    from Orchestrator.config import UPLOADS_DIR
    return UPLOADS_DIR


# --- tasks.py cooperative-cancel plumbing (lazy imports keep this module
# import-light and cycle-safe; tasks.py is fully loaded by app startup) ------
def _is_cancel_requested(task_id: str) -> bool:
    from Orchestrator.tasks import is_cancel_requested
    return is_cancel_requested(task_id)


def _request_cancel(task_id: str) -> None:
    from Orchestrator.tasks import request_cooperative_cancel
    request_cooperative_cancel(task_id)


def _clear_cancel(task_id: str) -> None:
    from Orchestrator.tasks import clear_cancel_request
    clear_cancel_request(task_id)


def _register_handle(task_id: str) -> None:
    from Orchestrator.tasks import register_cancel_handle
    register_cancel_handle(task_id, "tts_queue")


def _unregister_handle(task_id: str) -> None:
    from Orchestrator.tasks import unregister_cancel_handle
    unregister_cancel_handle(task_id)


def _is_transient(exc: BaseException) -> bool:
    """Transient = worth ONE automatic retry: RuntimeError (incl. the
    429-exhausted RuntimeError synthesize_batch raises), any requests
    transport error, and timeouts. ValueError et al. (bad input) are not."""
    import requests as _requests
    return isinstance(exc, (RuntimeError, TimeoutError, asyncio.TimeoutError,
                            _requests.exceptions.RequestException))


def _wav_seconds(wav_bytes: bytes) -> float:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            rate = wf.getframerate() or 1
            return round(wf.getnframes() / float(rate), 2)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# loop capture + sync-path lock helper (the /tts single-shot qwen branch)
# ---------------------------------------------------------------------------
def capture_loop() -> None:
    """Called from the tts_routes startup hook so run_locked_sync can reach
    the app's event loop from threadpool (sync-def route) threads."""
    global _main_loop
    _main_loop = asyncio.get_running_loop()


def run_locked_sync(fn, timeout: float = 900.0):
    """Run fn() while holding QWEN_SYNTH_LOCK, from a NON-event-loop thread
    (FastAPI runs sync-def routes in the threadpool). This is how the /tts
    single-shot qwen branch stops bypassing the single-flight discipline.

    Fail-open: if no loop was captured / it is not running (unit contexts,
    early startup), fn() runs unlocked — same behavior as before B1, and the
    stack-off dev box never reaches here anyway."""
    loop = _main_loop
    if loop is None or loop.is_closed() or not loop.is_running():
        return fn()
    try:
        if asyncio.get_running_loop() is loop:  # pragma: no cover — defensive
            return fn()   # on the loop thread itself: cannot block on it
    except RuntimeError:
        pass  # normal case: we are on a threadpool thread
    fut = asyncio.run_coroutine_threadsafe(QWEN_SYNTH_LOCK.acquire(), loop)
    try:
        fut.result(timeout=timeout)
    except Exception:
        fut.cancel()
        raise
    try:
        return fn()
    finally:
        loop.call_soon_threadsafe(QWEN_SYNTH_LOCK.release)


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------
def _ensure_worker() -> None:
    """Start (or restart) the ONE worker task on the current running loop.
    Loop-aware: a fresh loop (tests; conceivable server re-init) gets a fresh
    queue with any still-queued jobs re-enqueued in seq order."""
    global _pending, _worker_task, _worker_loop, _main_loop
    loop = asyncio.get_running_loop()
    _main_loop = loop
    if (_worker_loop is loop and _worker_task is not None
            and not _worker_task.done() and _pending is not None):
        return
    _pending = asyncio.Queue()
    for job in sorted(_jobs.values(), key=lambda j: j["seq"]):
        if job["status"] == "queued":
            _pending.put_nowait(job["task_id"])
    _worker_loop = loop
    _worker_task = loop.create_task(_worker_run(_pending))


def shutdown_worker() -> None:
    """App-shutdown hook: cancel the drain task so the loop closes clean."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        _worker_task.cancel()
    _worker_task = None


async def _worker_run(pending: asyncio.Queue) -> None:
    while True:
        task_id = await pending.get()
        job = _jobs.get(task_id)
        if job is None or job["status"] != "queued":
            continue  # cancelled-while-queued / stale requeue entry
        if _is_cancel_requested(task_id):
            job["status"] = "cancelled"
            _clear_cancel(task_id)
            continue
        job["status"] = "generating"
        job["started_mono"] = time.monotonic()
        job["subbatch"] = 0
        _register_handle(task_id)
        try:
            try:
                await _run_job(job)
            except asyncio.CancelledError:
                raise
            except Exception as first_err:
                if (job["status"] == "generating" and job["auto_retries"] == 0
                        and _is_transient(first_err)
                        and not _is_cancel_requested(task_id)):
                    job["auto_retries"] = 1
                    job["subbatch"] = 0
                    print(f"[TTS QUEUE] {task_id}: transient failure "
                          f"({first_err}) — auto-retrying once")
                    await _run_job(job)
                else:
                    raise
        except asyncio.CancelledError:
            raise  # shutdown — leave the job as-is (in-memory anyway)
        except Exception as e:
            if job["status"] == "generating":
                job["status"] = "failed"
                job["error"] = str(e)[:500]
                job["retryable"] = _is_transient(e)
                print(f"[TTS QUEUE] {task_id}: FAILED ({e})")
        finally:
            _unregister_handle(task_id)
            _clear_cancel(task_id)
            if job["status"] in _TERMINAL:
                job["finished_mono"] = time.monotonic()


async def _run_job(job: Dict[str, Any]) -> None:
    """One synthesis attempt: sanitize -> chunk -> sub-batched member calls
    (single-flight lock held per sub-batch) -> stitch -> save. Sets status
    done/cancelled itself; raises on failure (worker classifies)."""
    # Lazy imports: tts_routes imports this module at its top, so importing it
    # back at module level would cycle. By worker-time both are fully loaded.
    from Orchestrator import qwen_tts
    from Orchestrator.routes.tts_routes import chunk_text_for_tts, stitch_wav_chunks
    from Orchestrator.tts_sanitize import sanitize_for_speech

    task_id = job["task_id"]
    text = sanitize_for_speech(job["text"])
    # Mirror /tts/batch's qwen chunk sizing: 1000 on the native-batch path
    # (consistency eval 2026-07-23 — c300's wide batches caused audible
    # per-chunk voice drift, cosine 0.744 vs the 0.91 same-voice ceiling;
    # c1000+t0.7+seed restores 0.892-0.924 and replies <=1000 chars become a
    # single boundary-free call), 600 sequential.
    max_chunk = 1000 if qwen_tts.native_batch_enabled() else 600
    chunks = chunk_text_for_tts(text, max_chars=max_chunk)
    if not chunks:
        raise ValueError("text produced no chunks after sanitize/split")

    size = _sub_batch_size()
    subs = [chunks[i:i + size] for i in range(0, len(chunks), size)]
    job["subbatches_total"] = len(subs)
    loop = asyncio.get_running_loop()

    wavs: List[bytes] = []
    for i, sb in enumerate(subs):
        if _is_cancel_requested(task_id):
            job["status"] = "cancelled"
            print(f"[TTS QUEUE] {task_id}: cancelled at sub-batch "
                  f"{i}/{len(subs)}")
            return
        # Same single-flight discipline as /tts/batch — per sub-batch, so an
        # interactive request can interleave between a long job's sub-batches.
        async with QWEN_SYNTH_LOCK:
            try:
                got = await loop.run_in_executor(
                    None, lambda sb=sb: qwen_tts.synthesize_batch(
                        job["voice"], sb, response_format="wav"))
            except qwen_tts.QwenBatchUnsupported as e:
                # Old member without the batch endpoint — per-chunk fallback
                # inside the same lock hold (mirrors /tts/batch).
                print(f"[TTS QUEUE] {task_id}: {e} — per-chunk fallback")
                got = []
                for ch in sb:
                    r = await loop.run_in_executor(
                        None, lambda ch=ch: qwen_tts.synthesize(
                            job["voice"], ch, response_format="wav"))
                    if r.status_code != 200:
                        raise RuntimeError(
                            f"Qwen TTS failed (HTTP {r.status_code}): "
                            f"{r.text[:200]}")
                    got.append(r.content)
        wavs.extend(got)
        job["subbatch"] = i + 1

    if _is_cancel_requested(task_id):
        job["status"] = "cancelled"
        return

    combined = stitch_wav_chunks(wavs)
    filename = f"{task_id}.wav"   # mirrors the Gemini TTS task save shape
    (_uploads_dir() / filename).write_bytes(combined)
    job["audio_url"] = f"/ui/uploads/{filename}"
    job["bytes"] = len(combined)
    job["seconds"] = _wav_seconds(combined)
    job["status"] = "done"
    print(f"[TTS QUEUE] {task_id}: done — {job['bytes']} bytes, "
          f"{job['seconds']}s audio")


# ---------------------------------------------------------------------------
# public API (the routes call these)
# ---------------------------------------------------------------------------
def submit(text: str, voice: str, operator: str = "unknown") -> Dict[str, Any]:
    """Enqueue one on-box TTS job. `voice` is the BARE token (route strips any
    qwen: prefix). Returns the job's status dict (status=queued). Must be
    called with a running event loop (i.e. from an async route)."""
    global _seq
    _ensure_worker()
    _seq += 1
    task_id = f"ttsq-{uuid.uuid4().hex[:12]}"
    _jobs[task_id] = {
        "task_id": task_id,
        "text": text,
        "voice": voice,
        "operator": operator,
        "chars": len(text),
        "status": "queued",
        "seq": _seq,
        "created": time.time(),
        "started_mono": None,
        "finished_mono": None,
        "subbatch": 0,
        "subbatches_total": 0,
        "auto_retries": 0,
        "error": None,
        "retryable": None,
        "audio_url": None,
        "seconds": None,
        "bytes": None,
    }
    _pending.put_nowait(task_id)
    print(f"[TTS QUEUE] {task_id}: queued ({len(text)} chars, voice={voice}, "
          f"operator={operator})")
    return get_status(task_id)


def _positions() -> Dict[str, int]:
    active = sorted((j for j in _jobs.values()
                     if j["status"] in ("queued", "generating")),
                    key=lambda j: j["seq"])
    return {j["task_id"]: i + 1 for i, j in enumerate(active)}


def get_status(task_id: str) -> Optional[Dict[str, Any]]:
    job = _jobs.get(task_id)
    if job is None:
        return None
    now = time.monotonic()
    if job["status"] == "generating" and job["started_mono"] is not None:
        elapsed = now - job["started_mono"]
    elif job["started_mono"] is not None and job["finished_mono"] is not None:
        elapsed = job["finished_mono"] - job["started_mono"]
    else:
        elapsed = 0.0
    est = est_synth_seconds(job["chars"])
    if job["status"] == "queued":
        eta = est
    elif job["status"] == "generating":
        eta = max(0.0, est - elapsed)
    else:
        eta = 0.0
    out = {
        "task_id": task_id,
        "status": job["status"],
        "queue_position": _positions().get(task_id, 0),
        "subbatch": job["subbatch"],
        "subbatches_total": job["subbatches_total"],
        "elapsed_s": round(elapsed, 1),
        "eta_s": round(eta, 1),
        "voice": job["voice"],
        "operator": job["operator"],
        "chars": job["chars"],
        "created": job["created"],
    }
    if job["status"] == "done":
        out["audio_url"] = job["audio_url"]
        out["seconds"] = job["seconds"]
        out["bytes"] = job["bytes"]
    if job["status"] == "failed":
        out["error"] = job["error"]
        out["retryable"] = job["retryable"]
    return out


def queue_status() -> Dict[str, Any]:
    """Whole-queue summary (Updates panel). Newest 50 jobs, active first."""
    positions = _positions()
    rows = [get_status(tid) for tid in _jobs]
    rows.sort(key=lambda r: (r["queue_position"] == 0, r["queue_position"],
                             -r["created"]))
    generating = next((r["task_id"] for r in rows if r["status"] == "generating"),
                      None)
    return {
        "status": "ok",
        "queue_length": len(positions),
        "generating": generating,
        "jobs": rows[:50],
        "note": "in-memory v1 — a service restart drops queued jobs",
    }


def retry(task_id: str) -> Optional[Dict[str, Any]]:
    """Requeue a FAILED job (fresh auto-retry budget, back of the queue).
    Returns None for unknown ids or jobs not in `failed`."""
    global _seq
    job = _jobs.get(task_id)
    if job is None or job["status"] != "failed":
        return None
    _ensure_worker()
    _seq += 1
    job.update(status="queued", seq=_seq, error=None, retryable=None,
               subbatch=0, subbatches_total=0, auto_retries=0,
               started_mono=None, finished_mono=None,
               audio_url=None, seconds=None, bytes=None)
    _pending.put_nowait(task_id)
    print(f"[TTS QUEUE] {task_id}: manually requeued")
    return get_status(task_id)


def cancel(task_id: str) -> Optional[Dict[str, Any]]:
    """Cancel a job. Queued -> cancelled immediately; generating -> sets the
    tasks.py cooperative flag (the worker stops at the next sub-batch
    boundary). Terminal jobs report already_terminal. None for unknown ids."""
    job = _jobs.get(task_id)
    if job is None:
        return None
    if job["status"] in _TERMINAL:
        return {"task_id": task_id, "cancelled": False,
                "status": job["status"], "already_terminal": True}
    if job["status"] == "queued":
        job["status"] = "cancelled"
        job["finished_mono"] = time.monotonic()
        return {"task_id": task_id, "cancelled": True, "status": "cancelled",
                "detail": "cancelled while queued"}
    _request_cancel(task_id)   # generating: cooperative, between sub-batches
    return {"task_id": task_id, "cancelled": True, "status": "generating",
            "detail": "cancel requested — stops at the next sub-batch boundary"}


def _reset_for_tests() -> None:
    """Test hook: drop all state and detach from any (possibly dead) loop."""
    global _jobs, _pending, _worker_task, _worker_loop, _main_loop, _seq
    for tid in list(_jobs):
        try:
            _clear_cancel(tid)
        except Exception:
            pass
    if (_worker_task is not None and not _worker_task.done()
            and _worker_loop is not None and not _worker_loop.is_closed()):
        try:
            _worker_loop.call_soon_threadsafe(_worker_task.cancel)
        except Exception:
            pass
    _jobs = {}
    _pending = None
    _worker_task = None
    _worker_loop = None
    _main_loop = None
    _seq = 0
