# Pluggable Snapshot Embeddings — Design

**Date:** 2026-06-11
**Status:** Validated with Brandon (brainstorming session)
**Goal:** Replace the hardcoded `gemini-embedding-001` snapshot-embedding pipeline with a pluggable provider layer — cloud (Gemini, OpenAI) and local (Ollama/Qwen3) — selectable in the onboarding wizard and switchable on the fly, with a seamless background re-embed/cutover flow and vendor-deprecation self-defense.

---

## 1. Problem

Embedding models keep churning (`text-embedding-004` → `gemini-embedding-001` was a forced migration; the next one is a matter of time). Today the model is hardcoded in four places and the system has no concept of "which model made this vector":

- `Orchestrator/monitoring.py:347` — `generate_embedding()` hardcodes `models/gemini-embedding-001`; also used for **queries** (with `task_type="retrieval_document"` — a quirk; queries should use `retrieval_query`).
- `Orchestrator/checkpoint.py` — 3 mint-time call sites.
- `Orchestrator/backfill_embeddings.py` — standalone CLI script with its own copy of the function; detects wrong-dim vectors, resumable. (Proof the diff-and-fill pattern works; this design productizes it.)
- `Orchestrator/toolvault/config.py` — separate `EMBEDDING_MODEL` constant for tool-description embeddings.

Storage is the bigger liability: all 6,975 vectors live **inline in `Manifest/snapshot_index.json` as JSON floats — 408MB** (the conversation volume itself is 35MB). Every mint rewrites the whole file; every search brute-forces a pure-Python cosine loop (~21M float ops, seconds per query). Vectors carry no model metadata — dimension count is the only fingerprint, and `cosine_similarity()` silently returns 0.0 on a length mismatch, so model drift would look like "no results," not an error.

