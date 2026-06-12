"""Migration job — diff-and-fill + atomic cutover (Task 8).

"Switch models" and "backfill after switching back" are the same operation:
diff the target store's ids against the snapshot index, embed what's missing
(text read from the volume by byte offset), append, repeat until the diff is
empty (catch-up loop absorbs mints that land during the job), then cut over.

Cutover is IN-PROCESS and two-part, in this order:
  1. store.set_active_slug(target)   — persists the pointer (active.json)
  2. search.swap_active(target)      — repoints live searches in memory
An out-of-process pointer flip would never be observed by the orchestrator —
search.py holds the active store handle in module state.

Known race (documented, detected, not prevented): a mint that embeds under the
OLD model just before cutover can append into the TARGET store just after the
in-memory swap when the dims match (e.g. 3072↔3072). The vector is wrong-space
but missing() will never flag it. We detect it: everything this job wrote is
tracked (job_appended), the target's preexisting ids are snapshotted at job
start, and at cutover `raced = target.ids() - preexisting - job_appended` is
computed and loudly logged. Full prevention needs an upsert API — out of scope.

Failure containment: a batch that exhausts the provider's own retries (4
attempts inside providers.py) is quarantined for THIS RUN — its snap_ids go to
`skipped`, the job keeps going and still completes. Quarantined ids remain
missing() in the store, so a later run (re-POST, watcher gap-heal, resume)
retries them. A permanently-failing text can therefore never stall the job —
with one guard: if EVERY missing snapshot was quarantined and the job appended
NOTHING (dead provider: revoked key, daemon down), it stalls instead of
cutting over to a near-empty store. Any NON-provider exception parks the job
in `stalled` with the error recorded; re-POSTing resumes via the diff
(progress truth = store contents).

One job at a time, module-level singleton (CU session-manager pattern).
State is persisted to {stores_dir}/migration_state.json every PERSIST_EVERY
appends and on every state transition — resume metadata only; on boot,
resume_if_interrupted() relaunches a job whose persisted state says "running".
"""
import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from Orchestrator import config
from Orchestrator.embeddings import search
from Orchestrator.embeddings.providers import EmbeddingProviderError, get_provider
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import _atomic_write_json, get_store, set_active_slug
from Orchestrator.volume import read_volume_bytes

STATE_FILE = "migration_state.json"
BATCH_SIZE = 8          # texts per provider.embed call
PERSIST_EVERY = 25      # appends between migration_state.json writes
BATCH_SLEEP_S = 0.2     # cloud rate-limit pause between batches

# ── singleton job state ──────────────────────────────────────────────────────

_JOB: dict | None = None          # None = idle / never run this process
_JOB_LOCK = threading.Lock()      # guards _JOB mutation + reads (copy out)
_CANCEL = threading.Event()       # cooperative cancel, checked between batches
_JOB_TASK: "asyncio.Task | None" = None   # strong ref to the scheduled engine task


def _state_path() -> Path:
    return Path(config.EMBEDDINGS_STORES_DIR) / STATE_FILE


def get_job_status() -> dict | None:
    """Copy of the live job dict (the /embeddings/status `job` field); None when idle."""
    with _JOB_LOCK:
        if _JOB is None:
            return None
        snap = dict(_JOB)
        snap["skipped"] = list(snap["skipped"])
        snap["raced"] = list(snap["raced"])
        # Computed, never persisted: lets the wizard show "cancelling —
        # finishing current batch" between the cancel POST and the transition.
        snap["cancel_requested"] = _CANCEL.is_set()
        return snap


def _persist_locked() -> None:
    """Write the current job dict to migration_state.json (atomic). Caller holds _JOB_LOCK."""
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(path, _JOB)
    except OSError as e:
        # Persistence is resume METADATA only (real truth = store contents);
        # a disk hiccup must not kill a multi-minute embed run.
        print(f"[MIGRATE] could not persist migration state: {e}")


def _update_job(persist: bool = False, **fields) -> None:
    with _JOB_LOCK:
        _JOB.update(fields)
        if persist:
            _persist_locked()


def _advance_done(n: int, persist: bool) -> None:
    with _JOB_LOCK:
        _JOB["done"] += n
        if persist:
            _persist_locked()


def _set_total_from_missing(n_missing: int) -> None:
    with _JOB_LOCK:
        _JOB["total"] = _JOB["done"] + n_missing
        _persist_locked()


def _finish_job(state: str, **fields) -> None:
    _update_job(
        persist=True,
        state=state,
        finished_at=datetime.now(timezone.utc).isoformat(),
        **fields,
    )
    print(f"[MIGRATE] job finished: state={state}")


