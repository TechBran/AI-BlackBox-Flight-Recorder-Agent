"""Deprecation watcher — daily health check, "auto only when forced" (Task 9).

One daily pass (plus POST /embeddings/health/check as the manual trigger)
keeps the active embedding model honest:

1. PROBE   — embed one short string with the active provider (30s hard cap).
   A failure is re-probed ONCE after RETRY_PROBE_DELAY_S inside the same run:
   one 429 burst (the provider burns its retries in seconds) must not be
   enough to flip a working model's state. Only two failures = "broken".
2. CATALOG — ask the active model's vendor whether the model is still listed
   and whether a newer same-family non-preview model exists (GA-over-preview
   rule). A catalog endpoint being UNREACHABLE is NOT a model failure: the
   check is skipped with a note in detail — a dead listing API must never
   flip a working model's state.
3. STATE (the design-table contract, §7):
     probe ok + listed + no successor → ok          (+ gap-heal, below)
     probe ok + (delisted OR successor) → superseded (banner only; NO auto-spend)
     probe failing                      → broken     (auto-migrate, below)
4. GAP-HEAL (ok only) — embed up to HEAL_CAP index ids missing from the
   active store (mints proceed vector-less when the provider is down, §5).
   Failures are logged and retried next run, quarantine-style — never raised,
   never a state flip.
5. AUTO-MIGRATE (broken only — self-preservation): kicks ONLY on a
   CONSECUTIVE broken run — the previous health.json must already say
   "broken". The first failing run writes broken health plus a loud banner
   and defers; the hourly broken recheck (below) confirms or recovers within
   ~1h. When the gate passes, the first viable target wins:
     (1) registry-mapped same-provider vendor successor whose preflight is
         ready (cloud key present / ollama model pulled),
     (2) the most-complete OTHER existing store whose provider is ready —
         LOCAL stores outrank cloud ones regardless of count (an automatic
         kick must never opt the operator into cloud spend while a local
         store exists; cloud is eligible only when no local store is),
     (3) the lightest local registry model when the Ollama daemon has it,
     (4) none → stay broken, detail says why.
   health.json is written BEFORE the migration job is kicked so the operator
   sees what/why even if the job dies early. A migration already running is
   noted in detail (the all-skipped guard in migrate.py means a broken-
   provider job stalls safely instead of cutting over to an empty store).

health.json ({stores_dir}/health.json, the shape embeddings_routes reads):
    {state, detail, successor, successor_slug, checked_at}
    (+ healed when gap-heal appended; successor is display copy — possibly a
    raw vendor id — while successor_slug is the registry slug or null, the
    only thing an [Update] button may bind to)

Successor heuristic: candidates are same-provider embed-capable catalog ids,
never containing "-preview"/"-exp" (GA-over-preview, MEMORY rule); a candidate
counts only when it has the SAME name skeleton (digit runs masked) and a
STRICTLY HIGHER version tuple than the active model id. Deliberately narrower
than "any other embedding model sorted desc": the live Gemini catalog still
lists text-embedding-004 (deprecated) and OpenAI lists text-embedding-ada-002
— a naive pick would brand those as "successors" of gemini-embedding-001 /
text-embedding-3-large and banner every box forever.

Scheduling: a plain asyncio loop task started from an async startup hook in
startup.py — first run WATCHER_FIRST_DELAY_S after boot (startup is already
index-rebuild heavy), then every WATCH_INTERVAL_OK_S while ok/superseded but
every WATCH_INTERVAL_BROKEN_S while broken (so the consecutive-run migration
gate confirms in ~1h, not a day — and recovery is noticed just as fast).
Chosen over registering
an APScheduler job: CronJobManager is user-facing persisted cron
infrastructure (SQLite rows, history, delivery channels) and would surface an
internal maintenance task as a user job, while the embeddings module already
runs its background work as loop tasks (migration engine, startup resume).
Same-loop tasks also avoid the uvloop run_coroutine_threadsafe bridge.
"""
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import google.generativeai as genai
import httpx
import openai

