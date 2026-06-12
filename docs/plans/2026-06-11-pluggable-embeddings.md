# Pluggable Snapshot Embeddings — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task (implementer → spec reviewer → quality reviewer per task).

**Goal:** Pluggable embedding providers (Gemini / OpenAI / Ollama-Qwen3) behind one module, per-model persistent binary vector stores replacing the 408MB inline-JSON index, background diff-and-fill migration with atomic cutover, a deprecation watcher ("auto only when forced"), an onboarding-wizard step that owns the whole module, and notification cards in both update sections (Portal + Android) that deep-link to the wizard.

**Design:** `docs/plans/2026-06-11-pluggable-embeddings-design.md` (read it first — decisions are locked).

**Architecture:** New `Orchestrator/embeddings/` package (registry, providers, store, search, migrate, watcher) + `routes/embeddings_routes.py`. `monitoring.generate_embedding`/`semantic_search` become delegates so every existing caller keeps working. `fossils.update_snapshot_index` is the single vector write seam. Frontends: `Portal/onboarding/steps/embeddings.js` (new step), `Portal/modules/updates-manager.js` (card), Android `ui/updates/UpdatesScreen.kt` (card).

**Tech stack:** numpy (already in venv, 2.4.2), httpx (present), google.generativeai (present), openai 2.41.1 (present), Ollama REST `localhost:11434`.

**Worktree:** `.worktrees/pluggable-embeddings` (branch `feat/pluggable-embeddings`). `config.ini`/`.env` are symlinked to the main checkout. Run tests with `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/ -q` from the worktree root. Baseline: 331 passed.

**Hard rules (carried from CU pass):**
- Prod runs LIVE from the main checkout working tree — all work stays in the worktree until final merge.
- CRLF: `Portal/modules/*.js`, Android `*.kt` are CRLF. Edit with python **binary mode** (`'rb'/'wb'`, `\r\n`) or careful Edit; never text-mode rewrite whole files.
- Multiline commit messages via `git commit -F -` heredoc (chained `sed && git commit -m "multiline"` silently no-ops).
- Stage explicit paths only; never `git add -A`.
- Never hardcode an embedding-model literal outside `Orchestrator/embeddings/registry.py` (guard-tested in Task 16).
- APScheduler + uvloop: capture loop via `asyncio.get_running_loop()` in async start, use `asyncio.run_coroutine_threadsafe` from sync wrappers.

---

## Task 1: Registry + config section

**Files:**
- Create: `Orchestrator/embeddings/__init__.py` (empty), `Orchestrator/embeddings/registry.py`
- Modify: `Orchestrator/config.py` (new `[embeddings]` section accessors)
- Test: `Orchestrator/tests/test_embeddings_registry.py`

**Step 1: Write failing tests** — `test_embeddings_registry.py`:
- every entry in `EMBEDDING_MODELS` has fields: `provider` (∈ {gemini, openai, ollama}), `model_id`, `dims` (int), `label`, `ram_gb` (float, 0 for cloud), `cost_per_1m_tokens` (float, 0 for local), `privacy` (∈ {cloud, local}), `quality_note` (str), `query_instruction` (str|None), `keep_alive` (str|None)
- exact slugs present: `gemini-embedding-001` (dims 3072), `openai-text-embedding-3-large` (dims 3072), `qwen3-embedding-0.6b` (dims 1024), `qwen3-embedding-8b` (dims 4096)
- slugs are kebab-case (`re.fullmatch(r"[a-z0-9.\-]+", slug)`)
- `config.EMBEDDINGS_ACTIVE_DEFAULT` exists and is a registry slug; `config.EMBEDDINGS_STORES_DIR` ends with `Manifest/embeddings`

