# BlackBox Retrieval Upgrade — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> Design source of truth: `docs/plans/2026-07-01-retrieval-upgrade-spec-audit.md` (WI-1..WI-11,
> amendments A1–A13, Brandon's locked decisions). When this plan and the audit doc conflict, the
> audit doc wins — flag the conflict, don't improvise.

**Goal:** Perfect recallable memory — every fact in every snapshot is scorable (chunk-for-scoring),
every retrieved snapshot reaches the model whole (cap-free delivery within verified provider
windows), with real token math end-to-end.

**Architecture:** Chunk vectors at the embedding layer only (store schema v2, collapse-to-snapshot
inside the store); deliver whole snapshots budgeted by config.ini count knobs; one central
tokenization module feeds clamps, chunk sizing, and window guards; every behavior change is
flag-gated or store-schema-derived with a documented rollback.

**Tech Stack:** Python 3.12 (Orchestrator/venv), numpy flat-f32 vector stores, Ollama 0.30.8,
gemini-embedding-2 (active), tiktoken + HF `tokenizers` (new, vendored assets), pytest.

**North-star test for every review:** does this change ever cause a fact that exists in a snapshot
to (a) not be scorable, or (b) not reach the model when its snapshot is selected? If yes → reject.

**Standing rituals (every milestone):**
- TDD per task; run `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/ -k "embedding or retrieval or fossil or context" -v` before each commit.
- Commit explicit paths only (never `git add -A`); pre-commit secret grep.
- Adversarial review per milestone (superpowers:requesting-code-review) before push; this repo's reviews catch real bugs.
- Code changes need `sudo systemctl restart blackbox.service` (pre-authorized, ~90s warm-up); registry/schema edits alone go live via `POST /toolvault/reload`.
- Device-validate on real surfaces after service-touching milestones (MCP `search_snapshots`, in-app chat, one voice session, phone `/local/turn/prepare`).

---

## M0 — Baseline & prior-art capture (no behavior change)

**Files:** Commit: `benchmarks/digest_ab/` (explicit paths, exclude `__pycache__`), `benchmarks/README.md` (new, 5 lines: what digest_ab is, how to run).

1. `git status benchmarks/` — inventory; stage `run_ab.py`, `answers.py`, `artifacts.json`, `results.json` explicitly.
2. Run the retrieval suite once green: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/ -k "embedding or retrieval or fossil or context" -v` → record pass count in the commit body (this is the regression baseline).
3. Capture live baseline provenance: `curl -s localhost:9091/embeddings/status > docs/plans/artifacts/2026-07-01-baseline-embeddings-status.json` (create `docs/plans/artifacts/`).
4. Commit: `chore(retrieval-upgrade): commit digest_ab bench prior-art + baseline status (M0)`.

---

## M1 — WI-8 operator plumbing + WI-7b dedupe-with-backfill (early context-quality wins)

### Task 1.1: Operator into the 6 chat provider loops + CU driver

**Files:**
- Modify: `Orchestrator/routes/chat_routes.py:844, 1453, 2235, 2977, 4877, 5542` (the six `hybrid_retrieve(vol_txt, query, k=min(k,5))` calls)
- Modify: `Orchestrator/browser/driver_anthropic.py:420`
- Test: `Orchestrator/tests/test_chat_loop_operator_scoping.py` (new)

**Step 1 — failing test:** monkeypatch `Orchestrator.fossils.hybrid_retrieve` recording kwargs; drive each loop's tool-dispatch helper (extract the smallest callable per provider loop — if the loop body is not separately callable, test via the shared tool-dispatch function they all call; read the six call sites first and pick the ONE seam they share). Assert `operator` == the session operator, not `''`.
**Step 2:** run → FAIL (operator missing).
**Step 3:** plumb the session operator each loop already holds (each provider loop has the request's operator in scope — verify variable name per site) into the `hybrid_retrieve` call. CU driver: pass the CU session operator.
**Step 4:** run → PASS. Also assert the keyword channel now populates: with an index entry owned by the operator, `retrieve()` called via `hybrid_retrieve` yields keyword candidates (integration-style, live-store skip guard like `test_retrieval_golden.py:64-72`).
**Step 5:** Commit `fix(retrieval): scope chat/CU tool-loop memory search to the session operator (WI-8)`.

**Gotcha from the audit:** operator `''` today = semantic-unscoped AND keyword-dead. After this change scoped operators see ONLY their snapshots on these surfaces — that is the intended behavior change; note it in the commit body. `operator="system"` remains all-see.

### Task 1.2: WI-7b — dedupe-with-backfill in `build_fossil_context`

**Files:**
- Modify: `Orchestrator/context_builder.py:118-151`
- Test: `Orchestrator/tests/test_context_builder_backfill.py` (new)

**Step 1 — failing test (hermetic; monkeypatch the four retrievers):**
```python
def test_keyword_section_backfills_after_recent_dedupe(monkeypatch):
    # recent returns snaps A,B; keyword ranked list returns A,C,D,E (A dupes recent)
    # KF=3 -> keyword section must deliver C,D,E (3 items), NOT just C,D
    ...
    assert prov["keyword"] == ["SNAP-C", "SNAP-D", "SNAP-E"]