from Orchestrator import config
from Orchestrator.embeddings.migrate import (
    chunk_group_batches,
    slice_snapshot_text,
    start_migration,
)
from Orchestrator.embeddings.providers import get_provider
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.embeddings.store import (
    _atomic_write_json,
    get_active_slug,
    get_store,
    list_stores,
)
from Orchestrator.notifications.bus import notify
from Orchestrator.volume import read_volume_bytes

HEALTH_FILE = "health.json"        # same name embeddings_routes reads
PROBE_TIMEOUT_S = 30.0             # 1-string probe cap (provider retries inside)
RETRY_PROBE_DELAY_S = 60.0         # in-run re-probe gap; outlives a 429 burst
CATALOG_TIMEOUT_S = 20.0           # ollama /api/tags fetch
HEAL_CAP = 50                      # max gap-heal embeds per run
WATCHER_FIRST_DELAY_S = 5 * 60     # don't pile onto startup
WATCH_INTERVAL_OK_S = 24 * 3600    # daily while ok/superseded
WATCH_INTERVAL_BROKEN_S = 3600     # hourly while broken: confirm/recover fast

# Recent-end gap guard on auto-migration target selection (F4): a fallback
# store frozen at an old date — missing the NEWEST snapshots — must never be
# auto-activated, or a broken active key silently loses recent memory from
# search. A candidate store is rejected if it is missing more than
# RECENT_GAP_MAX index ids total, OR if it is missing ANY of the newest
# RECENT_GAP_TAIL snapshots (by counter). Defaults from config; module-level
# so tests can monkeypatch them.
RECENT_GAP_MAX = config.EMBEDDINGS_RECENT_GAP_MAX    # max total missing ids
RECENT_GAP_TAIL = config.EMBEDDINGS_RECENT_GAP_TAIL  # newest-N that must all be present

# Cloud preflight = key present (same contract as embeddings_routes).
_CLOUD_KEY_ATTRS = {"gemini": "GOOGLE_API_KEY", "openai": "OPENAI_API_KEY"}


# ── vendor catalog seams (each one mockable in tests — NO network there) ─────

async def _gemini_catalog() -> list[str]:
    """Embed-capable model names from the live Gemini catalog (sync SDK)."""
    models = await asyncio.to_thread(lambda: list(genai.list_models()))
    return [
        m.name for m in models
        if "embedContent" in (getattr(m, "supported_generation_methods", None) or [])
    ]


async def _openai_catalog() -> list[str]:
    """All model ids from the OpenAI catalog (embedding filter is the caller's)."""
    async with openai.AsyncOpenAI(api_key=config.OPENAI_API_KEY or None) as client:
        page = await client.models.list()
        return [m.id for m in page.data]


async def _ollama_tags() -> list[str]:
    """Locally pulled model names from the Ollama daemon."""
    async with httpx.AsyncClient(timeout=CATALOG_TIMEOUT_S) as client:
        resp = await client.get(f"{config.OLLAMA_BASE_URL}/api/tags")
        resp.raise_for_status()
        return [m.get("name", "") for m in resp.json().get("models", [])]


# ── successor heuristic ──────────────────────────────────────────────────────

_DIGITS_RX = re.compile(r"\d+")


def _skeleton(model_id: str) -> str:
    """Model id with digit runs masked: gemini-embedding-001 → gemini-embedding-#."""
    return _DIGITS_RX.sub("#", model_id)


def _version_key(model_id: str) -> tuple:
    """All digit runs as an int tuple — the 'how new is it' sort key."""
    return tuple(int(n) for n in _DIGITS_RX.findall(model_id))


def _pick_successor(current_id: str, catalog_ids: list[str]) -> "str | None":
    """Newest same-skeleton, strictly-newer, non-preview candidate; else None."""
    candidates = [
        cid for cid in catalog_ids
        if cid != current_id
        and "-preview" not in cid and "-exp" not in cid   # GA-over-preview rule
        and _skeleton(cid) == _skeleton(current_id)
        and _version_key(cid) > _version_key(current_id)
    ]
    return max(candidates, key=_version_key) if candidates else None