**Step 2:** Run: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_embeddings_registry.py -q` — expect FAIL (module missing).

**Step 3: Implement.** `registry.py`:
```python
EMBEDDING_MODELS = {
    "gemini-embedding-001": {
        "provider": "gemini", "model_id": "models/gemini-embedding-001", "dims": 3072,
        "label": "Gemini (cloud)", "ram_gb": 0.0, "cost_per_1m_tokens": 0.15,
        "privacy": "cloud", "quality_note": "Current default; auto-tracked for deprecation",
        "query_instruction": None, "keep_alive": None,
    },
    "openai-text-embedding-3-large": {
        "provider": "openai", "model_id": "text-embedding-3-large", "dims": 3072,
        "label": "OpenAI (cloud)", "ram_gb": 0.0, "cost_per_1m_tokens": 0.13,
        "privacy": "cloud", "quality_note": "Second cloud option (BYOK OpenAI key)",
        "query_instruction": None, "keep_alive": None,
    },
    "qwen3-embedding-0.6b": {
        "provider": "ollama", "model_id": "qwen3-embedding:0.6b", "dims": 1024,
        "label": "Qwen3 0.6B (local, light)", "ram_gb": 1.0, "cost_per_1m_tokens": 0.0,
        "privacy": "local", "quality_note": "Fast on CPU; fully offline",
        "query_instruction": "Instruct: Given a search query, retrieve relevant conversation snapshots\nQuery: ",
        "keep_alive": "-1",
    },
    "qwen3-embedding-8b": {
        "provider": "ollama", "model_id": "qwen3-embedding:8b", "dims": 4096,
        "label": "Qwen3 8B (local, max quality)", "ram_gb": 6.0, "cost_per_1m_tokens": 0.0,
        "privacy": "local", "quality_note": "MTEB #1 open-source; slow re-embeds on CPU",
        "query_instruction": "Instruct: Given a search query, retrieve relevant conversation snapshots\nQuery: ",
        "keep_alive": "5m",
    },
}
EMBEDDING_MAX_CHARS = 10000  # truncate document text before embedding (existing behavior)
```
`config.py`: follow the `[computer_use]` pattern — `EMBEDDINGS_ACTIVE_DEFAULT = _get("embeddings", "active", "gemini-embedding-001")`, `EMBEDDINGS_STORES_DIR = str(PROJECT_ROOT / "Manifest" / "embeddings")`, `OLLAMA_BASE_URL = _get("embeddings", "ollama_url", "http://localhost:11434")`.

**Step 4:** Tests pass. **Step 5:** Commit `feat(embeddings): model registry + config section`.

---

## Task 2: VectorStore (binary per-model stores)

**Files:**
- Create: `Orchestrator/embeddings/store.py`
- Test: `Orchestrator/tests/test_embeddings_store.py` (use `tmp_path`)

**Spec.** `VectorStore(slug, dims, base_dir)` manages `{base_dir}/{slug}/` containing `vectors.f32` (raw little-endian float32, row-major N×dims), `ids.json` (ordered snap_id list; row i ↔ vector i), `meta.json` (`{slug, dims, normalized: True, count, last_updated}`).

API:
- `open()` — load ids, mmap vectors (`np.memmap` or `np.fromfile` reload on append), **self-heal**: `rows = filesize // (4*dims)`; if `len(ids) != rows`, truncate BOTH to `min(...)` (file truncate / ids slice) and log
- `append(snap_id, vector)` — reject `len(vector) != dims` with `ValueError`; L2-normalize; append row + flush + fsync; rewrite `ids.json` atomically (tmp + `os.replace`); update meta; skip silently if snap_id already present (idempotent)
- `ids()` → set; `count` property
- `missing(all_snap_ids)` → ordered list of snap_ids not in store
- `search(query_vec, k, allowed_ids=None)` — normalize query, single mat-vec `scores = M @ q`, walk `np.argsort(scores)[::-1]`, skip ids not in `allowed_ids` (when given), return top-k `(snap_id, float(score))`
- module fn `list_stores(base_dir)` → `[{slug, dims, count, last_updated}]`
- module fns `get_active_slug()` / `set_active_slug(slug)` — `Manifest/embeddings/active.json` atomic read/write; falls back to `config.EMBEDDINGS_ACTIVE_DEFAULT` when file absent

**TDD steps:** failing tests for: append/search roundtrip (3 small vectors, correct order), normalization (stored row has unit norm), dims mismatch raises, idempotent re-append, self-heal (truncate ids.json by hand → open() heals; truncate vectors.f32 mid-row → heals), `missing()` diff, allowed_ids filter, active.json roundtrip + default fallback. Then implement, pass, commit `feat(embeddings): binary VectorStore with self-heal + active pointer`.

---

## Task 3: Providers (Gemini / OpenAI / Ollama)

**Files:**
- Create: `Orchestrator/embeddings/providers.py`
- Test: `Orchestrator/tests/test_embeddings_providers.py`

