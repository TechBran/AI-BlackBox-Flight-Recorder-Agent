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

Rebuild mode (M6 task 6d, ADDITIVE — the model-switch flow above is the
watcher's recovery path and is untouched): run_rebuild(slug) builds a
schema-2 chunk store under {stores_dir}/_build/{slug} — same re-diff loop in
SNAPSHOT currency, but each snapshot's text is chunked (chunk_snapshot; a
multi-chunk snapshot additionally carries its WHOLE clamped body at ordinal
0 — the M6f iteration-2 group policy, see chunk_group_batches), texts are
FLATTENED across whole snapshots into ≤CHUNK_BATCH_CAP-chunk
provider calls (a single snapshot exceeding the cap still goes in ONE call
by itself — group alignment beats the cap), then regrouped into one
append_group per snapshot (contiguous ordinals, whole-group idempotent).
BUILD-ONLY: the rebuild path NEVER cuts over — no set_active_slug, no
search.swap_active; activation is the explicit M6f stop-service dir-swap.
The job kind ({"kind": "rebuild", "activate": false}) is PERSISTED in
migration_state.json so boot resume stays build-only. The _build parent has
no meta.json of its own and list_stores does not recurse, so candidates are
invisible to every status/list surface until the swap.
"""
import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from Orchestrator import config
from Orchestrator.embeddings import search
from Orchestrator.embeddings.chunker import chunk_snapshot
from Orchestrator.embeddings.providers import EmbeddingProviderError, get_provider
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import _atomic_write_json, get_store, set_active_slug
from Orchestrator.volume import read_volume_bytes

STATE_FILE = "migration_state.json"
BATCH_SIZE = 8          # texts per provider.embed call (model-switch engine)
PERSIST_EVERY = 25      # appends between migration_state.json writes
BATCH_SLEEP_S = 0.2     # cloud rate-limit pause between batches
CHUNK_BATCH_CAP = 32    # flattened chunks per provider.embed call (rebuild/heal)
BUILD_DIR_NAME = "_build"  # {stores_dir}/_build/{slug} — invisible to list_stores

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


def _begin_job(target_slug: str, kind: "str | None" = None) -> None:
    """Claim the singleton: RuntimeError if a job is running, else fresh state.

    kind="rebuild" marks a build-only chunk-store job: the marker (and
    activate=false) is PERSISTED so boot resume relaunches into the rebuild
    engine, never the cutover one. Model-switch jobs stay kind-less — their
    persisted dict keeps the exact pre-6d key set (compat + status contract).
    """
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
        if kind == "rebuild":
            _JOB["kind"] = "rebuild"
            _JOB["activate"] = False
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


def _launch(target_slug: str, rebuild: bool = False) -> "asyncio.Task":
    """Schedule the engine on the running loop and RETAIN the Task.

    The event loop holds only WEAK references to tasks: an engine task nobody
    references can be garbage-collected mid-run, dying silently and leaving
    _JOB stuck "running" (permanent 409 until restart). _JOB_TASK is the
    strong reference; both launch sites (route POST + startup resume) route
    through here. Claims nothing — caller runs _begin_job first.
    rebuild=True schedules the build-only chunk-rebuild engine instead of the
    cutover one (the persisted job kind decides at the resume site).
    """
    global _JOB_TASK
    engine = _run_rebuild_engine if rebuild else _run_engine
    task = asyncio.get_running_loop().create_task(engine(target_slug))
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


async def start_rebuild(target_slug: str) -> dict:
    """Route entry for the IN-SERVICE chunk rebuild (M6f build step).

    Mirror of start_migration: claim (kind="rebuild"), schedule the
    BUILD-ONLY engine on the running loop, return the job dict. Running it
    inside the service keeps exactly ONE writer on the box while mints
    continue against the active v1 store (the re-diff loop absorbs them into
    the candidate). Same ValueError→404 / RuntimeError→409 semantics; the
    claim happens synchronously BEFORE create_task, so a racing second POST
    can never double-start.
    """
    if target_slug not in EMBEDDING_MODELS:
        raise ValueError(
            f"unknown embedding model slug {target_slug!r}; "
            f"known: {sorted(EMBEDDING_MODELS)}"
        )
    _begin_job(target_slug, kind="rebuild")
    _launch(target_slug, rebuild=True)
    return get_job_status()


async def run_rebuild(target_slug: str) -> dict:
    """Run a chunk-store rebuild to completion in this coroutine (CLI + 6f).

    Builds a schema-2 store for target_slug under {stores}/_build/{slug} — a
    CANDIDATE for the explicit M6f cutover. BUILD-ONLY by contract: this path
    never calls set_active_slug or search.swap_active; active.json and the
    live search handle are untouched. Same singleton claim as migrations
    (one job at a time across both kinds); returns the final job dict.
    """
    if target_slug not in EMBEDDING_MODELS:
        raise ValueError(
            f"unknown embedding model slug {target_slug!r}; "
            f"known: {sorted(EMBEDDING_MODELS)}"
        )
    _begin_job(target_slug, kind="rebuild")
    return await _run_rebuild_engine(target_slug)


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
    # The persisted kind decides the engine: a "rebuild" job resumes into the
    # build-only engine — a restart must never turn a candidate build into a
    # cutover. Kind-less state (every pre-6d file + all model-switch jobs)
    # resumes the migration engine exactly as before.
    if persisted.get("kind") == "rebuild":
        print(f"[MIGRATE] resuming interrupted chunk rebuild of {target} (build-only)")
        _begin_job(target, kind="rebuild")
        return _launch(target, rebuild=True)
    print(f"[MIGRATE] resuming interrupted migration to {target}")
    _begin_job(target)
    return _launch(target)


# ── shared volume-slice helper (engine + Task 9 watcher gap-heal) ────────────

def slice_snapshot_text(snap_id: str, index: dict, vol_bytes: bytes) -> "str | None":
    """Snapshot body text sliced from the volume by index byte offsets.

    None when the recorded range is invalid for this volume (stale index
    entry, truncated volume) — callers quarantine/skip those ids.
    """
    entry = index.get(snap_id, {})
    bs = entry.get("byte_start", 0)
    be = entry.get("byte_end", 0)
    if bs >= len(vol_bytes) or be > len(vol_bytes) or be <= bs:
        return None
    return vol_bytes[bs:be].decode("utf-8", errors="replace")


# ── chunk batching helpers (rebuild engine + watcher v2 gap-heal) ────────────

def pack_chunk_batches(chunked: list, cap: "int | None" = None) -> list:
    """Pack [(snap_id, [chunks])] into provider-call batches of WHOLE snapshots.

    Snapshots are taken in order until adding the next would exceed `cap`
    flattened chunks; a batch always holds at least one snapshot, so a single
    snapshot with more than `cap` chunks still goes in ONE call by itself —
    a chunk group must come from one aligned provider call (whole-snapshot
    atomicity beats the cap). Order is preserved; nothing is dropped.
    """
    if cap is None:
        cap = CHUNK_BATCH_CAP
    batches: list = []
    current: list = []
    n_chunks = 0
    for snap_id, chunks in chunked:
        if current and n_chunks + len(chunks) > cap:
            batches.append(current)
            current, n_chunks = [], 0
        current.append((snap_id, chunks))
        n_chunks += len(chunks)
    if current:
        batches.append(current)
    return batches


def chunk_group_batches(ids_texts: list, model_key: str) -> tuple:
    """[(snap_id, text)] → (packed chunk batches, empty_ids).

    Chunks each snapshot with chunk_snapshot (verbatim scoring windows sized
    for model_key) and packs the results via pack_chunk_batches. Snapshots
    whose text chunks to nothing are returned in empty_ids for the caller's
    quarantine/skip bookkeeping. Sync + CPU-bound (tokenizer work) — callers
    run it via asyncio.to_thread.

    GROUP POLICY (M6f iteration 2): a multi-chunk snapshot contributes the
    WHOLE-snapshot text FIRST (ordinal 0 — the provider's M5 clamp bounds
    its length, same as the v1 embedding) followed by its chunks, so the
    landed group scores max(whole, chunks) under the v2 max-cosine collapse.
    Single-chunk snapshots are unchanged (their one chunk IS the whole
    text). The +1 text per multi-chunk snapshot counts against the
    CHUNK_BATCH_CAP packing like any other group member. Every group-fill
    consumer inherits this here: the rebuild engine, the model-switch v2
    fill (_fill_v2_batch), and the watcher gap-heal. M8 windowing relies on
    the rule: an ordinal-0 hit means "no specific window".
    """
    chunked, empty_ids = [], []
    for snap_id, text in ids_texts:
        chunks = chunk_snapshot(text, model_key=model_key)
        if not chunks:
            empty_ids.append(snap_id)
            continue
        if len(chunks) > 1:
            chunks = [text] + chunks  # whole-doc vector at ordinal 0
        chunked.append((snap_id, chunks))
    return pack_chunk_batches(chunked), empty_ids


# ── engine ───────────────────────────────────────────────────────────────────

async def _fill_v2_batch(target, provider, ids_texts: list,
                         model_key: str) -> tuple:
    """Embed + group-append one snapshot batch into a schema-2 store.

    The model-switch engine's v2 fill body (A4 side-door fix): chunk each
    snapshot, pack whole snapshots into ≤CHUNK_BATCH_CAP-chunk provider
    calls, regroup by offset, ONE atomic append_group per snapshot. Returns
    (appended_sids, quarantined_sids) — both SNAPSHOT currency, feeding the
    caller's job_appended/raced set algebra unchanged. A snapshot already
    present at append time (raced concurrent append) is skipped and credited
    to NOBODY, mirroring the v1 already_present filter. The rebuild engine
    keeps its own pass-level variant (per-chunk-batch cancel gates +
    progress lines); the chunk/pack core is shared via chunk_group_batches.
    """
    appended: list = []
    quarantined: list = []
    batches, empty_ids = await asyncio.to_thread(
        chunk_group_batches, ids_texts, model_key
    )
    for sid in empty_ids:
        print(f"[MIGRATE] {sid}: empty snapshot body - skipping this run")
    quarantined.extend(empty_ids)
    for batch in batches:
        batch_ids = [sid for sid, _ in batch]
        flat = [chunk for _, chunks in batch for chunk in chunks]
        try:
            vectors = await provider.embed(flat, "document")
        except EmbeddingProviderError as e:
            print(
                f"[MIGRATE] batch failed after provider retries, quarantining "
                f"{len(batch_ids)} snapshot(s) for this run: {batch_ids}: {e}"
            )
            quarantined.extend(batch_ids)
            await asyncio.sleep(BATCH_SLEEP_S)
            continue
        if not vectors or len(vectors) != len(flat):
            # Belt-and-braces (mirrors embed_snapshot_for_index): a drifting
            # provider must never yield a misaligned chunk group.
            print(
                f"[MIGRATE] provider returned {len(vectors or [])} vectors "
                f"for {len(flat)} chunks - quarantining {batch_ids} for this run"
            )
            quarantined.extend(batch_ids)
            await asyncio.sleep(BATCH_SLEEP_S)
            continue
        already_present = target.ids()
        offset = 0
        for sid, chunks in batch:
            group = vectors[offset:offset + len(chunks)]
            offset += len(chunks)
            if sid in already_present:
                continue  # raced concurrent append — not credited to this job
            # fsync-heavy store writes off the loop (engine constraint)
            if await asyncio.to_thread(target.append_group, sid, group):
                appended.append(sid)
        await asyncio.sleep(BATCH_SLEEP_S)
    return appended, quarantined


async def _run_engine(target_slug: str) -> dict:
    """The diff-and-fill loop. Caller has already claimed the job via _begin_job."""
    from Orchestrator.fossils import load_snapshot_index  # lazy: avoid import cycle

    try:
        target = get_store(target_slug)
        provider = get_provider(target_slug)
        # Schema decides the fill shape ONCE (a live store never changes
        # schema mid-job): v1 = whole-text single rows (today's path,
        # byte-identical); v2 = chunk groups — appending whole-snapshot
        # vectors to a v2 store would land LEGAL 1-chunk groups that empty
        # missing() and self-hide forever (A4 side door, M6d hardening).
        target_schema = target.schema
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
                    text = slice_snapshot_text(sid, index, vol_bytes)
                    if text is None:
                        print(
                            f"[MIGRATE] {sid}: invalid byte range for "
                            f"{len(vol_bytes)}-byte volume — skipping this run"
                        )
                        quarantined.add(sid)
                        _quarantine_ids([sid])
                        continue
                    texts.append(text)
                    good_ids.append(sid)

                if good_ids and target_schema == 2:
                    # A4 side-door fix: chunk + group-append via the shared
                    # helpers; quarantine/credit bookkeeping identical to the
                    # v1 branch, all in snapshot currency.
                    appended_now, quarantined_now = await _fill_v2_batch(
                        target, provider, list(zip(good_ids, texts)),
                        target_slug,
                    )
                    if quarantined_now:
                        quarantined.update(quarantined_now)
                        _quarantine_ids(quarantined_now)
                    job_appended.update(appended_now)
                    appends_since_persist += len(appended_now)
                    persist_now = appends_since_persist >= PERSIST_EVERY
                    _advance_done(
                        len(good_ids) - len(quarantined_now),
                        persist=persist_now,
                    )
                    if persist_now:
                        appends_since_persist = 0
                elif good_ids:
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
        try:
            _toolvault_cutover_hook(target_slug)
        except Exception as e:  # noqa: BLE001 — cutover must not fail on toolvault
            print(f"[MIGRATE] toolvault cutover hook raised (non-fatal): {e}")
        # Recompute health for the NEW active model BEFORE marking the job done,
        # so a status poll that sees state=done also reads fresh health. Without
        # this, the watcher's cached "superseded -> <target>" verdict (computed
        # while the OLD model was active) persists until the next daily run or a
        # restart — leaving the banner telling the operator to upgrade to the
        # model they just switched TO. watcher imported lazily: watcher.py
        # imports migrate at module load, so a top-level import here would cycle.
        try:
            from Orchestrator.embeddings import watcher  # lazy: avoid import cycle
            await watcher.run_health_check()
        except Exception as e:  # noqa: BLE001 — cutover must not fail on health refresh
            print(f"[MIGRATE] post-cutover health refresh failed (non-fatal): {e}")
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


# ── rebuild engine (M6 task 6d — build-only, NEVER cuts over) ────────────────

def _build_base_dir() -> Path:
    """Parent dir for candidate chunk stores: {stores_dir}/_build.

    A distinct realpath from the live stores dir, so get_store hands back a
    distinct canonical instance; no meta.json lives at _build's root and
    list_stores does not recurse, so candidates never appear in status/list.
    """
    return Path(config.EMBEDDINGS_STORES_DIR) / BUILD_DIR_NAME


async def _run_rebuild_engine(target_slug: str) -> dict:
    """Diff-and-fill a schema-2 chunk store under _build. No cutover, ever.

    Same convergence shape as _run_engine — re-diff missing() in SNAPSHOT
    currency each pass, converge when empty — but per snapshot the text is
    chunked and appended as ONE contiguous group. Provider calls are sized in
    CHUNKS (CHUNK_BATCH_CAP flattened across whole snapshots; regrouped by
    offset after the call). Failure semantics mirror the migration engine:
    provider-failed batches are quarantined for the run (retried by a later
    run via missing()), any non-provider exception parks the job "stalled",
    and an all-quarantined zero-progress run stalls instead of reporting a
    dead candidate as "done". Caller has already claimed the job.
    """
    from Orchestrator.fossils import load_snapshot_index  # lazy: avoid import cycle

    try:
        # F1 lesson: the build store is ALWAYS opened with explicit schema=2 —
        # autodetect on a fresh dir would default v1 and cement the downgrade.
        target = get_store(target_slug, base_dir=_build_base_dir(), schema=2)
        provider = get_provider(target_slug)
        quarantined: set[str] = set()   # skipped for THIS RUN only
        rows_appended = 0               # rows THIS run wrote
        snaps_appended = 0              # snapshots THIS run wrote
        appends_since_persist = 0

        while True:
            # Re-diff each pass (snapshot currency): mints during the build go
            # to the ACTIVE store (6c is schema-derived), but their index
            # entries land here on the next pass — the loop converges when
            # nothing is missing.
            index = await asyncio.to_thread(load_snapshot_index)
            missing = [
                sid for sid in target.missing(sorted(index.keys()))
                if sid not in quarantined
            ]
            if not missing:
                break
            _set_total_from_missing(len(missing))

            # One volume read per pass, sliced per snapshot (engine pattern).
            vol_bytes = await asyncio.to_thread(
                read_volume_bytes, Path(config.VOL_PATH)
            )
            ids_texts, bad_ids = [], []
            for sid in missing:
                text = slice_snapshot_text(sid, index, vol_bytes)
                if text is None:
                    print(
                        f"[MIGRATE] rebuild {target_slug}: {sid}: invalid byte "
                        f"range for {len(vol_bytes)}-byte volume — skipping this run"
                    )
                    bad_ids.append(sid)
                else:
                    ids_texts.append((sid, text))

            # Chunk + pack off the loop (tokenizer work is CPU-bound).
            batches, empty_ids = await asyncio.to_thread(
                chunk_group_batches, ids_texts, target_slug
            )
            for sid in empty_ids:
                print(
                    f"[MIGRATE] rebuild {target_slug}: {sid}: empty snapshot "
                    f"body — skipping this run"
                )
            bad_ids.extend(empty_ids)
            if bad_ids:
                quarantined.update(bad_ids)
                _quarantine_ids(bad_ids)

            for batch in batches:
                if _CANCEL.is_set():
                    _finish_job("cancelled")
                    return get_job_status()

                batch_ids = [sid for sid, _ in batch]
                flat = [chunk for _, chunks in batch for chunk in chunks]
                try:
                    vectors = await provider.embed(flat, "document")
                except EmbeddingProviderError as e:
                    # Provider already retried with backoff — quarantine the
                    # batch for the run and keep moving (engine constraint 3).
                    print(
                        f"[MIGRATE] rebuild batch failed after provider "
                        f"retries, quarantining {len(batch_ids)} snapshot(s) "
                        f"for this run: {batch_ids}: {e}"
                    )
                    quarantined.update(batch_ids)
                    _quarantine_ids(batch_ids)
                    await asyncio.sleep(BATCH_SLEEP_S)
                    continue
                if not vectors or len(vectors) != len(flat):
                    # Belt-and-braces (mirrors embed_snapshot_for_index): a
                    # drifting provider must never yield a misaligned group.
                    print(
                        f"[MIGRATE] rebuild {target_slug}: provider returned "
                        f"{len(vectors or [])} vectors for {len(flat)} chunks "
                        f"— quarantining {batch_ids} for this run"
                    )
                    quarantined.update(batch_ids)
                    _quarantine_ids(batch_ids)
                    await asyncio.sleep(BATCH_SLEEP_S)
                    continue

                # Regroup by offset; each snapshot lands as ONE atomic group
                # (append_group is whole-group idempotent: a group already
                # present — crash-rerun — writes nothing and returns 0).
                offset = 0
                for sid, chunks in batch:
                    group = vectors[offset:offset + len(chunks)]
                    offset += len(chunks)
                    rows_appended += await asyncio.to_thread(
                        target.append_group, sid, group
                    )
                snaps_appended += len(batch_ids)
                appends_since_persist += len(batch_ids)
                persist_now = appends_since_persist >= PERSIST_EVERY
                _advance_done(len(batch_ids), persist=persist_now)
                if persist_now:
                    appends_since_persist = 0
                with _JOB_LOCK:
                    done, total = _JOB["done"], _JOB["total"]
                print(
                    f"[MIGRATE] rebuild {target_slug}: {done}/{total} "
                    f"snapshots ({rows_appended} rows)"
                )
                await asyncio.sleep(BATCH_SLEEP_S)

        # ── zero-progress guard (mirror of the cutover guard, minus cutover):
        # every missing snapshot quarantined and nothing appended (dead
        # provider) is failure, not a finished candidate — "done" here would
        # read as ready for the 6f swap.
        if not snaps_appended and quarantined:
            msg = (
                f"provider failed for all {len(quarantined)} snapshots; "
                f"rebuild made no progress - build store unchanged"
            )
            print(f"[MIGRATE] ERROR: {msg}")
            _finish_job("stalled", error=msg)
            return get_job_status()

        # Build-only by design: NO cutover on this path, ever — activation is
        # the explicit M6f dir-swap. Completion records the candidate's counts.
        _finish_job("done", rows=target.rows, snapshots=target.snapshots)
        print(
            f"[MIGRATE] rebuild complete: {target_slug} candidate at "
            f"{target.dir} ({target.snapshots} snapshots, {target.rows} rows); "
            f"activation is a separate explicit step"
        )
        return get_job_status()

    except asyncio.CancelledError:
        # Loop teardown (shutdown/restart): leave the persisted state
        # "running" (kind=rebuild) so boot resume relaunches build-only.
        raise
    except Exception as e:  # noqa: BLE001 — park, surface, stay resumable
        print(f"[MIGRATE] rebuild job stalled: {type(e).__name__}: {e}")
        _finish_job("stalled", error=str(e))
        return get_job_status()


def _quarantine_ids(snap_ids: list[str]) -> None:
    """Record run-skipped ids in job state (visible in status; persisted)."""
    with _JOB_LOCK:
        _JOB["skipped"].extend(snap_ids)
        _persist_locked()


def _toolvault_cutover_hook(target_slug: str) -> "threading.Thread | None":
    """Re-embed ToolVault tool descriptions under the new active model.

    Fire-and-forget: the work runs in a daemon thread — sync_embeddings makes
    one blocking embed call per stale description (seconds for ~50 tools),
    far too long to hold the event loop this engine shares with voice/WS
    traffic. ToolVault imports are LAZY (the embeddings package must never
    import toolvault at module level — import-cycle guard). Exceptions are
    contained at both the spawn and inside the thread body: a ToolVault
    hiccup must never fail the cutover. Idempotent: sync_embeddings diffs on
    (model slug, description hash) — a re-fire after a resume-after-crash
    just re-embeds whatever is still stale (wasteful at worst, never
    harmful). Returns the Thread (tests join it) or None if spawning failed.
    """
    def _run():
        try:
            from Orchestrator.toolvault import embeddings as tv_embeddings
            from Orchestrator.toolvault import registry as tv_registry

            canonical = tv_registry.load_canonical()
            store = tv_embeddings.sync_embeddings(canonical)
            print(
                f"[MIGRATE] toolvault re-embed under {target_slug} complete: "
                f"{len(store)} tool vectors cached"
            )
        except Exception as e:  # noqa: BLE001 — never propagate out of the hook
            print(
                f"[MIGRATE] toolvault re-embed failed (non-fatal): "
                f"{type(e).__name__}: {e}"
            )

    try:
        thread = threading.Thread(
            target=_run, name="toolvault-cutover-reembed", daemon=True
        )
        thread.start()
        return thread
    except Exception as e:  # noqa: BLE001
        print(f"[MIGRATE] could not launch toolvault re-embed (non-fatal): {e}")
        return None
