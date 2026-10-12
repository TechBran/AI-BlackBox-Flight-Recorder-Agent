# Per-Card "Re-embed" Button — Implementation Plan

**Date:** 2026-07-06
**Status:** DESIGN — awaiting approval to execute
**Author:** Brandon-DEV session (deep-analysis workflow `wlb2ks3w4`, 6 parallel subsystem readers)
**Related memory:** [[project_pluggable_embeddings]], [[project_retrieval_upgrade_complete]], [[project_reranker_tiering_bodyonly]]

---

## 1. Goal

Add a **"Re-embed" button to each embedding-model card** in the onboarding Memory & Search step (and, in a follow-on, the Portal updates-manager card + Android UpdatesScreen). Clicking it performs a **full re-embed of that model's store under the current chunk strategy (schema 2)** — in the background, with the existing live-progress UI — and then makes the freshly-rebuilt store live. Every model's store stays on disk and switchable at any time, and the pre-rebuild store is retained as a rollback.

**Driving use case (Brandon):** the embedding strategy changed from whole-document (schema 1) to chunk-for-scoring (schema 2). A box still on the old strategy has no in-product way to upgrade its vectors — the current migrate flow treats the old rows as "present, nothing to do." We need a one-click, on-demand, per-model re-embed.

### Non-goals
- No re-embed on **reranker** cards (rerankers own no vector store — a re-embed is a no-op for them). *[Decision, §3]*
- No new job/progress infrastructure — we reuse the existing singleton job + `status.job` + wizard progress panel.
- No change to the mint path, retrieval scoring, or the chunk strategy itself.
- Body-only (`content_mode='body'`) re-embed is **out of scope** — measured not worth it and abandoned (content_mode stays inert; we re-embed in `'full'`). See [[project_reranker_tiering_bodyonly]].

---

## 2. Background — what exists vs. the gap

*(All facts below verified against source in the 2026-07-06 analysis workflow.)*

### 2.1 Already works (reuse, don't rebuild)
- **Per-model stores are preserved & switchable.** Each slug owns `{EMBEDDINGS_STORES_DIR}/{slug}/` (`vectors.f32`, `ids.json`, `ordinals.json`, `meta.json`). Switching only rewrites `active.json`; switching back is instant (nothing missing). `store.get_active_slug` / `set_active_slug`, `search.swap_active`.
- **Singleton background job + progress.** `migrate.py` module globals `_JOB` / `_JOB_LOCK` / `_CANCEL` / `_JOB_TASK`; `get_job_status()` → `status.job = {target, state(running|done|stalled|cancelled), done, total, started_at, finished_at, error, skipped[], raced[], cancel_requested}`. `done`/`total` are **snapshot** currency on every schema. Percent + ETA are computed **client-side**. Persisted to `migration_state.json`; `resume_if_interrupted()` relaunches across restart.
- **Progress UI (wizard).** `Portal/onboarding/steps/embeddings.js`: `routeRender()` branches on `status.job.state`; `renderJobPanel()`/`updateJobPanel()` draw the bar + percent + rate-based ETA + Cancel; 2 s poll; `watchingJob` latch; page-reload/restart **re-attach for free** by routing on `status.job`. `409` on a busy job = "attach to the running panel," never an error.
- **A full-rebuild engine exists.** `POST /embeddings/migrate {target, rebuild:true}` → `start_rebuild` → `_run_rebuild_engine` re-embeds the **whole corpus** chunked into `{stores}/_build/{slug}` (schema 2 forced on an empty dir → every snapshot is `missing()` → full re-embed).

### 2.2 The gap (what we must build)
- **Stale schema is invisible to `missing()`.** `store.missing(ids)` keys purely on snap_id membership. A fully-populated **schema-1** store returns `[]` missing → the diff-and-fill migrate engine short-circuits "nothing to do." `open_migration_target` keeps an existing store's on-disk schema (v1 stays v1). **Gap-fill ≠ re-embed.** Schema is only ever a *reported* field, never a trigger.
- **The rebuild engine is BUILD-ONLY — it never activates.** It builds `_build/{slug}` and stops. The activation half — swap the candidate into the live dir, evict the `get_store` cache, repoint live search, catch-up-fill gate-window mints — **is a manual "M6f stop-service dir-swap runbook," not an endpoint.** *Confirmed: every activation reference in code is a printed runbook instruction.*
- **Rebuild tops up a stale candidate** rather than restarting if `_build/{slug}` already has rows, and the `get_store` cache (keyed `(realpath(base), slug)`) must be evicted or a stale in-memory `_id_set` makes `missing()` skip everything.