**Spec.** One interface: `async def embed(texts: list[str], purpose: str) -> list[list[float]]` where `purpose ∈ {"document", "query"}`. Factory `get_provider(slug)` reads the registry. All providers: truncate each text to `EMBEDDING_MAX_CHARS`, retry 3× with exponential backoff (1s/2s/4s), raise `EmbeddingProviderError` after final failure (callers decide whether to swallow).

- **GeminiProvider**: `genai.embed_content(model=model_id, content=text, task_type="retrieval_document"|"retrieval_query")` — `purpose` maps to task_type (**fixes the existing bug where queries used retrieval_document**). Loop per text (SDK batching optional later). Run sync SDK call via `asyncio.to_thread`.
- **OpenAIProvider**: `AsyncOpenAI().embeddings.create(model=model_id, input=texts)`; `purpose` ignored. API key from existing config (`OPENAI_API_KEY`).
- **OllamaProvider**: `httpx.AsyncClient` POST `{OLLAMA_BASE_URL}/api/embed` body `{"model": model_id, "input": [texts...], "keep_alive": keep_alive}` → `resp["embeddings"]`. For `purpose="query"` and a non-None `query_instruction`, prefix each text with the instruction.

**Tests (all mocked — no network):** purpose→task_type mapping asserted via mock call args; Ollama query-instruction prefixing; Ollama keep_alive passthrough; retry-then-raise; truncation applied. Commit `feat(embeddings): provider layer (gemini/openai/ollama) with purpose mapping`.

---

## Task 4: Transcode — inline JSON → binary store (layout v2)

**Files:**
- Create: `Orchestrator/embeddings/transcode.py`
- Modify: `Orchestrator/startup.py` (idempotent startup hook)
- Test: `Orchestrator/tests/test_embeddings_transcode.py`

**Spec.** `transcode_inline_embeddings(index_path, base_dir)`:
1. Load index. If **no** entry has an `embedding` key → return `{"migrated": 0}` (idempotent no-op).
2. Group inline vectors by dims; vectors with dims 3072 go to the `gemini-embedding-001` store (`append` each — normalization applied there); any other dims are logged and **dropped** (pre-2026 768-dim leftovers are stale by definition).
3. Write slim index: strip `embedding` keys from every entry, write to tmp, **backup original to `snapshot_index.json.bak.pre-embeddings-v2`**, then `os.replace`.
4. Return counts `{"migrated", "dropped", "index_bytes_before", "index_bytes_after"}`.