def _registry_slug_for(provider_name: str, vendor_id: str) -> "str | None":
    """Registry slug whose entry matches a vendor model id; None if unmapped."""
    for slug, entry in EMBEDDING_MODELS.items():
        if entry["provider"] == provider_name and entry["model_id"] == vendor_id:
            return slug
    return None


def _local_fallback_slug() -> "str | None":
    """Lightest local (ollama) registry model — the broken-path last resort.

    Derived from the registry (min ram_gb), never a slug literal: registry.py
    is the only place embedding-model literals may live (Task 16 ratchet).
    """
    locals_ = [
        (entry["ram_gb"], slug)
        for slug, entry in EMBEDDING_MODELS.items()
        if entry["provider"] == "ollama"
    ]
    return min(locals_)[1] if locals_ else None


# ── catalog check ────────────────────────────────────────────────────────────

async def _catalog_check(entry: dict) -> tuple:
    """(listed, successor_vendor_id, note) for the active registry entry.

    Catalog UNREACHABLE ≠ model dead: listed stays True, successor None, and
    note records that the check was skipped (surfaces in an ok-state detail).
    """
    provider_name = entry["provider"]
    model_id = entry["model_id"]
    try:
        if provider_name == "gemini":
            ids = await _gemini_catalog()      # already embed-capable only
            candidates = ids
        elif provider_name == "openai":
            ids = await _openai_catalog()
            candidates = [i for i in ids if "embedding" in i]
        else:  # ollama — local models don't deprecate; just confirm presence
            return (model_id in await _ollama_tags()), None, None
    except Exception as e:  # noqa: BLE001 — any listing failure = skip, not dead
        return True, None, f"catalog check skipped ({type(e).__name__}: {e})"
    return model_id in ids, _pick_successor(model_id, candidates), None


# ── recent-end gap guard (F4) ────────────────────────────────────────────────

_SNAP_RX = re.compile(r"SNAP-(\d{8})-(\d+)")


def _snap_sort_key(snap_id: str) -> tuple:
    """(date, counter) ordering key; unparseable ids sort oldest (front)."""
    m = _SNAP_RX.match(snap_id)
    return (m.group(1), int(m.group(2))) if m else ("", 0)


def _newest_tail(index_ids, n: int) -> set:
    """The n newest snap_ids by (date, counter) — the must-be-present tail."""
    return set(sorted(index_ids, key=_snap_sort_key)[-n:]) if index_ids else set()


def _recent_gap_reason(slug: str, store, index_ids, newest_tail) -> "str | None":
    """Reject reason if `store` has a recent-end gap, else None.

    A candidate is unsafe when its missing set (index ids not yet embedded)
    either exceeds RECENT_GAP_MAX total OR includes any of the newest-tail
    ids. Either arm means activating it would drop recent snapshots from
    search — better to stay broken with a loud banner.
    """
    try:
        present = store.ids()
    except Exception as e:  # noqa: BLE001 — an unreadable store is not a safe target
        return f"rejected {slug}: store unreadable ({type(e).__name__})"
    missing = index_ids - present
    if not missing:
        return None
    missing_tail = missing & newest_tail
    if missing_tail:
        return (
            f"rejected {slug}: missing {len(missing_tail)} of the newest "
            f"{len(newest_tail)} snapshots (recent-end gap)"
        )
    if len(missing) > RECENT_GAP_MAX:
        return (
            f"rejected {slug}: missing {len(missing)} snapshots "
            f"(> recent-gap cap {RECENT_GAP_MAX})"
        )
    return None


# ── broken-path target selection ─────────────────────────────────────────────

