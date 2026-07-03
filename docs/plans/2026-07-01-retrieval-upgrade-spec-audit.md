# BlackBox Retrieval Upgrade Spec — Grounding Audit

**Date:** 2026-07-01
**Input:** `blackbox_retrieval_upgrade_spec.md` (Brandon's NAS, `smb://192.168.1.155/brandon/brandon/`)
**Method:** 7 parallel grounding verifiers over the live repo/config/corpus/external docs + 4 adversarial
reviewers attacking each work item with the grounded facts + live empirical probes (Ollama truncation,
CPU embed throughput, corpus distribution). All file:line references verified against the working tree.

---

## 1. Verdict

**The core architecture is right and the spec is buildable — after corrections.**
Chunk-for-scoring / deliver-whole-snapshot slots into `retrieve()` cleanly, the seven invariants are
the correct ones, and the WI-6-first sequencing is sound. But the spec was written against a stale
snapshot of the system: its two "verified" claims that drive the WI-1 and WI-2 designs are **refuted**
(Gemini `AUTO_TRUNCATE=false` is not implementable on our API path; stores are *not* keyed by
slug+dims+schema-version — no schema versioning exists at all), its model table omits the **active**
embedding model, and its scale numbers are wrong in both directions (corpus 10× bigger, chunk
multiplier 4× smaller). Four blocker-class design gaps would each have produced a silent-recall
regression — the exact failure class the spec exists to eliminate.

Nothing found kills the project. Everything found is fixable in the spec before code is written.

---

## 2. Grounded reality vs. the spec (the numbers that changed)

| Topic | Spec says | Grounded reality |
|---|---|---|
| Active model | table lists `gemini-embedding-001` (2,048 tok) | **`gemini-embedding-2`** (`Manifest/embeddings/active.json`), input limit **8,192 tokens**, threshold 0.55 calibrated 2026-06-21 |
| Binding token constraint | Gemini 2,048 | **Ollama effective ctx** — probed live (v0.30.8): default silently truncates at **4,095 tokens**; `options.num_ctx=16384` honored; `truncate:false` → 400 fail-loud. Real binding = min(8192, 8191, num_ctx-we-set) |
| Ledger size | "750+ snapshots" | **7,568** index entries (active store: 7,873 rows — 305 orphans to prune) |
| Corpus distribution | unknown ("emit it in WI-6") | mean 5,218 chars, p50 4,351, p90 9,645, max 101k; **672 snapshots (8.9%) exceed the 10k cap → 1.96M chars unrecallable today**; 30.9% > 6k chars |
| Chunk multiplier | "5–10×" | **2.52×** at ~3,000 chars / 15% overlap → ~19,061 vectors, ~235 MB matrix (in-RAM; fine) |
| Backfill cost | open question | cloud gemini-embedding-2: **~$2–3, well under 1 h**; qwen3-8b CPU: **~99 h measured** (46 tok/s warm) |
| GPU | "the Ada GPU… 16GB Ada" | **No NVIDIA GPU installed** (AMD Raphael iGPU only). SKU ambiguity: RTX 4000 Ada = **20 GB**; a 16 GB card is RTX 2000 Ada / 4060 Ti class. FP16-8B (~15.1 GB weights) hinges on this |
| Golden set | "extend Phase-5 golden set / test_embeddings_search" | `test_embeddings_search.py` is a hermetic fake-provider suite (category error). Real seed = `test_retrieval_golden.py`: **3 labeled pairs + 4 soft queries**, boolean asserts, no recall@k/MRR. The reusable machinery is `benchmarks/digest_ab/run_ab.py` (hit@1/3/5 + MRR + LLM query bootstrap) — untracked, must be committed |
| Reranker serving | "Ollama scoring-prompt vs TEI / Infinity / vLLM" | **Ollama has no `/api/rerank`** (404 verified live; PR #7219 unmerged). **TEI and Infinity do not support Qwen3-Reranker** (open issues #643/#691/#763, #642). Only real server: **vLLM `/score`** with `Qwen3ForSequenceClassification` hf_overrides (validate against transformers reference — open fidelity bugs), or in-process transformers, or DIY yes/no-logit prompting |

### Two refuted "verified facts"

1. **WI-1 `AUTO_TRUNCATE=false` — REFUTED.** `providers.py:25` uses the legacy
   `google.generativeai` 0.8.6 SDK over the API-key Developer API; its `embed_content` has no such
   parameter and the underlying proto doesn't carry it. The newer `google-genai` 1.64.0 defines
   `auto_truncate` but its Developer-API converter **hard-raises**
   `ValueError('auto_truncate parameter is not supported in Gemini API.')`
   (`google/genai/models.py:700-701`) — it is Vertex-only. Fail-loud for Gemini must be a
   **client-side token-count guard**, not a request flag.
2. **WI-2 "stores are keyed by slug+dims+schema-version" — REFUTED.** Store identity is
   `(realpath(base_dir), slug)` only (`store.py:328`); `meta.json` is exactly
   `{slug, dims, normalized, count, last_updated}` (`store.py:204-211`) — **no schema version
   exists anywhere**, and a chunked rebuild of the *same* model has no addressable identity:
   `POST /embeddings/migrate` at the active slug diffs an already-full store and no-ops;
   same-dir second instances race the live store. Schema versioning must be **invented**, not reused.

### Other load-bearing discoveries (not in the spec)

- **The mint path is missing from WI-2.** `checkpoint.py` (3 sites) → `fossils.py:501
  store.append(snap_id, embedding)` writes ONE whole-body vector at snapshot creation. Post-swap,
  every new snapshot would land unchunked; because reconciliation is snap_id set-algebra
  (`store.missing()`, `migrate.py:280`, `watcher.py:354`), it would be marked complete and **never
  re-chunked** — the newest memory (which recency boosts) permanently regresses. `fossils.py`
  needs a real logic change, not "no logic change".
- **`append_many` dedupes by snap_id, first-wins, silently** (`store.py:179-180`) — a naive chunked
  backfill "succeeds" while storing **only chunk 0** of every snapshot (worse than today), and
  `transcode.py`/`migrate.py` *rely* on that dedupe for crash-rerun idempotency.
- **`semantic_threshold` is dead code in ranking.** All three consumers pass it into
  `semantic_retrieve`'s **unused** `threshold` param (`fossils.py:114-115`). The only live floor is
  `junk_floor=0.40` (`retrieval.py:151`). WI-3's "reuse semantic_threshold" would raise the
  effective floor 0.40→0.55 for the active model — a default-ON results change contradicting
  invariant 6, on values calibrated as *whole-snapshot relevance thresholds*, not junk floors.
- **The migrate engine auto-cuts-over** the moment its diff is empty (`migrate.py:367-369`) — no
  build-without-activate mode; **boot auto-resume** (`startup.py:282-283`) would swap an unevaluated
  chunk store at 3am. And `retrieve()` hardcodes the ACTIVE store (`retrieval.py:131`) — there is
  **no seam to eval a candidate store**, so "swap only after golden passes" is unexecutable as-is.
- **Whole-snapshot delivery is shim-level only.** Every model-facing consumer truncates: 3,000 chars
  in the 6 chat loops + 3 voice routes, 10,000 in the `search_snapshots` executor, 500-char snippets
  on `/fossil/hybrid`. Chunk-scoring will surface long snapshots whose relevant passage sits *past*
  the delivery cap — found but never shown. (North-star gap; see Open Questions.)
- **Six chat provider loops + CU driver pass no operator** to `hybrid_retrieve` → keyword channel is
  silently EMPTY there (op `''` matches no index entries) and semantic is unscoped. Golden-set runs
  through scoped surfaces won't cover the surfaces users actually chat on.
- **WI-1's tokenizer plan is half-fictional:** no Ollama tokenize API exists (404 live), tiktoken is
  not in the venv and fetches BPE files over the network on first use (breaks the offline Qwen-only
  profile). Gemini `countTokens` was live-verified working for both Gemini models (the client-side
  pre-check). And the spec's chars≈tokens×**3** heuristic is NOT conservative: corpus average is
  2.9 chars/token, but code-dense real snapshots measure **2.12** (hexdump 1.14, base64 1.34) — a
  ×3 clamp overshoots the token budget up to 42% on code-heavy snapshots. Safe fallback is **×2**.
- **"Land WI-1 first" is an ordering trap for fail-loud:** without the chunker, a client-side raise
  on over-limit documents fires on the 30.9% of the corpus above ~2,048 tokens (under the gemini-001
  fresh-box default) — converting truncated recall into ZERO recall. WI-1 must ship clamp-only with
  a `[EMBEDDING] clamped N tokens` telemetry line; fail-loud activates only post-WI-2, and only for
  snapshot documents. Queries are ALWAYS clamped, never fail-loud (a pasted 20k-char log prompt
  would otherwise blank retrieval and flip health to degraded).
- **Killing `EMBEDDING_MAX_CHARS` also removes the query-side cap** that `chat_routes.py:134-144` and
  `tasks.py:1530-1537` explicitly rely on (the send_sms truncation fix). The token-aware clamp must
  keep covering `purpose="query"`. Sweep: `test_embeddings_registry.py:63`,
  `test_embeddings_providers.py:270-285`, `benchmarks/digest_ab/run_ab.py:62`.
- **Chunking must be call-site-scoped:** ToolVault description embeds, the watcher health probe, and
  query embeds all flow through the same `generate_embedding_sync`/`provider.embed` chokepoints —
  chunk only at snapshot document sites (mint seam, `watcher._gap_heal`, `migrate.py` engine).
- **WI-4 placement math is wrong as written.** RRF fused scores span ~0.010–0.033 and the recency
  boost caps at 0.05 (up to 2× the whole RRF spread — recency is a strong orderer, not cosmetic).
  Replacing relevance with reranker probabilities [0,1] silently destroys the recency term AND
  breaks MMR's `lam*rel − (1−lam)*sim` balance (~30× scale jump → near-duplicate clusters flood
  back). Rerank must map order back into rank-space scores `1/(rrf_c+rank)`, re-apply the recency
  tie-break, and rebuild `score_by_id`.
- **CPU rerank is 1.5–3 min/query** for 50 candidates (anchored to the measured 46 tok/s) — WI-4 is
  strictly post-GPU; the flag needs a latency preflight, not just provider availability.

---

## 3. Required spec amendments (all adopted into the implementation understanding)

**A1 — Collapse lives in the store, not `retrieve()` step 4.** `search`/`search_with_vectors`
dedupe by snap_id during the argsort descent: the first occurrence of a snapshot IS its max-cosine
best chunk (order property), `k` keeps meaning *k distinct snapshots*, and the best-chunk vector
falls out for free. `retrieve()` then needs zero changes at lines 150–153; `semantic_search` and
`calibrate_threshold.py` (the other store consumers) are covered automatically. This kills four
bugs at once: candidate-pool shrinkage, RRF duplicate double-count (length bias), `vec_by_id`
last-wins (worst-chunk MMR representative), and duplicate snapshots in legacy top-k contracts.
It also makes the sibling-duplication/context-bloat trap (multiple chunks of one snapshot each
triggering a whole-parent fetch → the same 30k snapshot delivered N times) structurally
impossible: `retrieve()` emits unique snap_ids, so delivery fetches each parent at most once.
Acceptance test: a store where one snapshot's chunks occupy semantic ranks 1–3 returns that
snapshot exactly once, with its best chunk's score/vector, and k distinct snapshots overall.

**A2 — Row-id currency is sacred.** `ids.json` rows stay **bare snap_ids** (repeated per chunk);
chunk ordinals live in a parallel `ordinals.json` sidecar. Composite ids ("SNAP-x#3") would
fail-closed operator scoping to zero results and make every reconciliation loop (migrate
convergence, gap-heal, status `missing`, F4 recent-gap guard) diverge forever.

**A3 — Chunk groups are atomic.** All chunks of one snapshot land in ONE `append_many` call; the
idempotency key stays snap_id but means "full chunk group present" (skip whole incoming group if
any row exists — preserves transcode/migrate crash-rerun contracts). Write order (vectors fsync
before ids rewrite) + self-heal truncation then guarantee a torn group heals to fully-absent →
still `missing` → cleanly re-embedded. Store validate asserts per-snapshot ordinal contiguity.

**A4 — Mint path chunks too, and lands FIRST.** Add `checkpoint.py` + `fossils.py` to WI-2's file
list; route mint embedding through the same chunker (snapshot-only helper). Sequence constraint:
mint-path chunking ships *before* the backfill starts so racing mints write chunk groups. Add a
schema guard at the mint vector seam (`fossils.py:483-504`): a single whole-snapshot vector destined
for a v2 store is dropped with the existing "catch-up re-embeds it" semantics (mirrors the
dims-mismatch branch).

**A5 — Invent minimal schema versioning + build-only mode.** `meta.json` gains
`{schema: 2, rows, snapshots, generation}` (absent schema ⇒ 1; `generation` bumps on every
mutation incl. self-heal — this is also the whole WI-5 seam). Candidate store builds in a distinct
directory (e.g. `{stores}/_build/{slug}` via the existing `base_dir` param → distinct
`get_store` cache entry, invisible to `list_stores`). `migrate.py` (the REAL backfill engine —
`backfill_embeddings.py` is a thin CLI over it) gains a rebuild mode: same slug,
`activate=false`, chunk-aware batching (snapshot-granular, chunks flattened for `provider.embed`,
regrouped for group-append; gap-heal gains sub-batching sized in chunks). Cutover is a separate
explicit action; persisted job state records build-only so **boot resume never swaps**. Swap = dir
rename under a new locked store-reload seam (or v1: CLI with service stopped + restart).

**A6 — Dual-schema read path, forever.** v1 one-vector stores stay a permanently supported input
(schema-autodetected per store from meta): the watcher's broken-path auto-migration or the Portal
Update button can legally re-activate a v1 store at any time. Non-active stores (gemini-001,
qwen 0.6b/8b) are **retained untouched** as rollback assets — rebuilt lazily only when an operator
switches to them with chunking enabled; never deleted. Documented rollback: flip active pointer
back to the retained v1 dir, then immediately POST a diff-fill migrate for the mint gap (don't
wait on the 50/day watcher heal).

**A7 — WI-1 redesign (client-side, offline-safe, two modes).** Per-model `max_input_tokens` lands
in the registry (net-new field; the Task-16 guard pattern already enforces registry-only literals).
Mode 1 (lands first): token-aware CLAMP that never raises, + `[EMBEDDING] clamped N tokens`
telemetry. Mode 2 (activates post-WI-2, snapshot documents only): fail-loud. Mechanisms: no
server-side flag exists — Gemini fail-loud = `countTokens` pre-check (live-verified both models;
heuristic-gate it so it only fires near the limit); Ollama = `options.num_ctx` set explicitly from
the registry budget (probed: honored on 0.30.8) + `truncate: false` (probed: 400 fail-loud), with
`prompt_eval_count` as the acceptance verifier; effective Qwen limit = min(32768, num_ctx we set).
No-tokenizer fallback heuristic is chars≈tokens×**2** (measured: corpus mean 2.9, code-dense 2.12,
pathological 1.14 chars/token — the spec's ×3 overshoots). Tokenizers strictly optional + lazily
imported (tiktoken vendored/pre-cached if shipped; no Ollama tokenizer exists — delete that claim).
Queries are ALWAYS clamped, never fail-loud (chat_routes/tasks rely on the query-side cap; the
Ollama query_instruction prefix counts inside the budget). The shared `providers.embed` seam stays
clamp-not-raise for all non-snapshot documents (ToolVault descriptions — max live description
1,331 chars measured — and the watcher health probe, whose failure streak triggers auto-migration);
fail-loud is scoped to the snapshot pipeline call sites. Sweep in the same change:
`test_embeddings_registry.py:63`, `test_embeddings_providers.py:270-285`,
`benchmarks/digest_ab/run_ab.py:62`, and the reliance comments at `chat_routes.py:134-144` /
`tasks.py:1530-1537`.

**A8 — WI-3 redesign (measured, not assumed).** Do NOT reuse `semantic_threshold` (dead in
ranking — all three consumers feed `semantic_retrieve`'s documented-UNUSED param; calibrated as a
whole-snapshot relevance threshold, owned by a different calibration procedure). Live probes show
reuse would be a silent no-op on the active model (gemini-2: 40/40 candidates ≥0.55 on every
on-topic probe) but a **recall wipe on the offline profile** (qwen3-0.6b: on-topic "website
faster" query top1=0.4499 → 0/40 pass a 0.54 floor; the phone-lean path returns empty — an
invariant-2 violation). Design: net-new nullable per-model `junk_floor` registry field calibrated
as a NOISE floor (measured bands: qwen-0.6b ≈0.35–0.40, gemini-2 ≈0.50), precedence = registry
value if non-null else `[retrieval].junk_floor`, behind a `registry_floor_enabled` flag default
false. Recalibrate on **chunk-max** distributions against the candidate store pre-swap (update
`scripts/calibrate_threshold.py` to search the collapsed chunk path and report relevance AND
noise bands) — sequenced AFTER WI-2's store exists, not "alongside". Same change: delete or
comment the dead `threshold` plumbing (three `active_threshold` call sites → unused param) and
add an acceptance test that the phone-lean path stays non-empty on each local model with the
registry floor active.

**A9 — WI-4 redesign.** Placement: rerank top-`min(rerank_candidate_n, len(fused))` (pool is ≤40
on most surfaces), map reranked ORDER to rank-space scores `1/(rrf_c+new_rank)`, re-apply
`apply_recency_tiebreak`, rebuild `score_by_id`, then MMR unchanged. Define rerank input for
keyword-only candidates (decode + chunk on the fly; every pool member scored on one scale — else
the keyword channel's exact-string wins are annihilated). Serving: vLLM `/score` primary (cap
`gpu_memory_utilization` ~0.15–0.25 or it evicts the Ollama embedder), in-process transformers for
eval, DIY logit-prompt as last resort; TEI/Infinity are dead ends. Gating: flag AND provider
availability AND a startup latency preflight (e.g. 1-candidate probe < 500 ms). Explicitly
post-GPU-install; pre-GPU work is the provider abstraction + WI-6 offline arms only.

**A10 — WI-6 redesign.** Labeled set: stratified ~500-snapshot sample (oversample the >10k-char
band where truncation loss is proven; stratify by age/operator), query per snapshot generated from
a **random 1–2k-char span at a random offset** (position recorded; recall reported by head/middle/
tail third — this is the leakage guard that makes a chunk-arm win attributable to tail recovery
rather than query-gen bias), ≥10% hand-validated, the 3 human-verified golden pairs + real recent
queries as leakage-free holdout. Machinery: seed from `benchmarks/digest_ab/run_ab.py` (commit it
first; fix its `EMBEDDING_MAX_CHARS` import with WI-1) but rank **through `retrieve()` against the
full live ledger**, not a closed corpus. Matrix split: **Phase A (now, gates WI-2):**
{whole, chunk} × {gemini-embedding-2, qwen3-embedding-0.6b}, rerank off — the gemini-2 delta is
THE swap-authorizing number. **Phase B (post-GPU, gates WI-4 + SKU question):** {quant-8B,
FP16-8B} × {rerank on/off} on the subset. Requires the A5 eval seam (candidate store addressable;
bench report must prove it ran against the candidate pre-swap).

**A11 — Ops/status currency.** `/embeddings/status` keeps `count` in snapshot units and adds
`rows` (additive — the status payload is a BINDING contract for wizard/Portal/Android cards);
watcher `_pick_migration_target` ranks by unique-snapshot coverage, not raw rows; the backfill CLI
gains a service-liveness probe (refuse without `--force` when the orchestrator is up — the
cross-process append race is real and destructive).

**A12 — Chunk sizing is a quality choice now, not a limit fit.** With the real binding limit ~4–8k
tokens, `chunk_tokens=1024` survives as a *scoring-resolution* default (p50 snapshot ≈ 1,500 tok →
1–2 chunks), but it is a WI-6 measured axis, and Ollama `num_ctx` must be set ≥ chunk budget.

**A13 — Fresh-box portability.** New boxes initialize stores directly at schema v2 (nothing to
migrate, no gate); the golden-set comparison is a dev-box gate for shipping the pattern, not a
runtime precondition. All new knobs ship with code fallbacks (config.ini is never rewritten by
code; there is no `[embeddings]` section on this box).

---

## 4. Corrected sequencing

1. **WI-8** — operator plumbing into the 6 chat loops + CU driver (small; makes every later
   measurement representative).
2. **WI-11** — central tokenization module (vendored local tokenizers + remote counters +
   calibrated floor). Foundational: WI-1, WI-2, WI-6, WI-9, and WI-10 all consume it.
3. **WI-10 (verification half)** — provider context-window audit (docs + live over-cap probes,
   incl. the 210k-char TTFB repro per provider), token math via WI-11. Pure measurement; runs
   parallel with WI-6. The cap-removal half lands with its transport hardening, any time after
   verification.
4. **WI-6 Phase A** — harness + eval seam + stratified labeled set. Emits the swap-gating numbers.
5. **WI-1 (mode 1)** — registry `max_input_tokens` + clamp-only token-aware cap via WI-11 (docs &
   queries) + Ollama num_ctx/truncate:false + clamp telemetry + dependent-test/benchmark
   sweep. Fail-loud (mode 2) is deferred to post-WI-2, snapshot documents only.
6. **WI-2** — store schema v2 (A1–A6, A11): mint chunking first, build-only cloud backfill
   (~$2–3, <1h), calibrate on candidate, golden + Phase A gates, explicit cutover, documented rollback.
7. **WI-10 (cap-removal half)** — delete delivery truncation for verified cloud providers +
   transport hardening (210k repro green per provider); WI-7a windowing remains only for
   window-bound profiles (phone lean; unverified providers' interim guards).
8. **WI-7** — (a) matched-chunk windowing for window-bound profiles (needs A1's best-chunk
   identity; lands with or immediately after WI-2's swap). Part (b) dedupe-with-backfill has NO
   WI-2 dependency — it can land early alongside WI-8 as a small contained context-quality win.
9. **WI-3** — per-model `junk_floor` registry field from the post-chunk NOISE calibration
   (after WI-2, not alongside), behind `registry_floor_enabled` default false.
10. **WI-9** — hardware probe + VRAM-aware `_model_preflight` gating + placement toggles across
   wizard/Portal/Android. The probe endpoint and embedder placement land here (useful
   immediately for the qwen-8b recommendation); reranker placement activates with WI-4, which
   depends on this probe.
11. **WI-4** — post-GPU (RTX 2000 Ada 16GB): vLLM /score provider + corrected placement + latency
    preflight against the WI-9-chosen device; Phase B eval (quant-8B arms; no FP16-8B on-box)
    decides on/off.
12. **WI-5** — nothing now; the seam is A5's `{schema, generation}` meta fields (+ documented ANN
    invalidation key `(slug, schema, generation, rows)` and the scoped-operator oversampling gate).

## 5. Decisions (Brandon, 2026-07-01)

1. **GPU SKU = RTX 2000 Ada (16 GB).** Consequence: FP16 Qwen3-8B (~15.1 GB weights) cannot
   co-reside — the WI-6 Phase B FP16-8B arm is dropped from the on-box matrix (a CPU subset run is
   allowed for a directional signal only). Q4 8B embedder (~6–7 GB resident) + Qwen3-Reranker-0.6B
   FP16 (~1.2 GB) or 4B Q4 (~2.5 GB) fit comfortably; if vLLM co-serves the reranker, cap
   `gpu_memory_utilization` (~0.15–0.25) or it evicts the Ollama embedder.
2. **Delivery gap: IN SCOPE.** New work item **WI-7 — delivery & context-assembly quality**, two parts:
   - **(a) Matched-chunk-aware delivery — SCOPE REVISED by WI-10 (2026-07-01):** cloud surfaces
     deliver whole snapshots CAP-FREE (see WI-10 — count knobs are the only budget). Matched-chunk
     windowing (center the delivered window on the best-matching chunk, which retrieve() knows
     post-A1, instead of blind head-truncation) is the delivery mechanism ONLY where a genuine
     window bound exists: the on-device phone lean profile, and any provider that hasn't yet
     passed WI-10's transport-hardening acceptance (its interim guard cap windows smartly rather
     than clipping heads).
   - **(b) Cross-channel dedupe-with-BACKFILL in the sectioned context builders (added
     2026-07-01):** `build_fossil_context` (`context_builder.py:138-151`) dedups keyword-vs-recent
     and semantic-vs-(recent∪keyword) but is filter-only — a dropped duplicate SHRINKS the section
     (keyword fetches exactly KF=4; a duplicate leaves 3 or fewer) instead of pulling the
     channel's next-ranked candidate. Fix: over-fetch each channel's ranked list (or fetch
     `section_k + |seen|`) and fill each section with the first N *unseen* candidates, preserving
     today's precedence (recent → keyword → semantic); decide checkpoint-section dedup explicitly
     (currently not cross-deduped at all). Sweep the sibling implementations: chat_routes
     `build_streaming_context` and the non-stream worker's assembly in `tasks.py:1404-1405`.
     Scope guard: this applies ONLY to the sectioned builders — inside `retrieve()`,
     keyword/semantic overlap is deliberate signal (RRF rewards agreement; fused top-k is already
     unique post-A1). Do not "dedupe" the RRF channels.
     Acceptance: with a snapshot present in both Recent and the keyword ranking, the keyword
     section still delivers KF distinct snapshots (backfilled), and total per-turn context
     contains no duplicate snap_id across sections.
3. **Keyword/operator plumbing: IN SCOPE.** New work item **WI-8**: plumb the session operator into
   the 6 chat provider tool-loops + CU driver so `hybrid_retrieve` is actually hybrid and
   operator-scoped on the main chat surface. Small, contained; do it early so WI-6 measurements
   reflect real surfaces.
4. **Non-active stores:** retain-untouched policy (A6) adopted — no proactive rebuild of
   gemini-001/qwen stores; lazy rebuild on model switch only; never delete (rollback assets).
   *Update (post-gate flip, Brandon 2026-07-02, landed 2026-07-03):* model-switch migrations now
   CREATE fresh target stores as schema-2 (chunked) by default; existing v1 stores remain legal
   fill targets (rollback assets + the watcher's recovery path). Existence-based decision at the
   engine's target-open site (`migrate.open_migration_target`), never registry-based.
5. **Hardware-aware onboarding gate + per-model device placement: IN SCOPE (added 2026-07-01).**
   New work item **WI-9**:
   - **Detection.** A server-side host-hardware probe (GPU present? via nvidia-smi/lspci; VRAM;
     system RAM; cached, fail-soft) — none exists today. Precedent to follow: `GET /cu/preflight`
     (machine-readiness checks with customer-facing remediation strings).
   - **Gating.** Extend `_model_preflight()` (`embeddings_routes.py:111-149` — already does
     Ollama install/start/pull + `ram_preflight(entry["ram_gb"])`) with VRAM awareness: per
     model (embedders AND the WI-4 reranker entries), compute whether the GPU path fits from
     VRAM budget arithmetic (Q4-8B embedder ≈6–7 GB resident incl. ctx buffers; reranker 0.6B
     FP16 ≈1.2 GB / 4B Q4 ≈2.5 GB) and emit `ready`/`blockers`/recommended placement
     accordingly. No GPU or insufficient VRAM ⇒ the CPU path is offered, never a dead end
     (CPU-only = today's behavior; fresh-box/offline gate holds).
   - **Placement toggles.** Per-model device placement (GPU/CPU) as an explicit toggle,
     recommended defaults computed from the probe. Brandon's example is the canonical case: an
     8 GB-VRAM GPU fits the Q4-8B embedder ALONE ⇒ recommend embedder→GPU, reranker→CPU; user
     can override each. Enforcement mechanics: embedder CPU-pin via Ollama `options.num_gpu: 0`
     (GPU = omit and let Ollama offload); reranker placement selects the serving path (vLLM
     `/score` on GPU with capped `gpu_memory_utilization` vs in-process/llama.cpp CPU scoring).
     Persist placement per slug beside the stores (mirror the existing `keep_alive.json`
     pattern). Note the interaction: Ollama's VRAM-tiered default ctx means placement changes
     the effective context — WI-1's explicit `num_ctx` must be sent on BOTH placements.
   - **Surfaces.** `/embeddings/status` gains `hardware` + per-model `placement`/
     `recommended_placement` fields (ADDITIVE — the payload is a binding contract for
     wizard/Portal/Android cards); onboarding wizard step gates model choices and shows the
     toggles (`onboarding_routes.py:656` already rolls up embeddings status); Portal updates
     card + Android card follow (3-surfaces rule).
   - **Acceptance.** On a no-GPU box every model still offers a CPU path (this box today); on an
     8 GB-VRAM box the recommendation is embedder-GPU/reranker-CPU; toggling placement takes
     effect without restart and is persisted; rerank latency preflight (A9) runs against the
     *chosen* placement, so a CPU-placed reranker that misses the latency ceiling stays off with
     a logged reason rather than degrading chat.
6. **No caps on model-bound context — verify provider windows: IN SCOPE (added 2026-07-01).**
   New work item **WI-10 — provider context-window verification + delivery-cap removal.**
   Brandon's directive: caps exist ONLY at the embedding/chunking layer (so ranking picks the
   best snapshots); the context the chat model receives is governed by the config.ini COUNT
   knobs (recent/keyword/semantic/checkpoint — live: RF=5, KF=3, SF=6, CP=2) and nothing else —
   every delivered snapshot arrives WHOLE.
   - **Cap inventory to remove/re-derive (all clip model-bound content today):** per-snapshot
     `[context] max_fossil_chars` = 8,000 live (code fallback 10k) via `cap_chars`; 3,000-char
     truncation in the 6 chat tool-loops + 3 voice routes; 10,000-char cap in the
     `search_snapshots` executor; 500-char snippets on `/fossil/hybrid`;
     `MAX_TOTAL_CONTEXT_CHARS = 200000` and `PROVIDER_CAPS` (`context_builder.py:40-62`:
     anthropic 75k chars, computer-use 75k, openai 100k, local 16k).
   - **Verification (docs + live tests, per model/provider actually in use):** chat (Opus 4.7
     default, OpenAI, Gemini, Grok), voice-session models, CU models, on-device Gemma. For each:
     documented window, then a LIVE over-cap probe proving a whole worst-case context (16
     snapshots; corpus p99 = 14.9k chars each ≈ 238k chars ≈ 60-80k tokens; single-max snapshot
     101k chars) completes end-to-end with acceptable TTFB. Reconcile the `local` cap comment
     ("16K window") against the on-device engine's actual ctx default (6144 per the 2026-06-27
     work).
   - **The TTFB collision (must be paired, not ignored):** the anthropic 75k cap is a
     WORKAROUND for an empirically confirmed transport failure (2026-04-25: 210k chars → Opus
     4.7 adaptive-thinking TTFB 30-60s → Android OkHttp default SSE timeout → silent stall;
     same payload fine on Gemini). Cap removal therefore ships WITH transport hardening
     (SSE read-timeout raises / keepalive heartbeats on Android + Portal + voice) and the
     acceptance test is that exact repro: the 210k-char payload completes cap-free on every
     provider. Until a provider passes, its cap stays as a documented transport guard — never
     a content-quality decision.
   - **Window safety guard (not a cap):** delivery keeps one computed guard = verified provider
     window minus response/tool headroom, sourced from provider docs/discovery per the
     provider-API-as-SoT rule. With count-knob budgets it should essentially never bind on
     cloud models (worst case ≈80k tokens vs 256k-1M+ windows); if it ever would, drop the
     LOWEST-ranked snapshot whole rather than truncating any snapshot mid-body, and log it.
   - **Profile exception:** the on-device phone window is genuinely small — the lean profile
     keeps its budget and uses WI-7a matched-chunk windowing as its delivery mechanism. Cloud
     surfaces deliver whole snapshots, uncapped.
   - **Ranking side unchanged:** chunk caps live only in WI-1/WI-2 (embedding layer); count
     knobs stay the per-section budget; WI-7b backfill guarantees the sections are actually
     full of distinct snapshots.
7. **Real token accounting across the system: IN SCOPE (added 2026-07-01).** New work item
   **WI-11 — central tokenization module** (one seam, per-model backends resolved from the
   registries) so token math is REAL, not guessed — feeding WI-1's clamp/fail-loud, WI-2's chunk
   sizing, WI-6's corpus stats, WI-9's num_ctx sizing, and WI-10's window-guard math.
   - **Backends by what actually exists per provider:** OpenAI = tiktoken with VENDORED/pre-cached
     encodings (exact, local, offline-safe). Qwen embed+rerank = HF `tokenizers` with vendored
     `tokenizer.json` per model (exact, local, offline — NOT via Ollama, which has no tokenize
     API; `prompt_eval_count` is the post-hoc verifier). Gemini = `countTokens` API
     (live-verified this audit) — exact but REMOTE. Anthropic = `count_tokens` API — exact but
     REMOTE. On-device Gemma = vendored HF tokenizer server-side for estimates (the device
     engine owns true tokenization). Universal fallback = the calibrated conservative heuristic
     (chars≈tokens×2, measured floor for this corpus).
   - **Design principle (pushback adopted): exactness at boundaries, floors in the hot path.**
     Exact-LOCAL tokenizers run everywhere they're free (OpenAI/Qwen/Gemma). Exact-REMOTE
     counting (Gemini/Anthropic APIs) runs at calibration, preflight, WI-10 verification, and
     heuristic-gated near-limit checks ONLY — never unconditionally per embed/turn, because a
     network round-trip per call adds TTFB to every mint and retrieval and makes the OFFLINE
     profile depend on a cloud endpoint (an invariant-2 violation if load-bearing). The
     conservative floor + fail-loud verification (Ollama truncate:false, Gemini countTokens
     gate, prompt_eval_count asserts) means overflow is impossible even when the fast path used
     the heuristic — perfect recall is protected by construction, not by per-call exactness.
   - **Packaging:** `tiktoken` + `tokenizers` added to requirements with vendored assets
     (network-disabled boxes must pass); lazy imports; registry entries name their tokenizer
     backend so the Task-16 no-literals-elsewhere guard extends to tokenizer config.
   - **Acceptance:** per model in use, module token counts match ground truth (prompt_eval_count
     / provider count endpoints) within a stated tolerance on a reference sample; all local
     backends work with networking disabled; hot-path overhead stays negligible (local encode
     ~sub-ms); WI-1/WI-2/WI-10 consume ONLY this module for token math (no scattered heuristics).

## 6. Key evidence index

- Collapse/dedupe: `store.py:179-180, 260-266, 294-301`; `retrieval.py:41-43, 150-153, 191-198`
- Mint seam: `checkpoint.py:153/276/364` → `fossils.py:483-504` (`:501 append`)
- Backfill engine: `migrate.py:261-405` (auto-cutover `:367-369`, raced `:371`); `watcher.py:342-381`
  (gap-heal, HEAL_CAP=50, one big embed call `:371`); boot resume `startup.py:282-283`
- No schema/version: `store.py:204-211, 328`; active pointer `store.py:362-379`; reload seam: none
- Gemini SDK: `providers.py:25,124-134`; `google/genai/models.py:700-701` (ValueError, Vertex-only)
- Truncation: `providers.py:60-63, 81` (queries too); reliance `chat_routes.py:134-144`, `tasks.py:1530-1537`
- Dead threshold: `fossils.py:114-115`; live floor `retrieval.py:123,151`; calibration `registry.py:27-31`
- Surfaces: 6 chat loops `chat_routes.py:844/1453/2235/2977/4877/5542` (no operator); voice ×3;
  CU `driver_anthropic.py:420`; MCP via `/fossil/hybrid` `task_routes.py:241`; phone lean
  `local_routes.py:320` (`include_keyword=False`, k=3); delivery caps 3k/10k/500
- Eval prior art: `test_retrieval_golden.py` (3 pairs), `test_local_lean_retrieval.py` (phone
  invariant, hard-fail), `benchmarks/digest_ab/run_ab.py` (metrics + bootstrap), `scripts/calibrate_threshold.py`
- Corpus/cost: measured via scratchpad `corpus_stats.py` over `Manifest/snapshot_index.json` +
  `Volumes/SNAPSHOT_VOLUME.txt`; CPU throughput probed live (46 tok/s warm 8B); Ollama probes:
  default 4,095-tok silent cut, num_ctx honored, truncate:false → 400; `/api/tokenize` → 404

---

## 7. WI-5 — ANN escape-hatch seam (M12, documentation only; no code this cycle)

The seam shipped with store schema v2 (M6a): `meta.json` carries `{schema, rows, snapshots,
generation}` where `generation` increments on EVERY mutation — appends AND self-heal
truncations AND meta refreshes (store.py; fuzz-verified). When vector count or latency ever
motivates ANN, the contract is:

- **Sidecar identity:** the ANN index lives inside the store dir, keyed on
  `(slug, schema, generation, rows)`. Any mismatch on open ⇒ full rebuild of the index.
  `generation` (not `rows` or mtime) is the load-bearing field: self-heal can shrink and
  re-grow to the same row count, and only generation observes that.
- **Write path:** the index must be updated inside the same append path that invalidates the
  in-memory matrix, or its staleness window vs the mint-instant-searchability contract
  (CLAUDE.md: snapshots searchable the moment the mint returns) must be explicitly documented
  and bounded.
- **Compaction/pruning is a full-rebuild event** — ANN labels are positional (row i ↔ ids[i]);
  any row reorder invalidates every label.
- **The exact-vs-ANN acceptance gate MUST include operator-scoped queries.** allowed_ids
  filtering is post-scoring: exact search scores ALL rows so the filter is exact; ANN returns
  k' neighbors, so a scoped operator owning a small fraction of rows needs oversampling
  (k' ≈ k / ownership-fraction) or filter-aware search. An unscoped-only gate passes trivially
  and fails precisely for scoped operators.
- **Enablement bar (from the audit):** exact cosine at the post-chunk scale (~19-23k rows,
  ~235MB matrix, tens of ms) is nowhere near the ceiling; ANN is opt-in behind a flag, enabled
  only past ~100k rows AND after the gate above passes on the golden set.