Startup hook (`startup.py`, after snapshot index load): check disk free ≥ 1.5× index size before transcoding (`shutil.disk_usage`); on insufficient space log loudly and skip (system keeps working — search reads inline embeddings? **No**: keep cutover safe by making Task 5's search fall back to inline `data["embedding"]` when the active store is empty AND inline vectors exist. That fallback is deleted in Task 16.)

**Golden parity test:** build a fixture index (10 entries, random 3072-dim vectors, 2 operators) + fixture query vector; compute top-5 via the OLD pure-python `cosine_similarity` path; run transcode; compute top-5 via `VectorStore.search`; assert same ids, scores equal within 1e-5. Plus: idempotency (second run no-ops), backup file created, slim index has no `embedding` keys.

Commit `feat(embeddings): inline-JSON → binary store transcode with golden parity`.

---

## Task 5: Search + generate_embedding cutover (delegates)

**Files:**
- Create: `Orchestrator/embeddings/search.py`
- Modify: `Orchestrator/monitoring.py` (generate_embedding + semantic_search become delegates)
- Test: `Orchestrator/tests/test_embeddings_search.py`

**Spec.** `search.py` holds the live state:
- `_active: VectorStore|None` + `threading.Lock`; `get_active_store()` lazy-opens from `get_active_slug()`; `swap_active(slug)` for cutover
- `def generate_embedding_sync(text, purpose="document") -> list[float]|None` — resolves active slug → provider → `asyncio.run`/loop-safe bridge (monitoring callers are sync; use a helper that works both with and without a running loop — `asyncio.run` in a worker thread via `concurrent.futures` if a loop is running). Returns None on provider failure (preserves existing mint behavior).
- `def semantic_search(query, operator="", k=10)` — embed query with `purpose="query"`; operator filter: build `allowed_ids` from the snapshot index when `operator` set and ≠ "system" (same rule as today); `store.search(...)`. **Fallback:** if active store `count == 0`, fall back to the legacy inline-JSON cosine path (needed for the pre-transcode window; removed in Task 16).

`monitoring.py`: `generate_embedding(text)` → `return search.generate_embedding_sync(text, purpose="document")`; `semantic_search(...)` → delegate. Delete the hardcoded `models/gemini-embedding-001` call. Keep `cosine_similarity` (used by fallback + tests) until Task 16.

**Verify all consumers still work:** `grep -rn "\[.embedding.\]\|\.get(.embedding" Orchestrator/*.py Orchestrator/routes/*.py | grep -v venv` — every direct reader found must be routed through the store or the documented fallback. (Recon found readers only in `monitoring.py` + `backfill_embeddings.py`; re-verify.)

**Tests:** numpy search parity vs naive cosine on fixtures; operator filtering (incl. "system" sees all); query purpose passed to provider (mock); store-empty fallback path. Run **full suite** — existing search-related tests must stay green. Commit `feat(embeddings): numpy search + monitoring delegates (retrieval_query fix)`.

---

## Task 6: Mint path writes to the active store

**Files:**
- Modify: `Orchestrator/fossils.py` (`update_snapshot_index`, ~line 476)
- Test: `Orchestrator/tests/test_embeddings_mint.py`

**Spec.** `update_snapshot_index(...)` keeps its signature (3 checkpoint.py call sites untouched). Inside: when `embedding` is not None → `get_active_store().append(snap_id, embedding)`; the JSON entry **no longer stores** the `embedding` key. When `embedding` is None → entry written without vector (today's behavior; store self-heals later via watcher/migration).

Guard: if `len(embedding) != active_store.dims` (e.g. cutover happened mid-mint between generate and index-update) → log + drop the vector rather than corrupt the store (the catch-up loop re-embeds it).

**Tests:** mint writes vector to store + index entry has no `embedding` key; None-embedding mint OK; wrong-dims vector dropped with log, index entry still written. Full suite. Commit `feat(embeddings): mint path appends to active store, index stays slim`.

---

## Task 7: `/embeddings/status` + routes scaffold

**Files:**
- Create: `Orchestrator/routes/embeddings_routes.py`
- Modify: `Orchestrator/app.py` (include router — beside the onboarding router include, app.py:121)
- Test: `Orchestrator/tests/test_embeddings_routes.py` (FastAPI TestClient)

**Spec.** `GET /embeddings/status` →
```json
{
  "active": "gemini-embedding-001",
  "health": {"state": "ok|superseded|broken", "detail": "", "successor": null},
  "job": null,
  "stores": [{"slug": "...", "dims": 3072, "count": 6975, "missing": 12, "last_updated": "..."}],
  "models": [{"slug": "...", "label": "...", "dims": 3072, "ram_gb": 0.0,
               "cost_per_1m_tokens": 0.15, "privacy": "cloud", "quality_note": "...",
               "store_exists": true, "missing": 12, "ready": true, "blockers": []}],
  "ollama": {"installed": false, "running": false, "models": []}
}
```
`missing` = index ids minus store ids. `health` comes from watcher state (file `Manifest/embeddings/health.json`, default ok). `job` filled by Task 8. `models[].ready/blockers` = preflight-style: cloud → key present; ollama → installed+running+model pulled (Task 10 fills; stub `ready: true` for cloud, `false` w/ blocker "ollama integration pending" for local until Task 10).

Also `POST /embeddings/validate {"slug"}` → probe-embed one short string with that provider → `{ok, error?, dims?}` (used by the wizard before committing to a model).

**Tests:** shape assertions with fixture stores; validate happy + failure (mocked provider). Commit `feat(embeddings): status + validate endpoints`.

---

## Task 8: Migration job — diff-and-fill + atomic cutover

**Files:**
- Create: `Orchestrator/embeddings/migrate.py`
- Modify: `Orchestrator/routes/embeddings_routes.py` (migrate/cancel endpoints, job in status)
- Test: `Orchestrator/tests/test_embeddings_migrate.py`

**Spec.** Singleton `MigrationJob` (module-level, like CU session manager):
- State: `{target, state: idle|running|stalled|done|cancelled, done, total, started_at, error, last_snap_id}` — persisted to `Manifest/embeddings/migration_state.json` on every N=25 appends (resume metadata only; real resume truth is the store contents).
- `async run(target_slug)`: loop — `missing = target_store.missing(index_ids)`; if empty → cutover; else embed in batches of 8 (read text from volume by byte offsets — port the read pattern from `backfill_embeddings.py:171-184`), `purpose="document"`, append each, update progress. Cloud: `await asyncio.sleep(0.2)` between batches. Provider failure: backoff retries inside provider; if a batch still fails → `state=stalled` with error (resumable by re-POST).
- **Catch-up loop:** after fill, re-diff; repeat until `missing == []` (new mints land in the active store *and* the index — diff catches them).
- **Cutover:** `set_active_slug(target)` + `search.swap_active(target)` under the search lock; then fire-and-forget ToolVault re-embed hook (Task 11; no-op stub now); `state=done`.
- Startup resume: if persisted state says `running` → relaunch `run(target)` (app.py startup hook).

Endpoints: `POST /embeddings/migrate {"target"}` → 409 if running, 404 unknown slug, else start (asyncio task on the running loop) + return status. `POST /embeddings/migrate/cancel` → cooperative cancel flag, state=cancelled.

**Tests (fake provider returning deterministic vectors, tiny fixture volume + index):** full migration → cutover flips active.json; switch-back delta (pre-populate target store with most ids → job only embeds the diff); catch-up convergence (test injects a new index entry + active-store append between pass 1 and 2); cancel; resume-after-restart (re-instantiate job from persisted state); 409 on concurrent start. Commit `feat(embeddings): diff-and-fill migration job with atomic cutover`.

---

## Task 9: Watcher — daily health check, "auto only when forced"

**Files:**
- Create: `Orchestrator/embeddings/watcher.py`
- Modify: `Orchestrator/app.py` or scheduler startup (register daily job on existing APScheduler — follow `Orchestrator/scheduler/manager.py` patterns, mind the uvloop rule)
- Test: `Orchestrator/tests/test_embeddings_watcher.py`

**Spec.** `async run_health_check()` (also exposed as `POST /embeddings/health/check` for manual trigger + tests):
1. **Probe** active model: 1-string embed → ok/fail.
2. **Catalog** per provider of the active model: gemini → `genai.list_models()` filtered to `embedContent` support; openai → models list; ollama → `/api/tags` has model. Determine: still listed? newer stable embedding model present (successor heuristic: same provider, name contains "embedding", not the current one, not `-preview`/`-exp` — GA-over-preview rule)?
3. **State:** probe ok + listed → `ok` (write health.json; also gap-heal: embed up to 50 `missing()` ids of the ACTIVE store); probe ok + successor exists → `superseded` (health.json carries successor; frontends banner; NO auto-spend); probe failing or delisted → `broken` → **auto-migrate**: target = unambiguous vendor successor, else best complete local store, else `qwen3-embedding-0.6b` if Ollama ready, else stay broken with detail; kick `MigrationJob`, write health.json with what/why for the operator.

**Tests (canned catalogs/mocked probe):** each state transition; successor heuristic rejects `-preview`; broken-path target precedence (successor > complete local store > pullable 0.6b > stay broken); gap-heal caps at 50. Commit `feat(embeddings): deprecation watcher — notify on superseded, auto-migrate only when broken`.

---

## Task 10: Ollama integration (status, pull, preflight)

**Files:**
- Modify: `Orchestrator/routes/embeddings_routes.py`, `Orchestrator/embeddings/providers.py` (status helpers)
- Test: `Orchestrator/tests/test_embeddings_ollama.py`

**Spec.**
- `GET` status fills `ollama` block in `/embeddings/status`: `installed` (`shutil.which("ollama")` or `/api/version` reachable), `running` (`GET /api/version` 200), `models` (`GET /api/tags` names).
- `POST /embeddings/ollama/pull {"model"}` → background task streaming `POST /api/pull` (NDJSON progress lines) into a pull-state dict surfaced in `/embeddings/status` as `ollama.pull: {model, completed, total, status}`; 409 if a pull is running.
- **Preflight** in `models[].blockers` (customer-facing remediation strings, `/cu/preflight` contract): Ollama not installed → "Install Ollama: curl -fsSL https://ollama.com/install.sh | sh" (v1 = copy-paste remediation, NO orchestrator auto-install — service lacks root and ProtectSystem forbids /etc writes); not running → "Start it: sudo systemctl start ollama"; model not pulled → wizard offers the pull button; free RAM (`psutil.virtual_memory().available`) < `ram_gb` → "Needs ~XGB free RAM; close apps or pick the lighter model".

**Tests:** mocked `/api/tags`/`/api/version`/`/api/pull` NDJSON stream parsing; blocker strings for each failure mode; RAM gate. Commit `feat(embeddings): ollama status/pull/preflight`.

---

## Task 11: ToolVault on the shared provider layer

**Files:**
- Modify: `Orchestrator/toolvault/embeddings.py`, `Orchestrator/toolvault/config.py`, `Orchestrator/embeddings/migrate.py` (real cutover hook)
- Test: extend `Orchestrator/toolvault/tests/` (cache-key invalidation)

**Spec.** ToolVault's embedder calls the shared active provider instead of its own `EMBEDDING_MODEL` constant. Its `embeddings.json` cache entries gain the active slug in the key (`{slug}:{desc_hash}`) so a model switch invalidates cleanly; on cutover the migrate job triggers the existing ToolVault re-embed/reload path (~100 descriptions, seconds). Dims constant usages inside ToolVault must read dims from the registry. Validate with `python -m Orchestrator.toolvault.validate` + ToolVault test suite (21 injector tests must stay green).

Commit `feat(embeddings): toolvault rides the shared provider + model-keyed cache`.

---

## Task 12: `backfill_embeddings.py` → thin CLI wrapper

**Files:**
- Modify: `Orchestrator/backfill_embeddings.py` (gut to argparse wrapper)

**Spec.** Keep the filename (ops muscle-memory). New body: parse `--target <slug>` (default: active slug), then `asyncio.run(MigrationJob().run(target))` with console progress — same engine as the API, no duplicated embed logic. Delete the local `generate_embedding` copy. Smoke test: `--help` works; tiny-fixture run in test. Commit `refactor(embeddings): backfill script wraps the migration job`.

---

## Task 13: Onboarding wizard step (backend + frontend)

**Files:**
- Modify: `Orchestrator/onboarding/state.py` (`StepName`, `ALL_STEPS` — insert `"embeddings"` immediately after `"api_keys"`), `Portal/onboarding/onboarding.js` (`STEPS` + `STEP_LABELS` + `?step=` deep-link), `Portal/onboarding/onboarding.css` (cards)
- Create: `Portal/onboarding/steps/embeddings.js`
- Test: backend state test (step ordering/advance); frontend = manual visual check note

**Spec.**
- Backend: new step advances like the others (`/onboarding/step/complete|skip`). Skipping = stay on current active model (gemini default) — valid, never blocks onboarding.
- `steps/embeddings.js` (mirror the structure of `steps/transcription.js` — closest analog, also a provider picker): render model cards from `GET /embeddings/status` `models[]` (label, quality_note, dims, RAM or cost, privacy badge, freshness "store exists — N behind" when applicable, blockers list). Select cloud → `POST /embeddings/validate` probe → enable "Build index / Backfill N & switch" button → `POST /embeddings/migrate` → poll `/embeddings/status` every 2s → progress bar (`done/total`, ETA) → on `done` show cutover confirmation → step complete. Select local → if blockers: show remediation (+ Pull button wired to `/embeddings/ollama/pull` with progress) → then same migrate flow.
- Deep-link: in `onboarding.js`, when `params.get("step")` names a known step, jump straight to it (revisit mode — works after onboarding completion; reuse the existing `params` const at line 27).
- A migration already running when the step opens → show its progress instead of the picker grid (one job at a time).

Commit `feat(onboarding): embeddings wizard step — picker, ollama pull, migrate progress, deep-link`.

---

## Task 14: Portal update-section card

**Files:**
- Modify: `Portal/modules/updates-manager.js` (CRLF!), `Portal/index.html` (card container in `.updates-section` + version bump `?v=genui282`), relevant `Portal/styles/` feature css

**Spec.** On `initUpdatesPanel()` (menu open), additionally fetch `/embeddings/status`. Render a card when noteworthy; **two affordances** (Brandon 2026-06-11):

- **[Update] button** — one-click direct action, no wizard detour: `POST /embeddings/migrate {target: successor}`. The card copy *explains what will happen* before they click: "Your system will transfer embeddings to <successor> in the background. Search keeps working the whole time; the switch happens automatically when it finishes and survives restarts." (24/7 appliance — background is the normal mode.)
- **[Manage] button** — the managed path: `window.location.href = "/onboarding/?step=embeddings"` (wizard embeddings section, full control).

States: `health=superseded` → info card with both buttons; `health=broken` → urgent styling, auto-migration already underway per watcher — show what/why detail + [Manage] only; `job.state=running` → progress line "Re-embedding N/M…" + [Manage]; `health=ok` + no job → render nothing. Failure of the status fetch must not break the updates panel (try/catch, card hidden).

Commit `feat(portal): embeddings notification card in updates section → wizard deep-link`.

---

## Task 15: Android update-section card

**Files (CRLF, base `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/`):**
- Modify: `ui/updates/UpdatesScreen.kt`, `ui/updates/UpdatesViewModel.kt`, data layer (model + API call beside the existing `UpdateStatus` fetch — follow its exact pattern)

**Spec.** `EmbeddingsStatus` DTO (subset: active, health{state, detail, successor}, job{state, done, total}). ViewModel fetches it alongside `/update/status` (independent failure → null → no card). `EmbeddingsCard` composable mirroring Portal's three states and **the same two affordances**: **[Update]** → `POST /embeddings/migrate {target: successor}` directly from the card (copy explains the background transfer + restart survival before tapping), shown only for `superseded`; **[Manage]** → `Intent(Intent.ACTION_VIEW, Uri.parse("$origin/onboarding/?step=embeddings"))`, shown in all states. Use the existing Card/StatusCard idioms in the file.

**Gate:** `ANDROID_HOME=$HOME/Android/Sdk ./gradlew compileDebugKotlin` from the app dir. Commit `feat(android): embeddings card in UpdatesScreen → wizard deep-link`.

---

## Task 16: Guard ratchets, fallback removal, docs

**Files:**
- Create: `Orchestrator/tests/test_embeddings_guards.py`
- Modify: `Orchestrator/embeddings/search.py` + `Orchestrator/monitoring.py` (delete the inline-JSON fallback + dead `cosine_similarity` if unused), `CLAUDE.md`, `.claude/commands/snapshot-dev.md`

**Spec.**
- Guard tests (CU pattern, `inspect.getsource` scans): no `gemini-embedding-001` / `text-embedding` / `qwen3-embedding` literal outside `Orchestrator/embeddings/registry.py` (scan monitoring.py, checkpoint.py, fossils.py, toolvault modules, routes); registry completeness already covered by Task 1; `EMBEDDING_MODELS` slugs == stores the wizard can offer (status models[] built from registry — assert via route test).
- Remove the Task-5 inline-embedding fallback (transcode is in; fallback is dead weight) — full suite must stay green.
- Docs: CLAUDE.md embedding mentions ("3072-dim gemini-embedding-001") → "the active embedding model (see `Orchestrator/embeddings/registry.py`; status at `GET /embeddings/status`)"; same touch-up in `.claude/commands/snapshot-dev.md` model-notes line.

Commit `test(embeddings): literal ratchets; drop inline-JSON fallback; docs follow registry`.

---

## Task 17: Final review, merge prep, live verification checklist

1. Full suite in worktree; `python -m Orchestrator.toolvault.validate`.
2. Dispatch final code-reviewer subagent over the whole branch diff (`git diff main...HEAD`).
3. Merge to main (explicit-path staging rules), push.
4. **Live verification on the appliance (prod runs from main checkout):**
   - `df -h` first — transcode needs ~1.5× 408MB free.
   - `sudo systemctl restart blackbox.service` (pre-authorized); journalctl: watch for transcode log lines (`migrated=6975`, index shrink report), then `[EMBEDDING]` on next mint.
   - EXPECTED on first boot only: ToolVault re-embeds all ~49 tool descriptions (`[TOOLVAULT-EMB] embedded=49 ...`, ~30s background) — the cache re-keys from the old model-id literal to the registry slug. This is the one-time slug re-key, NOT a failure; second boot is a no-op.
   - `ls -lh Manifest/snapshot_index.json` (~5MB) + `Manifest/embeddings/gemini-embedding-001/` populated; `.bak.pre-embeddings-v2` present.
   - Search probe: `search_snapshots` via chat for a known phrase → results sane, latency subjectively instant.
   - `curl :9091/embeddings/status | python3 -m json.tool` — active store, counts, health ok.
   - Wizard: `/onboarding/?step=embeddings` renders cards; updates panel shows no card (health ok).
   - Optional local-path proof: install Ollama, pull `qwen3-embedding:0.6b`, run a wizard-driven switch, verify cutover + switch-back delta backfill.
5. `/snapshot-dev` the session.