**Therefore the feature = wire the existing rebuild engine + build the missing in-service activation seam.** ~20% new backend, ~80% reuse + UI.

---

## 3. Locked decisions (from 2026-07-06 approval)

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **Embedding cards only** — no Re-embed on reranker cards. | Rerankers own no store; re-embed is meaningless. Reranker cards keep Select + key validation. |
| D2 | **Cloud = confirm with cost estimate; local = one-click** (keep the existing CPU-time warning). | Cloud re-embed re-bills the user's key (~$2-3 / ~7.6k snapshots). Avoid surprise billing. |
| D3 | **Wizard first, then parity.** | Land + device-validate the onboarding wizard button; port to Portal updates-manager + Android as a follow-on once proven. |
| D4 | **Candidate-swap, not destructive in-place** (engineering call). | Building into `_build/{slug}` then atomic promote keeps live search up for the whole ~minutes-to-hours rebuild and retains a rollback. In-place clear-then-refill would wipe live search and race mints — unacceptable on a running box. |
| D5 | **Auto-activate on completion** (one job, one click). | Brandon: "just triggering a re-embed … in the background … show the progress." No two-step promote. |

---

## 4. Backend design

### 4.1 New engine entrypoint — `start_reembed(target)` (`Orchestrator/embeddings/migrate.py`)
A thin variant of `start_rebuild` that (a) forces a clean full rebuild and (b) auto-activates on convergence. Reuses the same singleton `_JOB`, `_CANCEL`, progress helpers, and `_run_rebuild_engine` build loop.

```
start_reembed(target_slug):
    _begin_job(target, kind="reembed", activate=True, content_mode="full")   # claims singleton → 409 if busy
    # PRE: guarantee a true full rebuild, not a top-up of a stale candidate
    _clear_build_candidate(target)      # rm -rf {stores}/_build/{slug} + evict _STORES[(realpath(_build), slug)]
    _launch(_run_reembed_engine(target))
    return get_job_status()             # 200 body = fresh running job
```

`_run_reembed_engine(target)`:
1. **Build phase** (`phase="building"`): identical to `_run_rebuild_engine` — open `_build/{slug}` with explicit `schema=2, content_mode="full"`; diff-and-fill against the snapshot index in `BATCH_SIZE` group-appends; advance `done`/`total`; honor `_CANCEL` between batches; persist every `PERSIST_EVERY`.
2. On convergence → **activate phase** (`phase="activating"`, cancel now disabled): call `_activate_candidate(target)`.
3. `_finish_job("done", rows=…, snapshots=…)`.

### 4.2 The activation seam — `_activate_candidate(slug)` (NEW)
The missing half of M6f, made in-service and near-zero-downtime:

```
_activate_candidate(slug):
    is_active = (get_active_slug() == slug)
    live   = {stores}/{slug}
    cand   = {stores}/_build/{slug}
    backup = {stores}/{slug}.pre-rebuild.<ts>

    # (a) Same-filesystem renames (fast). Live search still serves from the
    #     in-memory handle, so the on-disk gap is invisible to queries.
    os.rename(cand,  {stores}/{slug}.incoming)
    if live.exists(): os.rename(live, backup)          # rollback retained
    os.rename({stores}/{slug}.incoming, live)

    # (b) Evict BOTH cache entries so nothing serves a stale _id_set.
    _evict_store_cache(slug, base={stores})
    _evict_store_cache(slug, base={stores}/_build)

    # (c) Repoint live search ONLY if this slug is the active model.
    if is_active:
        search.swap_active(slug)                       # loads new schema-2 handle, then flips atomically
        _catch_up_fill(slug)                            # embed snap_ids minted during the build (chunk groups)

    _prune_old_rollbacks(slug, keep=1)                 # keep newest .pre-rebuild, prune older
```