def test_no_duplicate_snap_id_across_sections(monkeypatch): ...
def test_semantic_backfills_against_recent_and_keyword(monkeypatch): ...
def test_channels_exhausted_returns_short_section(monkeypatch): ...  # never invents items
```
**Step 2:** run → FAIL (today keyword shrinks to 2).
**Step 3:** implement: over-fetch each channel (`fetch_k = section_k + len(seen_so_far)` is sufficient and cheap — recent is bounded by RF), then fill each section with the first `section_k` unseen snaps, preserving precedence recent → keyword → semantic. Decide checkpoints explicitly: checkpoints stay UN-deduped (they are a pinned section, not a discovery channel) — add a one-line comment saying so.
**Step 4:** run → PASS.
**Step 5:** sweep the sibling implementations: `chat_routes.py` `build_streaming_context` and `tasks.py:1404-1405` — same over-fetch+backfill; extend the test file to cover whichever of these is separately callable. Run full `-k context` suite.
**Step 6:** Commit `feat(context): cross-section dedupe now backfills from each channel's ranked list (WI-7b)`.

**Scope guard (do NOT touch):** `retrieve()`'s RRF channels — overlap there is agreement signal; fused top-k is already unique.

**Milestone gate:** adversarial review; restart; device-validate one chat turn + `/debug/context`; push.

---

## M2 — WI-11 central tokenization module

### Task 2.1: Module skeleton + calibrated floor fallback

**Files:**
- Create: `Orchestrator/tokenization.py`
- Test: `Orchestrator/tests/test_tokenization.py` (new)

**API (complete — this is the seam everything consumes):**
```python
# Orchestrator/tokenization.py
"""Central token accounting. ONE seam for every token count/clamp in the system.

Backends per model, resolved lazily; NEVER raises from count paths — exactness
degrades to the calibrated conservative floor (chars/2) rather than erroring.
Policy (audit WI-11): exact-LOCAL where free (tiktoken, HF tokenizers, both
vendored); exact-REMOTE (Gemini countTokens, Anthropic count_tokens) only from
explicitly-invoked preflight/calibration helpers — never in hot paths.
"""
CHARS_PER_TOKEN_FLOOR = 2.0  # measured: corpus mean 2.9, code-dense 2.12, hexdump 1.14

def estimate_tokens(text: str, model_key: str | None = None) -> int:
    """Fast, never-network, never-raise. Exact if a local tokenizer is vendored
    for model_key, else ceil(len(text)/CHARS_PER_TOKEN_FLOOR) (over-estimates =
    safe for clamping)."""

def clamp_to_tokens(text: str, max_tokens: int, model_key: str | None = None) -> tuple[str, int]:
    """Head-preserving clamp to <= max_tokens. Returns (text, est_tokens).
    Local-tokenizer path clamps exactly; floor path clamps to max_tokens*2 chars."""

def count_tokens_remote(text: str, model_key: str) -> int | None:
    """Exact remote count (Gemini countTokens / Anthropic count_tokens).
    EXPLICIT-CALL ONLY (preflight, calibration, WI-10 verification). None on any failure."""
```
**Step 1 — failing tests:** floor path over-estimates vs known strings; `clamp_to_tokens` result re-estimates ≤ max; empty/None-model paths; a `hexdump`-style string still clamps safely (floor 2.0 vs measured 1.14 — the clamp is by ESTIMATE so over-estimation shrinks output, never overflows the budget... assert `estimate_tokens(clamped) <= max_tokens` for the local path, and for the floor path assert `len(clamped) <= max_tokens * 2`).
**Step 2-4:** implement floor-only first; PASS.
**Step 5:** Commit `feat(tokenization): central token module, calibrated x2 floor (WI-11 part 1)`.