Customers also need a **local** option: many will run fully offline (Brandon's bet: "my guess is everyone will do it local"), and quality-vs-RAM should be their dial.

## 2. Decisions (locked with Brandon)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Local inference runtime | **Ollama** (system service; manages model pulls, GGUF quantization, RAM load/unload; REST `/api/embed`). Matches the PTY-bridge philosophy: bridge the real upstream tool, inherit its updates. |
| 2 | Model menu | **Gemini cloud** (auto-tracked, see §7), **OpenAI cloud** (`text-embedding-3-large`, 3072-dim), **Qwen3-Embedding-0.6B** local (1024-dim, ~0.7GB RAM), **Qwen3-Embedding-8B** local (4096-dim, ~6GB RAM, MTEB #1 open-source). No Anthropic option — Anthropic has no embeddings API. Registry is data; adding e.g. Qwen3-4B or `-small` later is one entry. |
| 3 | Switch/migration flow | **Background job + atomic cutover.** Per-model stores are **persistent — never deleted on switch**. Switching back to a previously used model = delta backfill of snapshots minted since, then cutover (minutes, not hours). |
| 4 | Vendor auto-update | **Auto only when forced.** New model available → Portal banner + one-click migrate (no surprise BYOK spend). Current model hard-broken (delisted/erroring, search about to die) → auto-migrate as self-preservation, then report what happened. |

## 3. Storage: per-model binary vector stores

Vectors move out of `snapshot_index.json` entirely:

```
Manifest/embeddings/
  gemini-embedding-001/
    vectors.f32     # raw little-endian float32, N rows × dims — memory-mapped by numpy
    ids.json        # ordered snap_ids; row i ↔ vector i
    meta.json       # {provider, model_id, dims, normalized: true, count, created, last_updated}
  qwen3-embedding-8b/
    ...
```

- **Active store** named by `Manifest/embeddings/active.json` (runtime state, like `operator_state.json`; atomic write). `config.ini [embeddings] active` is only the first-boot default seed - configparser rewrites would destroy customer comments, so code never writes config.ini. Cutover = atomic `active.json` write + in-memory swap.
- Vectors are **L2-normalized at write time** → cosine similarity becomes a single numpy mat-vec dot product over the mmapped array (milliseconds; ~28–114MB RAM, active store only).
- **Append protocol:** append vector row, fsync, then append id. On startup, if `len(ids) != rows`, truncate both to `min` — self-healing, no torn state.
- `snapshot_index.json` keeps byte offsets/operator/timestamps and **shrinks 408MB → ~5MB**; mint no longer rewrites 408MB of JSON.
- **Day-one transcode:** the existing 6,975 JSON vectors are losslessly converted into the `gemini-embedding-001` store. Zero API calls, zero re-embedding to adopt the layout.
- Disk math: all four stores fully populated ≈ 320MB total — less than today's single JSON index.

## 4. Provider layer

New module `Orchestrator/embeddings/`:

```
providers.py   # GeminiProvider, OpenAIProvider, OllamaProvider — one interface:
               #   async embed(texts: list[str], purpose: "document"|"query") -> list[list[float]]
registry.py    # EMBEDDING_MODELS: dict[slug -> {provider, model_id, dims, ram_gb, cost_per_1m,
               #   quality_note, query_instruction, keep_alive}]  — data, not code (CU_MODEL_FILTERS pattern)
store.py       # VectorStore: open/append/diff/search/transcode
search.py      # semantic_search(query, operator, k) — replaces monitoring.py implementation
migrate.py     # diff-and-fill job (§6)
watcher.py     # daily catalog/health check (§7)
```

- `purpose` maps to Gemini `task_type` (`retrieval_document` vs `retrieval_query` — fixes the existing quirk), to Qwen3's instruction prefix (instruction-aware models gain 1–5% retrieval), and is a no-op for OpenAI.
- `monitoring.generate_embedding` / `semantic_search` become thin delegates to this module (all existing call sites keep working); `backfill_embeddings.py` is superseded by the migration job and reduced to a CLI wrapper around it.
- **ToolVault** switches to the shared provider layer. Its `embeddings.json` cache key gains the model slug; at cutover its ~100 descriptions re-embed in seconds.
- Cloud keys come from existing BYOK onboarding credentials. Embedding model choice is **box-global**, not per-operator.

## 5. Mint path

At mint, embed with the **active** model and append to the active store. If the provider is down (Ollama stopped, key missing), mint proceeds vector-less exactly as today — gaps self-heal: the daily watcher runs a small delta fill on the active store, and any migration/backfill closes gaps by construction.

## 6. Migration job — one job, two modes

"Switch models" and "backfill after switching back" are the same operation: **diff-and-fill**.

1. Diff target store's `ids.json` against the snapshot index → missing snap_ids.
2. For each: read text from the volume by byte offset, embed (`purpose="document"`), append. Rate-limited for cloud; sequential for local CPU.
3. **Catch-up loop:** new mints land in the *active* store during the job, so re-diff and fill until the delta is empty.
4. **Atomic cutover:** atomic `active.json` write + in-memory store swap. Old store untouched → rollback = flip back.

Resumable by construction — progress *is* the store contents; a restart re-diffs and continues. Search never goes dark (old model serves until cutover).

API: `POST /embeddings/migrate {"target": slug}` (409 if a job is running; explicit cancel-and-replace), `GET /embeddings/status` (active store, job progress/ETA, per-store freshness = missing-count), `POST /embeddings/migrate/cancel`. Runs on the existing APScheduler/asyncio infra (remember the uvloop `run_coroutine_threadsafe` pattern).

Scale check: 7K snapshots → Gemini/OpenAI ≈ 20–30 min rate-limited (≈ $2–3 on OpenAI-large, similar on Gemini); Qwen3-0.6B CPU ≈ 15–30 min; Qwen3-8B CPU ≈ hours — fine, it's background + resumable, and the wizard card says so.

## 7. Watcher — "auto only when forced"

Daily scheduled task, CU-live-catalog pattern:

- **Gemini:** `ListModels`, filter `supportedGenerationMethods` contains `embedContent` → is the configured model still listed? Is there a successor (newer non-preview embedding model)?
- **OpenAI:** models list, same logic. **Ollama:** local probe (service up, model present).
- Plus a 1-token live probe of the **active** model.

Health states drive behavior:

| State | Meaning | Action |
|-------|---------|--------|
| `ok` | active model fine | also: heal small vector gaps in active store |
| `superseded` | newer model exists, current works | Portal banner + one-click migrate; never auto-spends |
| `broken` | active model delisted/erroring — search dying | **auto-migrate** (self-preservation): prefer unambiguous vendor successor, else fall back to a local model; then notify the operator with what/why |

State exposed in `GET /embeddings/status`; banner rendered by Portal (and wizard).

## 8. Surfaces: wizard owns everything; update sections notify and deep-link

Placement rules (Brandon, 2026-06-11):

- **Onboarding wizard = the single management surface.** A new "embeddings" step (`Portal/onboarding/steps/embeddings.js` + `StepName`/`ALL_STEPS` in `Orchestrator/onboarding/state.py`) holds the full module: model cards (quality / dims / RAM / cost / privacy), Ollama presence check + model pull with streamed progress, BYOK probe validation, per-store freshness table, the backfill/switch action ("Backfill 312 snapshots (~4 min) and switch" when a store already exists), and live migration progress. The wizard supports post-onboarding re-entry via deep link `/onboarding/?step=embeddings` (`onboarding.js` already parses `location.search`).
- **Portal update section** (menu modal `.updates-section`, `Portal/modules/updates-manager.js`): gains an embeddings notification card driven by `GET /embeddings/status` - rendered for `superseded` / `broken` health states and while a migration is running - with a button that deep-links to the wizard step.
- **Android update section** (`ui/updates/UpdatesScreen.kt`, already documented as "parallel to updates-manager.js"): the same card with the same states; tapping opens `{origin}/onboarding/?step=embeddings`.
- Update notifications always *direct back to the wizard*; neither update panel embeds its own picker.

## 9. Error handling

- Dim mismatch on append → hard error (store metadata is authoritative); never silently store wrong-dim vectors. The silent `cosine_similarity` 0.0-on-mismatch path dies with the JSON layout.
- Provider failures: mint-time → vector-less mint + self-heal (§5); query-time → existing keyword-search fallback unchanged; migration-time → retry w/ backoff, park job in `stalled` with the error surfaced in status.
- Ollama OOM (8B on small box): wizard card states the RAM requirement; preflight-style check before pull (free RAM vs model requirement) with a customer-facing remediation string — same UX contract as `/cu/preflight`.
- Mid-migration restart: covered (resumable); mid-transcode restart: transcode writes to a temp dir, renamed atomically.

## 10. Testing

- **Golden parity:** transcoded binary store returns the same top-k (same scores within float tolerance) as the legacy JSON path for a fixture query set — gate before deleting inline embeddings.
- Store unit tests: append/fsync-truncate self-heal, diff, mmap search correctness vs naive cosine.
- Provider tests: mocked HTTP per provider; `purpose` mapping (Gemini task_type, Qwen3 instruction prefix).
- Migration: resume-after-kill, catch-up-loop convergence with concurrent mints, cancel-and-replace, cutover atomicity, switch-back delta path.
- Watcher: state machine (ok/superseded/broken) against canned catalog responses; broken-path target selection.
- Guard tests (CU pattern): no embedding-model literals outside `registry.py`/config; registry entries carry all required fields.

## 11. Out of scope (v1)

- ANN indexes (FAISS/HNSW) — brute-force numpy is milliseconds to ~100K snapshots; revisit at 10× growth.
- Reranking, Voyage AI, per-operator models, MRL dimension tuning (registry leaves room: a future entry could pin `dims` below a model's native size).
- Re-embedding media captions / code_embeddings.json — separate consumers, separate pass.
- Android switching UI.