async def _pick_migration_target(active: str, successor_slug: "str | None") -> tuple:
    """(target_slug, why) per the broken-path precedence; (None, reasons)."""
    try:
        tags = await _ollama_tags()
    except Exception:  # noqa: BLE001 — daemon unreachable = local not ready
        tags = None

    def ready(slug: str) -> bool:
        entry = EMBEDDING_MODELS[slug]
        attr = _CLOUD_KEY_ATTRS.get(entry["provider"])
        if attr is not None:
            return bool(getattr(config, attr, ""))
        return tags is not None and entry["model_id"] in tags

    reasons = []

    # 1. registry-mapped same-provider vendor successor with a ready preflight
    if successor_slug is not None and ready(successor_slug):
        return successor_slug, "vendor successor of the broken model"
    reasons.append("no ready registry successor")

    # 2. most-complete OTHER existing store whose provider is ready — LOCAL
    #    stores outrank cloud ones regardless of count: an automatic kick must
    #    never opt the operator into cloud spend while a local store exists
    #    (design-doc spend-consent rule); cloud is eligible only when none is.
    #    F4 recent-end gap guard: a store frozen at an old date (missing the
    #    newest snapshots) is NOT a safe target — auto-activating it would
    #    silently drop recent memory from search. Each candidate is checked
    #    against the live snapshot index and rejected on a recent-end gap,
    #    leaving the watcher broken (loud banner) over a stale cutover.
    from Orchestrator.fossils import load_snapshot_index  # lazy: avoid import cycle
    try:
        index_ids = set((await asyncio.to_thread(load_snapshot_index)).keys())
    except Exception as e:  # noqa: BLE001 — no index = can't vet recency = no pick
        index_ids = None
        reasons.append(f"snapshot index unavailable for gap check ({type(e).__name__})")
    newest_tail = _newest_tail(index_ids, RECENT_GAP_TAIL) if index_ids else set()

    candidates = [
        s for s in list_stores(Path(config.EMBEDDINGS_STORES_DIR))
        if s["slug"] != active and s["slug"] in EMBEDDING_MODELS
        and s["count"] > 0 and ready(s["slug"])
    ]
    # local-first, then biggest — same precedence; the guard filters within it.
    # "Biggest" is SNAPSHOT COVERAGE, pinned (M6e/audit A11): list_stores
    # `count` stays snapshot currency on every schema (a v2 meta keeps
    # count == snapshots, distinct from its raw chunk-row `rows`), so a
    # chunked store's row inflation must never outrank a store that covers
    # more snapshots. Never sort by s["rows"].
    candidates.sort(
        key=lambda s: (
            EMBEDDING_MODELS[s["slug"]]["privacy"] != "local",
            -s["count"],
        )
    )
    safe = []
    for s in candidates:
        if index_ids is None:
            # Without an index we cannot prove recency — refuse to auto-activate
            # any existing store rather than risk a silent recent-memory loss.
            reasons.append(f"{s['slug']}: skipped (recency unverifiable)")
            continue
        reason = _recent_gap_reason(s["slug"], get_store(s["slug"]), index_ids, newest_tail)
        if reason is None:
            safe.append(s)
        else:
            reasons.append(reason)
    if safe:
        best = safe[0]  # already sorted local-first, biggest-first
        privacy = EMBEDDING_MODELS[best["slug"]]["privacy"]
        return best["slug"], (
            f"most complete {privacy} ready store ({best['count']} vectors)"
        )
    if not candidates:
        reasons.append("no other ready store")

    # 3. lightest local model when the daemon already has it pulled
    fallback = _local_fallback_slug()
    if fallback is not None and fallback != active and ready(fallback):
        return fallback, "local fallback (Ollama model present)"
    reasons.append("local fallback not ready")

    return None, "; ".join(reasons)


# ── gap-heal ─────────────────────────────────────────────────────────────────