- **Catch-up fill** only matters for the **active** model (mints only ever append to the active store). During the build, live mints landed in the *old* store; after the swap the candidate is missing them → one short v2 diff-and-fill closes the gap (exactly what M6 did: "gate-window mints diff-filled to missing=0"). Non-active re-embed needs no catch-up.
- **Atomicity:** the three renames touch disk only; live search reads its cached in-memory handle throughout, then `swap_active` builds the new handle and flips the module pointer atomically — old handle GC'd. Near-zero downtime.
- **Disk:** transiently ~2× the store size (candidate + `.pre-rebuild` backup). Fine on target hardware (Ultras); note in docs.

### 4.3 New route — `POST /embeddings/reembed` (`Orchestrator/routes/embeddings_routes.py`)
Dedicated route (rather than overloading `MigrateRequest`, which Android + updates-manager already call) — cleaner contract, no risk to existing callers.
```
class ReembedRequest(BaseModel): target: str
@router.post("/reembed")   # 404 unknown slug; 409 job already running; else 200 job dict
    → return await start_reembed(req.target)
```
Cancel reuses the existing `POST /embeddings/migrate/cancel` (same singleton `_CANCEL`).

### 4.4 `status.job` additions
Add `kind: "reembed"` and `phase: "building"|"activating"` to the job dict so the UI can (a) label it "Re-embedding <model>…" and (b) hide Cancel during the atomic activate. Backward-compatible additive fields (mirrors the existing `kind:"rebuild"`).