### Task 2.2: Vendored local backends (tiktoken + HF tokenizers) — offline-safe

**Files:**
- Modify: `Orchestrator/tokenization.py` (backend registry), `Orchestrator/requirements.txt` (tiktoken, tokenizers)
- Create: `Orchestrator/tokenizers_vendored/` (tiktoken BPE cache + Qwen/Gemma tokenizer.json files, fetched ONCE at build time by `scripts/vendor_tokenizers.py` — new)
- Test: extend `test_tokenization.py`

**Steps:** failing test asserting `estimate_tokens` for a known Qwen string equals the live `prompt_eval_count` reference (record the reference constant from one manual `/api/embed` probe run and pin it in the test); implement lazy backend loading with `TIKTOKEN_CACHE_DIR`/local-files-only paths; **offline test:** monkeypatch sockets off (`socket.socket = raiser`) and assert local backends + floor still work. Map registry slugs → backend in ONE table inside `tokenization.py`, and add a registry key `tokenizer` per embedding-model entry (`registry.py`) so the Task-16 guard covers it. Commit `feat(tokenization): vendored tiktoken+HF backends, offline-verified (WI-11 part 2)`.

### Task 2.3: Remote counters (explicit-only)

`count_tokens_remote` for Gemini (`countTokens`, live-verified in audit) and Anthropic (`count_tokens`); tests use fakes asserting (a) hot-path functions NEVER call them (spy), (b) failures return None. Commit.

**Milestone gate:** review; no service behavior changed (module unused yet); push.

---

## M3 — WI-10 verification half: provider window audit (measurement only)

**Files:**
- Create: `scripts/audit_provider_windows.py` (probe harness), `docs/plans/artifacts/2026-07-XX-provider-window-audit.md` (results table)
- Test: none (ops script) — but the script must be re-runnable and idempotent.

1. Doc pass: for every model in `config.ini`/model selectors actually in use (chat: `claude-opus-4-7` default + OpenAI + Gemini + Grok; 3 voice models; CU models; on-device Gemma), record documented context window + max output. Sources: provider docs/discovery endpoints (provider-API-as-SoT rule).
2. Live probes via the existing chat paths (NOT raw API — the point is our transport): assemble a synthetic context at 3 sizes (75k chars = today's anthropic cap, 210k = Brandon's 2026-04-25 stall repro, worst-case 238k) and drive `/chat/stream` per provider; record complete/stall + TTFB + total time.
3. Reconcile `local` profile: `PROVIDER_CAPS["local"]=16000` comment says 16K window; the on-device engine default is 6144 ctx (2026-06-27 work). Record the true device window from the Android engine config; the number feeds M8.
4. Deliverable: the table + a one-paragraph per-provider verdict (pass cap-free / needs transport hardening / hard window). Commit artifacts.

**No code changes to caps in this milestone.**

---

## M4 — WI-6 Phase A: eval harness + labeled set + baseline numbers

### Task 4.1: Candidate-store eval seam

**Files:**
- Modify: `Orchestrator/retrieval.py` (optional `store=None` param, default `get_active_store()` — 3-line change), `Orchestrator/embeddings/search.py` (nothing — `swap_active` stays as-is)
- Test: `Orchestrator/tests/test_retrieval_store_override.py`

Failing test: `retrieve(q, store=fake_store)` scores against the override and NEVER touches active.json. Implement; PASS; commit. (This is the seam that makes "swap only after golden passes" executable — audit blocker.)

### Task 4.2: Stratified labeled set builder