async def _gap_heal(active: str) -> int:
    """Embed up to HEAL_CAP active-store gaps (vector-less mints).

    Returns SNAPSHOTS healed — the health["healed"] currency on every ops
    surface (on v1, one row per snapshot, so rows and snapshots coincide).
    NEVER raises: a heal failure is logged and retried on the next daily run
    (quarantine-style, matching migrate's skip semantics) — the model is
    healthy, so the state stays ok regardless.
    """
    from Orchestrator.fossils import load_snapshot_index  # lazy: avoid import cycle

    try:
        store = get_store(active)
        index = await asyncio.to_thread(load_snapshot_index)
        missing = store.missing(list(index.keys()))[:HEAL_CAP]
        if not missing:
            return 0
        # One volume read, sliced per snapshot — same pattern as the migration
        # engine (and the same shared slice helper).
        vol_bytes = await asyncio.to_thread(read_volume_bytes, Path(config.VOL_PATH))
        good_ids, texts = [], []
        for sid in missing:
            text = slice_snapshot_text(sid, index, vol_bytes)
            if text is None:
                print(f"[WATCHER] gap-heal: {sid} has an invalid byte range - skipping")
                continue
            good_ids.append(sid)
            texts.append(text)
        if not good_ids:
            return 0
        provider = get_provider(active)
        if store.schema != 2:
            # v1 active store: today's exact path — ONE whole-text embed call,
            # one append_many (byte-identical; audit A6 rollback safety).
            vectors = await provider.embed(texts, "document")
            # append is idempotent + lock-guarded, so concurrent mint appends
            # are safe; fsync-heavy write goes off the loop.
            appended = await asyncio.to_thread(
                store.append_many, list(zip(good_ids, vectors))
            )
            print(f"[WATCHER] gap-heal: embedded {appended} missing vector(s) for {active}")
            return appended
        # v2 active store: heal in chunk GROUPS — same flatten-cap-regroup as
        # the rebuild engine (chunks flattened across whole snapshots into
        # ≤CHUNK_BATCH_CAP-chunk provider calls; a 50-snapshot heal may take
        # several calls), one atomic append_group per snapshot. A mid-heal
        # provider death keeps the groups already appended (idempotent; the
        # rest stays missing() and is retried next run).
        batches, empty_ids = await asyncio.to_thread(
            chunk_group_batches, list(zip(good_ids, texts)), active
        )
        for sid in empty_ids:
            print(f"[WATCHER] gap-heal: {sid} chunked to nothing - skipping")
        rows = 0
        healed_snaps = 0
        for batch in batches:
            flat = [chunk for _, chunks in batch for chunk in chunks]
            vectors = await provider.embed(flat, "document")
            if not vectors or len(vectors) != len(flat):
                # A misaligned provider must never write a misgrouped batch;
                # the enclosing except logs + returns (retry next run).
                raise RuntimeError(
                    f"provider returned {len(vectors or [])} vectors "
                    f"for {len(flat)} chunks"
                )
            offset = 0
            for sid, chunks in batch:
                group = vectors[offset:offset + len(chunks)]
                offset += len(chunks)
                written = await asyncio.to_thread(
                    store.append_group, sid, group
                )
                if written:
                    rows += written
                    healed_snaps += 1
        print(
            f"[WATCHER] gap-heal: embedded {rows} chunk row(s) across "
            f"{healed_snaps} snapshot(s) for {active}"
        )
        # SNAPSHOT currency: matches health["healed"] everywhere else (a
        # group already present — raced-in — is not counted as healed).
        return healed_snaps
    except Exception as e:  # noqa: BLE001 — heal failure must not flip the state
        print(f"[WATCHER] gap-heal failed (will retry next run): {type(e).__name__}: {e}")
        return 0


# ── health.json ──────────────────────────────────────────────────────────────

def _write_health(health: dict) -> None:
    base = Path(config.EMBEDDINGS_STORES_DIR)
    base.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(base / HEALTH_FILE, health)