### 4.5 Resume-across-restart
Extend `resume_if_interrupted()` so a persisted `kind:"reembed"` job relaunches into `_run_reembed_engine` **and still performs activation on convergence** (today's rebuild resume stays build-only). Guard: if the persisted job was already in `phase:"activating"`, re-run `_activate_candidate` idempotently (renames tolerate a half-done swap via the `.incoming` marker).

### 4.6 Stale-schema surfacing (backend)
`status.models[]` already emits per-model `schema` (1|2|null) and `missing`. Confirm both are populated for every model; no new backend field strictly required. Optionally add a derived `strategy: "chunked"|"whole_document"|"none"` for UI clarity (additive).

---

## 5. Frontend design (M2 — onboarding wizard, `Portal/onboarding/steps/embeddings.js`)

- **Card action (`cardActionsHtml` / `wireCardActions`):** add a **Re-embed** button on every *built* embedding card — including the **active** card, which today renders only a static "Currently active" blurb with no buttons (this is the exact code path we change). Button enabled when the card's store exists and no job is running; disabled + "another re-embed is running" hint while `status.job.state==='running'`.
- **Strategy line:** per card, render `status.models[].schema` → "Strategy: chunked (current)" (schema 2) / "Whole-document (older) — re-embed to upgrade" (schema 1, with the existing "superseded"/freshness accent to nudge) / "Not built yet."
- **Confirm dialog (D2):** on click, if `privacy==='cloud'` show a small confirm — snapshot count + estimated cost from `cost_per_1m_tokens` — before POST. Local models: no confirm, but surface the existing `cpu_warning`.
- **Trigger:** `POST /embeddings/reembed {target: slug}`. Treat `200` (seed `status.job` from the body) and `409` (refreshStatus → routeRender → attach to the running panel) **identically** to `startMigrate` — reuse that idiom verbatim.
- **Progress:** reuse `renderJobPanel`/`updateJobPanel`/`etaText`/`cancelMigration` unchanged. Panel header already reads "Re-embedding to <target>". Add a tiny "activating…" sub-label when `job.phase==='activating'` and hide the Cancel button in that phase.
- **Completion:** `renderJobDone` shows "<model> re-embedded (N snapshots, chunked) — now live" (active) or "…— ready to switch to" (non-active).
- **Singleton UX:** because the job is process-wide, only one re-embed runs at a time; all cards' Re-embed buttons disable while any migrate/rebuild/reembed job runs, and every surface attaches to the same panel. Document this as intended.

---

## 6. Milestones & tasks

### M0 — Backend engine + activation seam *(the core)*
- [ ] `_clear_build_candidate(slug)` + `_evict_store_cache(slug, base)` helpers (store cache eviction by `(realpath(base), slug)`).
- [ ] `_activate_candidate(slug)`: rename-swap + evict + conditional `swap_active` + `_catch_up_fill` + `_prune_old_rollbacks(keep=1)`.
- [ ] `_run_reembed_engine` + `start_reembed` (kind="reembed", activate=True, phase field).
- [ ] `resume_if_interrupted` reembed-aware + idempotent activate.
- [ ] `status.job` additive `kind`/`phase`.
- [ ] Route `POST /embeddings/reembed`.
- [ ] **Tests:** unit (clear→build→activate on a tmp stores dir; non-active leaves `active.json` unchanged; active updates live search; catch-up fills gate-window mints; rollback dir created + pruned; 409 when busy; cancel during build aborts cleanly leaving live store intact; resume mid-build and mid-activate). Fresh-box cases: 0 snapshots, already-schema-2 store (still does a genuine full rebuild).

### M1 — Stale-schema surfacing (backend)
- [ ] Verify/emit `schema`+`missing` per model; optional derived `strategy`. Test the status shape.

### M2 — Onboarding wizard UI
- [ ] Re-embed button (active + non-active cards) in `cardActionsHtml`/`wireCardActions`.
- [ ] Strategy line + stale accent.
- [ ] Cloud confirm-with-cost dialog; local CPU-warning surfacing.
- [ ] Wire `/reembed`; 200-seed + 409-attach; reuse job panel; `phase` sub-label + cancel-hide.
- [ ] Completion copy.

### M3 — Validate + snapshot
- [ ] Live-verify on this box: re-embed a non-active model (store upgrades, active unchanged), then re-embed the active model (search flips to chunked, `missing=0`, rollback dir present). Tail journal for `[EMBEDDING]`/mint lines.
- [ ] Device-validate the wizard on the Fold. Mint a dev snapshot.

### M4 — Portal updates-manager parity *(follow-on)*
- [ ] Re-embed affordance in `Portal/modules/updates-manager.js` embeddings card (5 s poll, one-line progress, 409-attach).

### M5 — Android parity *(follow-on)*
- [ ] `EmbeddingsCard`/`UpdatesViewModel`/`UpdateRepository`/`EmbeddingsStatus.kt`: Re-embed action + `/reembed` call + `kind/phase` fields. Build + device-validate.

---

## 7. Risks & mitigations
- **Cache staleness after dir-swap** → evict both `_STORES` entries; add a test asserting `missing()` sees a fresh set post-activate.
- **Mint race during activate** → renames are disk-only; `swap_active` flips the in-memory handle atomically; catch-up fill closes the build window; post-swap mints already target schema 2.
- **Singleton contention** (watcher auto-migrate / another operator) → 409-attach idiom on every surface.
- **Disk (~2× transient)** → `.pre-rebuild` retained only `keep=1`, pruned on next success; document the transient footprint.
- **Cloud billing** → D2 confirm dialog with estimate.
- **Interrupted activate** → `.incoming` marker + idempotent `_activate_candidate` on resume.

## 8. Rollback plan
- Backend is additive (new route + engine variant; existing `/migrate` untouched). Revert = drop the route + engine fn.
- Per-operation rollback: the `{slug}.pre-rebuild.<ts>` dir is a complete prior store; a "revert re-embed" is a reverse dir-swap (future nicety; manual/CLI acceptable for v1).

## 9. Open items to confirm during build
- Exact estimated-cost formula for the cloud confirm (tokens ≈ corpus chars / 4 × `cost_per_1m_tokens`); pin wording.
- Whether to pin `[retrieval] chunk_tokens`/`chunk_overlap_pct` in `config.ini` before a mass re-embed (currently live on code fallbacks 1024/15).
- `.pre-rebuild` retention policy (keep=1 proposed).