def _begin_job(target_slug: str) -> None:
    """Claim the singleton: RuntimeError if a job is running, else fresh state."""
    global _JOB
    with _JOB_LOCK:
        if _JOB is not None and _JOB["state"] == "running":
            raise RuntimeError(
                f"a migration to {_JOB['target']!r} is already running"
            )
        _CANCEL.clear()
        _JOB = {
            "target": target_slug,
            "state": "running",
            "done": 0,
            "total": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "error": None,
            "skipped": [],
            "raced": [],
        }
        _persist_locked()


def _log_engine_task_outcome(task: "asyncio.Task") -> None:
    """Done-callback on the engine task: a death the engine's own exception
    handling never saw (so the job dict still says "running") would otherwise
    be silent — retrieve the exception and make it a loud journal line."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print(
            f"[MIGRATE] ERROR: engine task died with unretrieved exception: "
            f"{type(exc).__name__}: {exc}"
        )


def _launch(target_slug: str) -> "asyncio.Task":
    """Schedule the engine on the running loop and RETAIN the Task.

    The event loop holds only WEAK references to tasks: an engine task nobody
    references can be garbage-collected mid-run, dying silently and leaving
    _JOB stuck "running" (permanent 409 until restart). _JOB_TASK is the
    strong reference; both launch sites (route POST + startup resume) route
    through here. Claims nothing — caller runs _begin_job first.
    """
    global _JOB_TASK
    task = asyncio.get_running_loop().create_task(_run_engine(target_slug))
    task.add_done_callback(_log_engine_task_outcome)
    _JOB_TASK = task
    return task


# ── public API ───────────────────────────────────────────────────────────────

async def start_migration(target_slug: str) -> dict:
    """Route entry: claim the job, schedule the engine, return the job dict.

    Raises ValueError on an unknown slug (route → 404) and RuntimeError when a
    job is already running (route → 409). The claim happens synchronously
    BEFORE create_task, so a second POST racing this one can never double-start.
    """
    if target_slug not in EMBEDDING_MODELS:
        raise ValueError(
            f"unknown embedding model slug {target_slug!r}; "
            f"known: {sorted(EMBEDDING_MODELS)}"
        )
    _begin_job(target_slug)
    _launch(target_slug)
    return get_job_status()


async def run_migration(target_slug: str) -> dict:
    """Run a migration to completion in this coroutine (resume + CLI entry).

    Same claim semantics as start_migration; returns the final job dict.
    """
    if target_slug not in EMBEDDING_MODELS:
        raise ValueError(
            f"unknown embedding model slug {target_slug!r}; "
            f"known: {sorted(EMBEDDING_MODELS)}"
        )
    _begin_job(target_slug)
    return await _run_engine(target_slug)


def request_cancel() -> bool:
    """Set the cooperative cancel flag; False (no-op) when no job is running."""
    with _JOB_LOCK:
        if _JOB is None or _JOB["state"] != "running":
            return False
        _CANCEL.set()
        return True


def resume_if_interrupted() -> "asyncio.Task | None":
    """Relaunch a migration that a restart interrupted (startup hook entry).

    If migration_state.json says state=="running", the process died mid-job:
    re-diff-and-fill is safe by construction (store contents are the resume
    truth), so claim the job and schedule the engine on the running loop via
    _launch (which retains the Task). Must be called with an event loop
    running (async startup hook).
    """
    try:
        persisted = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(persisted, dict) or persisted.get("state") != "running":
        return None
    target = persisted.get("target")
    if target not in EMBEDDING_MODELS:
        print(f"[MIGRATE] interrupted job targets unknown slug {target!r}; not resuming")
        return None
    print(f"[MIGRATE] resuming interrupted migration to {target}")
    _begin_job(target)
    return _launch(target)


# ── engine ───────────────────────────────────────────────────────────────────

async def _run_engine(target_slug: str) -> dict:
    """The diff-and-fill loop. Caller has already claimed the job via _begin_job."""
    from Orchestrator.fossils import load_snapshot_index  # lazy: avoid import cycle

    try:
        target = get_store(target_slug)
        provider = get_provider(target_slug)
        preexisting_ids = target.ids()      # raced-detection baseline
        job_appended: set[str] = set()      # everything THIS job wrote
        quarantined: set[str] = set()       # skipped for THIS RUN only
        appends_since_persist = 0

        while True:
            # Re-diff each pass: new mints land in the index (and the active
            # store) during the job — the loop converges when nothing is missing.
            # Off the loop: the cold parse reads + json-loads the whole index
            # (already called from worker threads elsewhere).
            index = await asyncio.to_thread(load_snapshot_index)
            missing = [
                sid for sid in target.missing(list(index.keys()))
                if sid not in quarantined
            ]
            if not missing:
                break
            _set_total_from_missing(len(missing))

            # Volume bytes are read ONCE per pass (~35MB) and sliced per
            # snapshot — off the loop, a 35MB disk read blocks every request.
            vol_bytes = await asyncio.to_thread(
                read_volume_bytes, Path(config.VOL_PATH)
            )

            for i in range(0, len(missing), BATCH_SIZE):
                if _CANCEL.is_set():
                    _finish_job("cancelled")
                    return get_job_status()

                batch_ids = missing[i:i + BATCH_SIZE]
                texts, good_ids = [], []
                for sid in batch_ids:
                    entry = index.get(sid, {})
                    bs = entry.get("byte_start", 0)
                    be = entry.get("byte_end", 0)
                    if bs >= len(vol_bytes) or be > len(vol_bytes) or be <= bs:
                        print(
                            f"[MIGRATE] {sid}: invalid byte range ({bs},{be}) "
                            f"for {len(vol_bytes)}-byte volume — skipping this run"
                        )
                        quarantined.add(sid)
                        _quarantine_ids([sid])
                        continue
                    texts.append(vol_bytes[bs:be].decode("utf-8", errors="replace"))
                    good_ids.append(sid)

                if good_ids:
                    try:
                        vectors = await provider.embed(texts, "document")
                    except EmbeddingProviderError as e:
                        # Provider already retried 4x with backoff — quarantine
                        # this batch for the run and keep moving (constraint 3).
                        print(
                            f"[MIGRATE] batch failed after provider retries, "
                            f"quarantining {len(good_ids)} snapshot(s) for this "
                            f"run: {good_ids}: {e}"
                        )
                        quarantined.update(good_ids)
                        _quarantine_ids(good_ids)
                        await asyncio.sleep(BATCH_SLEEP_S)
                        continue

                    # Track precisely what WE write: an id appended by a racing
                    # mint between the diff and here is skipped by append_many's
                    # idempotency and must NOT be credited to this job (it feeds
                    # the raced computation at cutover).
                    already_present = target.ids()
                    rows = [
                        (sid, vec)
                        for sid, vec in zip(good_ids, vectors)
                        if sid not in already_present
                    ]
                    # fsync-heavy store writes off the loop — this loop also
                    # serves voice/WS traffic (store is thread-safe by design)
                    await asyncio.to_thread(target.append_many, rows)
                    job_appended.update(sid for sid, _ in rows)
                    appends_since_persist += len(rows)
                    persist_now = appends_since_persist >= PERSIST_EVERY
                    _advance_done(len(good_ids), persist=persist_now)
                    if persist_now:
                        appends_since_persist = 0

                await asyncio.sleep(BATCH_SLEEP_S)

        # ── cutover guard: dead provider must not activate an empty store ────
        # Every batch quarantined and nothing appended (revoked key, daemon
        # down): the empty diff is failure, not completion. Cutting over would
        # swap live searches onto a near-empty store — and boot auto-resume
        # would do it unattended. A pure switch-back where everything was
        # already present (quarantined empty too) is the fast path and still
        # cuts over below.
        if not job_appended and quarantined:
            msg = (
                f"provider failed for all {len(quarantined)} snapshots; "
                f"cutover aborted - active store unchanged"
            )
            print(f"[MIGRATE] ERROR: {msg}")
            _finish_job("stalled", error=msg)
            return get_job_status()

        # ── cutover (in-process, both parts, this order) ─────────────────────
        set_active_slug(target_slug)        # 1. persist the pointer (disk)
        search.swap_active(target_slug)     # 2. repoint live searches (memory)

        raced = sorted(target.ids() - preexisting_ids - job_appended)
        if raced:
            print(
                f"[MIGRATE] WARNING: {len(raced)} snapshot(s) raced the cutover "
                f"with old-model vectors: {raced} — their search ranking may be "
                f"slightly off until re-embedded"
            )
        _toolvault_cutover_hook(target_slug)
        _finish_job("done", raced=raced)
        print(f"[MIGRATE] cutover complete: active model is now {target_slug}")
        return get_job_status()

    except asyncio.CancelledError:
        # Loop teardown (shutdown/restart), NOT an operator cancel: leave the
        # persisted state as "running" so resume_if_interrupted relaunches.
        raise
    except Exception as e:  # noqa: BLE001 — park, surface, stay resumable
        print(f"[MIGRATE] job stalled: {type(e).__name__}: {e}")
        _finish_job("stalled", error=str(e))
        return get_job_status()


def _quarantine_ids(snap_ids: list[str]) -> None:
    """Record run-skipped ids in job state (visible in status; persisted)."""
    with _JOB_LOCK:
        _JOB["skipped"].extend(snap_ids)
        _persist_locked()


def _toolvault_cutover_hook(target_slug: str) -> None:
    """Re-embed ToolVault descriptions under the new model — Task 11 wires this."""
    print("[MIGRATE] toolvault re-embed hook (Task 11 wires this)")