def _previous_state() -> "str | None":
    """state from the PREVIOUS run's health.json; None when absent/corrupt.

    Read before this run overwrites the file — the consecutive-failing-runs
    gate on auto-migration lives here.
    """
    try:
        raw = json.loads(
            (Path(config.EMBEDDINGS_STORES_DIR) / HEALTH_FILE).read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return raw.get("state") if isinstance(raw, dict) else None


# ── the health check (daily job body AND the manual-trigger handler) ─────────

async def _probe_active(active: str) -> tuple:
    """One probe of the active provider: (ok, error_string_or_None)."""
    try:
        provider = get_provider(active)
        await asyncio.wait_for(
            provider.embed(["health probe"], "document"), PROBE_TIMEOUT_S
        )
        return True, None
    except Exception as e:  # noqa: BLE001 — any probe failure counts as a miss
        return False, f"{type(e).__name__}: {e}"


async def _notify_state_transition(prev_state, state: str, detail: str) -> None:
    """Fire a system index notification ONLY when the health state CHANGED.

    A steady state (prev == state, e.g. the hourly broken->broken recheck, or a
    daily ok->ok) is silent — re-notifying every run would spam. The first time
    the index goes ok->broken / ok->superseded (or recovers) the operator is told
    via notify(operator="system", category="index"). The watcher is already on
    the event loop, so this awaits notify directly — no sync bridge. Wrapped so a
    notify failure NEVER aborts the health check or its health.json write.
    """
    if prev_state == state:
        return
    # A fresh box (no prior health.json → prev_state is None) landing on "ok" is
    # NOT a meaningful transition — staying silent avoids a "healthy" boot-noise
    # notification. A first-ever broken/superseded IS actionable, so those still
    # fire on None → broken / None → superseded.
    if prev_state is None and state == "ok":
        return
    titles = {
        "broken": "Embedding index broken",
        "superseded": "Embedding model superseded",
        "ok": "Embedding index healthy",
    }
    title = titles.get(state, "Embedding index: " + state)
    try:
        await notify("system", title, detail or title, category="index")
    except Exception as e:  # noqa: BLE001 — a notify failure must not abort the check
        print("[WATCHER] index notification failed (non-fatal): " + repr(e))


async def run_health_check() -> dict:
    """One full watcher pass; writes health.json and returns the health dict."""
    active = get_active_slug()
    entry = EMBEDDING_MODELS.get(active)
    # Capture the PRIOR state once, up front — used both for the transition
    # notification (below) and the broken-path consecutive-run migration gate,
    # before this run's health.json overwrites the file.
    prev_state = _previous_state()

    # 1. probe the active model — one failure earns ONE in-run re-probe after
    #    RETRY_PROBE_DELAY_S; a transient blip (429 burst, hiccup) must not be
    #    declared "broken". Only a second miss in the same run is.
    probe_ok, probe_err = await _probe_active(active)
    if not probe_ok:
        print(
            f"[WATCHER] probe failed ({probe_err}); "
            f"re-probing once in {RETRY_PROBE_DELAY_S:.0f}s"
        )
        await asyncio.sleep(RETRY_PROBE_DELAY_S)
        probe_ok, probe_err = await _probe_active(active)

    # 2. vendor catalog (active slug missing from the registry = config drift;
    #    there is no vendor to ask, so the probe alone decides)
    if entry is not None:
        listed, successor_raw, catalog_note = await _catalog_check(entry)
    else:
        listed, successor_raw, catalog_note = True, None, None

    successor_slug = (
        _registry_slug_for(entry["provider"], successor_raw)
        if entry is not None and successor_raw is not None
        else None
    )
    # Registry slug when mapped; otherwise the raw vendor id is still REPORTED
    # (banner copy) but never auto-migrated to (target selection is stricter).
    successor = successor_slug or successor_raw

    # 3. state decision (the design table)
    if not probe_ok:
        state = "broken"
        detail = f"active model {active} failed its health probe: {probe_err}"
    elif not listed:
        state = "superseded"
        detail = (
            f"active model {active} is no longer listed in the "
            f"{entry['provider']} catalog"
            + (f"; successor available: {successor}" if successor else "")
        )
    elif successor is not None:
        state = "superseded"
        detail = (
            f"a newer {entry['provider']} embedding model is available: "
            f"{successor}; {active} still works (no automatic switch)"
        )
    else:
        state = "ok"
        detail = catalog_note or ""

    health = {
        "state": state,
        "detail": detail,
        "successor": successor,            # display copy (may be a raw vendor id)
        "successor_slug": successor_slug,  # registry slug or None — [Update] binds here
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

    # 6. broken → auto-migrate ("auto only when forced") — but ONLY on a
    #    consecutive broken run: the previous health.json must already say
    #    broken. A first failing run writes broken health + a loud banner and
    #    defers; the hourly broken recheck confirms (or sees recovery) in ~1h.
    if state == "broken":
        if prev_state != "broken":
            health["detail"] += (
                "; auto-migration deferred until a consecutive failing "
                "run confirms (recheck in "
                f"{WATCH_INTERVAL_BROKEN_S // 3600}h)"
            )
            _write_health(health)
            print("[WATCHER] " + "=" * 64)
            print(
                f"[WATCHER] EMBEDDINGS BROKEN (first detection): "
                f"{health['detail']}"
            )
            print("[WATCHER] " + "=" * 64)
            await _notify_state_transition(prev_state, state, health["detail"])
            return health
        target, why = await _pick_migration_target(active, successor_slug)
        if target is None:
            health["detail"] += f"; no viable auto-migrate target ({why})"
            _write_health(health)
        else:
            health["detail"] += f"; auto-migrating to {target}: {why}"
            _write_health(health)  # operator sees what/why BEFORE the job runs
            try:
                await start_migration(target)
            except RuntimeError as e:  # a job is already running — note it
                health["detail"] += f" (migration not started: {e})"
                _write_health(health)
        print(f"[WATCHER] state=broken: {health['detail']}")
        # Steady broken->broken no-ops inside the helper (no spam each hour).
        await _notify_state_transition(prev_state, state, health["detail"])
        return health

    # 5. ok → heal small vector gaps in the active store
    if state == "ok":
        healed = await _gap_heal(active)
        if healed:
            health["healed"] = healed

    # 7. persist + report
    _write_health(health)
    print(f"[WATCHER] state={state}" + (f": {detail}" if detail else ""))
    # ok/superseded: fires only on a real transition (broken->ok recovery,
    # ok->superseded); steady ok->ok / superseded->superseded stay silent.
    await _notify_state_transition(prev_state, state, health["detail"] or detail)
    return health


# ── scheduling (asyncio loop task; see module docstring for the choice) ──────

_WATCHER_TASK: "asyncio.Task | None" = None  # strong ref — loop refs are weak


def _log_watcher_task_outcome(task: "asyncio.Task") -> None:
    """Done-callback: the loop is written to run forever, so any non-cancel
    termination is a silent watcher death — retrieve the exception and make
    it a loud journal line (mirror of migrate's _log_engine_task_outcome)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        print(
            f"[WATCHER] ERROR: watcher task died with unretrieved exception: "
            f"{type(exc).__name__}: {exc}"
        )


def start_watcher() -> "asyncio.Task":
    """Schedule the daily loop on the running event loop (startup hook entry).

    Idempotent: a live loop task is reused. Must be called with an event loop
    running (async startup hook).
    """
    global _WATCHER_TASK
    if _WATCHER_TASK is not None and not _WATCHER_TASK.done():
        return _WATCHER_TASK
    _WATCHER_TASK = asyncio.get_running_loop().create_task(_watch_forever())
    _WATCHER_TASK.add_done_callback(_log_watcher_task_outcome)
    return _WATCHER_TASK


def _next_interval(state: str) -> float:
    """Sleep before the next run: hourly while broken (the consecutive-run
    migration gate confirms — or recovery is noticed — fast), else daily."""
    return WATCH_INTERVAL_BROKEN_S if state == "broken" else WATCH_INTERVAL_OK_S


async def _watch_forever() -> None:
    await asyncio.sleep(WATCHER_FIRST_DELAY_S)  # don't pile onto startup
    while True:
        state = "ok"
        try:
            state = (await run_health_check()).get("state", "ok")
        except Exception as e:  # noqa: BLE001 — the loop must survive any run
            print(f"[WATCHER] health check run failed: {type(e).__name__}: {e}")
        await asyncio.sleep(_next_interval(state))