**Files:**
- Create: `eval/build_labeled_set.py`, `eval/labeled_set.jsonl` (generated artifact, committed)
- Reuse: `benchmarks/digest_ab/run_ab.py:88-98` query-gen prompts (import, don't fork)

Per audit A10 — encode ALL of this in the script, not in operator memory:
- Sample ~500 snapshots stratified by length band (<6k / 6–10k / >10k chars — OVERSAMPLE >10k), age, operator.
- Query generated from a RANDOM 1–2k-char span at a random offset (span position head/middle/tail recorded per item).
- ≥10% flagged for hand validation (`"validate": true` rows; the run report counts them).
- Holdout: the 3 human pairs from `test_retrieval_golden.py:57-61` + any real recent queries, tagged `"source": "holdout"`.

### Task 4.3: Bench runner through retrieve()

**Files:** Create: `eval/run_bench.py` — for each labeled row call `retrieve(query, operator, k=10, store=<arm's store>)`; emit recall@1/3/5/10 + MRR, stratified by length band AND span position; artifacts-cached like digest_ab. Run the **whole-snapshot baseline** on gemini-embedding-2 + qwen3-0.6b; commit `eval/` + baseline results. **These two baseline numbers gate M6.**

**Milestone gate:** review; push. (Query-gen cost ≈ 500 Haiku calls; span protocol per A10 guards label leakage.)

---

## M5 — WI-1 mode 1: token-aware clamp (never raise)

### Task 5.1: Registry `max_input_tokens` + clamp in providers

**Files:**
- Modify: `Orchestrator/embeddings/registry.py` (add per-model `max_input_tokens`: gemini-001 **2048**, gemini-2 **8192**, openai-3-large **8191**, qwen 0.6b/8b **32768 with comment: effective = min(this, num_ctx we send)**; delete `EMBEDDING_MAX_CHARS` only AFTER all consumers below are swept)
- Modify: `Orchestrator/embeddings/providers.py` (`_truncate` → `tokenization.clamp_to_tokens(text, entry_max_tokens_with_margin, model_key)`; margin = 10% undershoot; applies to BOTH purposes — queries stay clamped forever per audit)
- Test: extend `Orchestrator/tests/test_embeddings_providers.py`; update `test_embeddings_registry.py:63`

**Steps:** failing tests — (a) every registry entry declares `max_input_tokens` (mirror the existing `test_every_model_declares_explicit_semantic_threshold` pattern at `test_embeddings_registry.py:80`); (b) a 91k-char doc under the qwen slug clamps to ≤ budget (estimate-verified); (c) QUERY purpose clamps and never raises; (d) Ollama query_instruction prefix counted INSIDE the budget (clamp before prefix must leave room — compute `budget - estimate(instruction)`); (e) telemetry line `[EMBEDDING] clamped ...` emitted on clamp. Implement; PASS.

### Task 5.2: Ollama num_ctx + truncate:false

**Files:** `providers.py` OllamaProvider payload: `options: {num_ctx: <entry budget+margin>}`, `truncate: false`.
Failing test (fake HTTP): payload carries both keys. Live acceptance (manual, recorded in commit body): re-run the audit's 91k-char probe — expect `prompt_eval_count` ≈ full input under num_ctx, and a 400 (not silent cut) when over. **This closes the live 4,095-token silent-truncation hole found in the audit.**

### Task 5.3: EMBEDDING_MAX_CHARS consumer sweep + delete

Sweep (from audit): `benchmarks/digest_ab/run_ab.py:62,159-163` (switch to tokenization module), reliance comments `chat_routes.py:142` and `tasks.py:1537` (reword: "embedding layer clamps token-aware via tokenization.py"), `tasks.py:1920` digest note, `test_tool_selection_full_prompt.py`, `test_embeddings_providers.py:270-285`. Then delete the constant from `registry.py`. Full suite green. Commit.

**Explicitly deferred:** fail-loud mode (raises on over-limit snapshot documents) — activates in M6 AFTER the chunker exists, documents-only. Landing it now would zero-recall 30.9% of the corpus (audit finding).

**Milestone gate:** review; restart; verify a mint embeds clean (`journalctl` `[EMBEDDING] Successfully generated`), ToolVault `sync_embeddings` green, watcher probe green (shared-seam regression per audit A7); push.

---

## M6 — WI-2: chunk-for-scoring, store schema v2 (the core)

> Execute strictly in order 6a → 6f. Mint-path chunking (6c) MUST land before any backfill (6f) — audit A4.

### Task 6a: Store schema v2 — meta, ordinals, group append, collapse-in-search

**Files:**
- Modify: `Orchestrator/embeddings/store.py`
- Test: `Orchestrator/tests/test_embeddings_store_v2.py` (new; keep the 39 existing store tests green untouched)

Failing tests FIRST (all hermetic, tmp_path stores):
```python
def test_v2_meta_fields():          # schema:2, rows, snapshots, generation present; v1 meta absent-schema => 1
def test_group_append_atomic():     # append_group("SNAP-X", [v0,v1,v2]) -> 3 rows, ids all "SNAP-X", ordinals [0,1,2]
def test_group_append_idempotent(): # re-append same snap_id group -> 0 written (whole-group skip)
def test_chunks_never_span_batches():  # append_many with mixed groups keeps each group in one lock hold
def test_search_collapses_to_unique_snapshots():
    # 5-chunk snapshot occupying raw ranks 1-3 returns ONCE, best score, best-chunk vector; k unique snaps
def test_collapse_covers_plain_search():   # store.search too (semantic_search/calibrate consumers)
def test_allowed_ids_scoping_on_chunked_store():  # bare-snap_id allowed_ids still filters correctly
def test_missing_is_snapshot_currency():   # ids()/missing() return DISTINCT snap_ids on v2
def test_self_heal_truncates_three_files_and_drops_partial_group():
    # torn write -> whole trailing group healed away -> snap_id reported missing again
def test_generation_bumps_on_append_and_heal():
def test_v1_store_reads_unchanged():       # dual-schema: v1 dir opens, searches, appends exactly as today
```
Implement (design fixed by audit A1–A3, A5): `ordinals.json` sidecar (parallel array); meta gains `{schema, rows, snapshots, generation}`; `append_group(snap_id, vectors)` + group-aware `append_many`; dedupe key stays bare snap_id meaning "full group present"; write order vectors→ids→ordinals with the 3-file min() self-heal PLUS trailing-partial-group drop (ordinal contiguity check); `search`/`search_with_vectors` dedupe by snap_id during argsort descent (first hit = max cosine = best chunk) — **collapse lives HERE, `retrieve()` lines 150–153 stay untouched**; `count` property = rows, new `snapshots` property; v1 stores (schema absent) behave byte-identically (regression: run the existing store test file unmodified).

Commit per green test cluster (3–4 commits).

### Task 6b: Snapshot chunker

**Files:** Create: `Orchestrator/embeddings/chunker.py`; Test: `Orchestrator/tests/test_chunker.py`.
`chunk_snapshot(text) -> list[str]`: window = `[retrieval] chunk_tokens` (default **1024**) via `tokenization.clamp/estimate` (×2 floor ⇒ ~2,048-char windows), overlap `[retrieval] chunk_overlap_pct` (default 15). Tests: short text → 1 chunk (identity); every chunk ≤ budget by estimate; overlap correct; deterministic. **Snapshot-only helper — NEVER wired into `providers.embed` or `generate_embedding_sync`** (ToolVault/probe/query protection, audit A7). Commit.

### Task 6c: Mint path chunks + schema guard at the vector seam

**Files:**
- Modify: `Orchestrator/fossils.py:478-504` (update_snapshot_index vector block), `Orchestrator/checkpoint.py` (3 sites: 153/160, 276/290, 364/371 — swap single-embed for the new helper), `Orchestrator/embeddings/search.py` (new `embed_snapshot_chunks(text) -> list[vec] | None`)
- Test: `Orchestrator/tests/test_embeddings_mint_v2.py`

Failing tests: mint against a v2 store lands a full chunk group (N rows, ordinals contiguous); mint against a v1 store lands exactly one row (schema-derived behavior, NOT flag-derived — audit A6 rollback safety); **schema guard:** a single whole-snapshot vector arriving at a v2 store is DROPPED with the existing catch-up log semantics (mirror of the dims-mismatch branch `fossils.py:493-496`); mint never raises on vector-layer failure (existing contract). Implement; PASS; commit. Restart + live-verify one real mint (`journalctl` group-append line).

### Task 6d: Migrate rebuild mode + gap-heal chunk batching + CLI liveness guard

**Files:**
- Modify: `Orchestrator/embeddings/migrate.py` (rebuild job: `target_schema=2`, builds under `{stores}/_build/{slug}` via the existing `base_dir` param; `activate=false` build-only mode PERSISTED in migration_state.json so boot resume stays build-only; batching snapshot-granular, chunks flattened for provider.embed and regrouped for group-append; `preexisting/raced` set algebra in snapshot currency)
- Modify: `Orchestrator/embeddings/watcher.py:342-381` (gap-heal: sub-batch sized in CHUNKS ≤ migrate's batch budget, group appends)
- Modify: `Orchestrator/backfill_embeddings.py` (service-liveness probe `GET localhost:9091` → refuse without `--force`)
- Test: `Orchestrator/tests/test_embeddings_migrate_v2.py`

Failing tests: rebuild converges (missing → empty in snapshot currency) on a fake 3-snapshot corpus with multi-chunk texts; **build-only never touches active.json**; boot-resume of a build-only job stays build-only; a raced bare mint row does NOT block chunk-completion (group-skip keys on full group — the racing row is dropped by 6c's schema guard instead); gap-heal splits 50 snapshots into chunk-budgeted provider calls. Implement; PASS; commit.

### Task 6e: Status + watcher currency

**Files:** `Orchestrator/routes/embeddings_routes.py` (per-store payload ADDS `rows`, `schema`; `count` STAYS snapshot-currency — binding contract), `watcher.py:304-309` (`_pick_migration_target` sorts by `snapshots` not rows). Tests in `test_embeddings_routes.py`. Commit. Portal/Android cards need no change (additive), verify rendering manually post-restart.

### Task 6f: Build, calibrate, gate, cutover

Runbook (each step recorded in `docs/plans/artifacts/`):
1. `POST /embeddings/migrate` rebuild gemini-embedding-2 → `_build` (cloud, ~$2–3, <1h). Mints during build are fine: v1 active gets them live; the re-diff loop picks them up for the candidate; racing bare rows into v2 are dropped by the 6c guard and re-diffed.
2. Update `scripts/calibrate_threshold.py` to accept a store override + report relevance AND noise bands on collapsed chunk-max scores; run against candidate (feeds M9 values).
3. `eval/run_bench.py` chunk arm against candidate via the M4 store-override seam; **gates:** recall@10 ≥ whole-snapshot baseline overall AND in the >10k band (the truncation band must IMPROVE); `test_retrieval_golden.py` + `test_local_lean_retrieval.py` run against the candidate (override fixture) — hard gates.
4. Cutover (explicit, no auto): stop-service dir-swap v1→`{slug}.pre-chunk`, candidate→live (v1 path per audit A5), restart, re-run golden live, device-validate all four surfaces.
5. Rollback runbook (commit into the audit doc): flip dirs back + restart + IMMEDIATELY `POST /embeddings/migrate` diff-fill for the mint gap (never wait on 50/day heal).
6. Enable WI-1 fail-loud mode for snapshot documents (now unreachable via chunker; loud beats silent).
7. Prune note: the rebuild diffs against the live index by construction → the 305 orphan ids simply don't transfer.

**Milestone gate:** full suite + bench report + adversarial review + push. Non-active stores are NOT rebuilt (rollback assets, audit A6).

---

## M7 — WI-10 cap-removal half + transport hardening

### Task 7.1: Transport hardening (prerequisite)

**Files:** Android SSE client (OkHttp read-timeout raise + heartbeat handling), Portal stream consumer, voice route stream plumbing — exact sites from the M3 audit's stall findings; server heartbeat comments/keepalives on `/chat/stream` if absent.
Test: the M3 210k repro script per provider → all complete. **No cap changes until each provider's repro is green.**

### Task 7.2: Remove delivery truncation for verified providers

**Files:**
- Modify: `Orchestrator/context_builder.py` (`cap_chars`/`max_fossil_chars` → whole snapshots; `PROVIDER_CAPS` → `window_guard(provider) = verified_window_tokens - response_headroom` via WI-11 token math + M3 table; guard binds by DROPPING the lowest-ranked snapshot WHOLE + log line — never mid-snapshot truncation)
- Modify: the 3,000-char truncations in 6 chat loops + 3 voice routes; `ToolVault/tools/search_snapshots/executor.py:33-34` 10k cap; `/fossil/hybrid` snippet stays (it's an ID/preview API, not model delivery — document that)
- Test: `Orchestrator/tests/test_delivery_uncapped.py` — a 30k-char fossil reaches the assembled context intact for a verified provider; guard drops lowest-ranked-whole when budget exceeded; `local` profile unchanged.

Config: count knobs (RF/KF/SF/CP) remain THE budget — untouched. Commit per surface cluster; device-validate a long-snapshot chat turn on each provider; push.

---

## M8 — WI-7a matched-chunk windowing (window-bound profiles only)

**Files:** `Orchestrator/retrieval.py` (return best-chunk ordinal/offset in provenance when store is v2 — additive), `Orchestrator/local_provider/` `/local/turn/prepare` delivery + any provider still under an M7 interim guard; Test: `test_local_lean_windowing.py`.
Failing test: phone-lean delivery of an over-budget snapshot contains the best-matching chunk's window (not the head). Uses the true device window from M3 recon (6144 vs 16k reconciliation). Commit; device-validate on the Fold.

---

## M9 — WI-3 registry junk_floor (measured, flag-gated)

**Files:** `registry.py` (nullable `junk_floor` per model — values FROM 6f calibration noise bands: gemini-2 ≈0.50, qwen-0.6b ≈0.35–0.40 as starting points, re-measured post-chunk), `retrieval.py` (precedence: registry if non-null else `[retrieval].junk_floor`, behind `[retrieval] registry_floor_enabled` fallback false), dead-plumbing cleanup (`semantic_retrieve` unused `threshold` param + 3 `active_threshold` call sites → delete or mark display-only, ONE approach), `calibrate_threshold.py` docstring names which field it feeds.
Tests: precedence matrix; flag-off = byte-identical results; **phone-lean non-empty on each local model with the floor active** (the audit's wipe scenario as a permanent regression test). Commit; review; push.

---

## M10 — WI-9 hardware probe + placement toggles

### Task 10.1: Host hardware probe
**Files:** Create `Orchestrator/hardware.py` (`probe() -> {gpu: bool, gpu_name, vram_mb, ram_mb}` via nvidia-smi/lspci/meminfo, cached, fail-soft) + tests (monkeypatched command outputs: no-GPU box, 8GB box, 16GB box).

### Task 10.2: VRAM-aware preflight + placement
**Files:** `embeddings_routes.py:_model_preflight` (+ reranker entries when M11 lands): VRAM budget arithmetic (Q4-8B ≈6–7GB resident, reranker 0.6B FP16 ≈1.2GB / 4B Q4 ≈2.5GB) → `recommended_placement`; per-slug `placement.json` beside stores (mirror keep_alive.json helpers `store.py:394-435`); OllamaProvider honors placement (`options.num_gpu: 0` pins CPU) — **num_ctx sent on BOTH placements** (audit: VRAM-tiered defaults change with device). Status payload gains `hardware` + per-model `placement`/`recommended_placement` (ADDITIVE).
Tests: no-GPU → every model offers CPU path; 8GB fixture → embedder-GPU/reranker-CPU recommendation; toggle persists + takes effect without restart.

### Task 10.3: Surfaces
Wizard step (onboarding rollup already carries embeddings status), Portal updates card, Android card — render gate + toggles (3-surfaces rule; contracts additive). Device-validate. Commit per surface; review; push.

---

## M11 — WI-4 reranker (post-GPU install; provider abstraction may land early)

Blocked on: RTX 2000 Ada 16GB installed. Pre-GPU allowed: provider abstraction + registry entries + tests with fakes.

**Files:** Create `Orchestrator/rerank.py` (provider-abstracted: primary vLLM `/score` with `Qwen3ForSequenceClassification` hf_overrides + capped `gpu_memory_utilization` 0.15–0.25; eval fallback in-process transformers; scores validated once against the transformers reference — vLLM fidelity bugs); Modify `retrieval.py` (insert stage: rerank top `min(rerank_candidate_n, len(fused))` → map ORDER to rank-space `1/(rrf_c+new_rank)` → RE-APPLY `apply_recency_tiebreak` → rebuild `score_by_id` → MMR unchanged); keyword-only candidates: decode + chunk on the fly, rerank best-lexical chunk — every pool member scored on ONE scale; Config `[retrieval] rerank_enabled=false`, `rerank_candidate_n=40`.
Gating: flag AND provider availability AND startup latency preflight (1-candidate probe < 500ms against the WI-9-chosen placement).
Tests: flag-off byte-identical (the audit's acceptance); fresh-beats-stale near-tie preserved with rerank on; near-duplicate suppression unchanged; keyword exact-string match retains top-k. Then WI-6 Phase B eval (quant-8B arms) decides default. Review; push.

---

## M12 — WI-5 seam documentation (no code)

Append to the audit doc: ANN sidecar invalidation key `(slug, schema, generation, rows)` (fields exist since 6a); exact-vs-ANN gate MUST include operator-scoped queries (post-scoring filter needs oversampling under ANN); compaction = full-rebuild event. Commit doc.

---

## Risks & rollback (top line — full table in audit doc)

| Risk | Mitigation |
|---|---|
| Chunk store regresses recall | M4 baseline + 6f gates (incl. >10k band) BEFORE cutover; v1 dir retained; rollback runbook + diff-fill |
| Cap removal re-triggers the Opus TTFB stall | M7 transport hardening gated per-provider on the 210k repro; interim guards stay until green |
| Fail-loud fires in shared seams | clamp-not-raise at providers.embed; fail-loud scoped to snapshot pipeline, post-chunker only |
| Qwen floor wipes phone recall | M9 flag default-off + measured noise floors + permanent lean-profile regression test |
| Mint-during-migration pollution | 6c schema guard lands before 6f backfill; re-diff loop; raced rows dropped not absorbed |
| OOM pressure from bigger matrix | 2.52× ≈ 235MB (measured corpus math); leak-hunt is a separate track — monitor after cutover |

---

## As-built addendum (2026-07-02, mid-execution — corrections where the build diverged from plan text)

- **M2/M5 naming + values:** the mint embed helper shipped as `embed_snapshot_for_index(text) -> dict`
  (schema-aware payload: `{"embedding": vec}` v1 / `{"chunk_vectors": [...]}` v2), not the planned
  `embed_snapshot_chunks`. Qwen registry `max_input_tokens` shipped as **8192** (not 32768):
  num_ctx KV at 32k ≈ 3.7GB CPU RAM per loaded model; raise post-GPU (comment on the entries).
- **M4b (not in the original plan):** the eval harness exposed a production ranking miscalibration —
  `recency_weight` 0.05 was ~7.6× the post-RRF relevance span, displacing older golds
  (57.6% of semantic misses had gold in raw top-10). Fixed to **0.005** (measured sweep,
  eval/results/2026-07-02-recency-sweep.md): r@10 0.268→0.489 hybrid. All M6 gate baselines are
  the w=0.005 rows; the gate REFUSES to run at any other live weight.
- **M6 store decisions locked by review:** v2 `meta.count` stays SNAPSHOT currency (rows is the
  new field) — plan line "count = rows" superseded by audit A11. `append_group` on a v1 store
  raises. Fresh stores default v1 until the post-gate default flip (board task: model-switch path
  creates v2 by default AFTER the 6f gate passes — Brandon-confirmed intent).
- **M6d hardening (post-review):** `_run_engine` is schema-aware at the fill site (v2 targets get
  chunk groups on ANY migration path incl. watcher auto-recovery — the A4 side door closed);
  `start_rebuild(slug)` + `POST /embeddings/migrate {"rebuild": true}` = in-service build-only.
- **6f gate tooling (exact commands):**
  `Orchestrator/venv/bin/python scripts/calibrate_threshold.py --store-dir Manifest/embeddings/_build --schema 2`
  `Orchestrator/venv/bin/python eval/run_bench.py --gate --candidate-dir Manifest/embeddings/_build --candidate-slug gemini-embedding-2`
  (gate exit 0 = cutover authorized; re-runs baseline arms fresh at current config; goldens+lean
  run against the candidate via an in-process get_active_store patch; skips count as failures).
- **M7.1a (pulled forward, safely decoupled):** Android SSE hardening landed pre-cutover
  (9b83714) — discovery: the 2026-04-25 stall had been "fixed" with an INFINITE read timeout
  (dead sockets hung chat forever, no watchdog). Now: stream client read 300s (= server
  provider-leg httpx timeout), connect 10s, comment-frame tolerance, test-pinned. Device
  validation on the Fold at the M7 gate.
- **M12 shipped early** (audit doc §7): ANN seam contract — pure docs, no code.
