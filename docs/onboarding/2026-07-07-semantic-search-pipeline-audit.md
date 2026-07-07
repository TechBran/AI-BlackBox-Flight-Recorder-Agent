# Semantic Search Pipeline — Full Forensic Audit (2026-07-07)

**Operator context:** Brandon / Brandon-DEV — "some snapshots come back with good relevancy, others just aren't relevant … the ranking/re-ranking is happening but something is missing in the semantic search."

**Method:** 10 read-only audit agents over the real source + a live empirical probe that replicated `retrieve()`'s exact stages against the live stores (real GOOGLE_API_KEY + Vertex creds), plus an adversarial adjudicator that settled 12 prior-session claims against code. Every finding below is cited to `file:line`. No code or config was changed.

---

## TL;DR — the diagnosis, corrected by data

The delivered noise tail is **NOT** a semantic-similarity failure, **NOT** the "arguments hub" snapshot leaking, and **NOT** primarily a threshold-tuning problem. The live probe proves:

1. **The semantic (embedding) lane is essentially clean.** Task types are correct (asymmetric `retrieval_query`/`retrieval_document`), vectors are L2-normalized both sides, coverage is complete (missing=0), and genuine-relevant snapshots score high raw cosine (0.64–0.79). The specific "arguments hub" snapshot the prior sessions blamed (SNAP-20260327-4931) scores cos **0.5143 at raw rank 2188** — it never enters the candidate window and is **never delivered** on the active store.

2. **The delivered noise tail (ranks ~4–10) is KEYWORD lexical-only injection via RRF fusion.** Across all three probe queries, ranks 4–10 in production were snapshots that never appeared in the semantic top-40 at all — pure lexical matches on shared tokens ("checkpoint", "search", "noise", "key"). Measured: the keyword channel's own top-40 was **28/40, 31/40, 30/40 lexical-only**. RRF fuses them at `1/(60+rank)`, the same magnitude as a mid-ranked semantic hit, so they seat right behind the 2–3 real hits. **Brandon's instinct — "keywords should be their own separate thing, only de-duplicating" — is exactly right, and now proven with live numbers.**

3. **The pipeline structurally cannot return fewer than k.** There is no per-result relevance floor on the output; `retrieve()` always pads to k=10 (fill-to-k). Even with keyword fixed, a query with only 3 good hits pads ranks 4–10 from weak candidates.

4. **The reranker is live (Google Vertex) but only re-orders — its absolute relevance score is computed, returned, and then thrown away** (`retrieval.py:283` remaps rank order to `1/(rrf_c+new_rank)`). It has the signal to prune junk but the architecture discards it. That is why "re-ranking runs but junk survives."

5. **There is no clean threshold number to find.** The probe shows relevant vs near-miss cosines *overlap* (in Q2, five UNKNOWN snapshots at 0.72–0.74 out-rank a genuine relevant at 0.70), and the Vertex reranker scores also overlap relevant/noise *and* false-negative real hits (a clearly-relevant snapshot at cos 0.72 scored rerank 0.026 = rank 27/40). **The fix is architectural, not a magic floor.**

> Net: Brandon was half-right and half-misdirected by the prior sessions. Right: keyword should be separate. Misdirected: it was never a "find the 0.6-vs-0.4 floor" problem — the floor is a minor lever; **the keyword-fusion architecture + fill-to-k is the leak.**

---

## The pipeline as it ACTUALLY runs (live, verified)

`search_snapshots` (both the remote MCP tool → `GET /fossil/hybrid` and the in-chat ToolVault executor → `fossils.hybrid_retrieve`) call the single canonical core `Orchestrator/retrieval.py::retrieve(..., include_keyword=True)`:

```
query
  │
  ├─ SEMANTIC lane ─ gemini-embedding-2 (retrieval_query) → store.search_with_vectors(candidate_n=40)
  │     • schema-2 CHUNKED store: 7,730 snapshots → 33,557 chunk rows
  │     • per-snapshot score = MAX cosine over its chunks + whole-doc vector  (store.py:637-649)
  │     • floor: cos >= junk_floor (0.40 GLOBAL)   (retrieval.py:401/405)   ← only floor in the whole path
  │
  ├─ KEYWORD lane ─ TF-IDF + boosts, top 40, floor = "score > 0"  (fossils.py:1390/1434)
  │     • +recency(2.0) +technical(3.0) +bigram(5.0) +trigram(10.0) boosts
  │     • NO cosine gate, NO semantic relevance check
  │
  ├─ RRF FUSE  score(d)=Σ 1/(60+rank)  → keyword & semantic merged into ONE list  (retrieval.py:426-429)
  ├─ RECENCY tie-break  +weight·2^(-age/90d), weight=0.005  (retrieval.py:440)
  ├─ RERANK (Vertex semantic-ranker, LIVE via sidecar) — reorders top-40, then DISCARDS scores → 1/(60+new_rank)  (retrieval.py:283)
  ├─ RECENCY re-applied
  ├─ MMR  λ=0.85, protect_top=3 (0 for semantic-only); keyword-only ids get a ZERO vector → immune to diversity penalty  (retrieval.py:473-480)
  └─ return top-k (k=10) — NO relevance floor on output → fill-to-k  (retrieval.py:488)
```

**Delivered `similarity` field is rank-space, not cosine.** Values sit in 0.0145–0.0217 (= `1/(60+rank)` + recency). A junk snapshot at cos 0.42 and a perfect hit at cos 0.88 both emerge as ~`1/(60+rank)`. The number callers see carries **zero** relevance information (`task_routes.py:278-280` even admits the key name is legacy). This is why the tail is invisible to any score-based filter — and why Brandon "seeing 0.62 in the logs" was chasing a value that gates nothing.

---

## Root-cause hierarchy (ranked by measured contribution)

| # | Cause | Locus | Evidence / data |
|---|---|---|---|
| **1** | **Keyword lexical-only injection via RRF fusion** — keyword IDs enter the *relevance* ranking with no semantic gate; a lexical match ranks equal to a semantic hit. Causes the noise tail AND evicts genuine relevants. | `retrieval.py:411-429` | 28–31 of 40 keyword candidates were lexical-only across 3 queries; production ranks 4–10 were ~all keyword-only; Q1 pushed genuine-relevant SNAP-...-4358 (cos 0.64) OUT of top-10 |
| **2** | **Fill-to-k with no output relevance floor** — always returns k=10 even when only 2–3 are relevant. | `retrieval.py:473-488` | No score-cutoff anywhere after fusion; `mmr_select` loops until `len(picked)<k` |
| **3** | **Reranker absolute score discarded** — Vertex scores computed then remapped to rank-space; the one signal that could prune is thrown away. Also passages are 4096-char head-cuts → false-negatives on long snapshots. | `retrieval.py:265-283`; `rerank.py:830` | Vertex returns 0-1 `score` (captured at `retrieval.py:265`), never floored; clearly-relevant SNAP-...-8066 (cos 0.72) scored rerank 0.026 → dropped |
| **4** | **Semantic floor miscalibrated & per-model floor inert** — live floor is global 0.40 vs the active model's own calibrated 0.55 / relevance band 0.62; `registry_floor_enabled=false`. Max-pool chunk aggregation + whole-doc vector broadens admission. | `retrieval.py:161-166,401`; `registry.py:76`; `store.py:637-649` | 0.40 drops nothing real (off-topic sits ~0.51); but this is a *minor* lever — cosine overlaps in 0.70–0.74 zone can't be split by any floor |
| **5** | **MMR zero-vector exemption for keyword-only IDs** — keyword-only candidates get a zero vector, so `max_sim=0` → never diversity-penalized, never prunable as near-dupes; float on rank alone into ranks 4+. | `retrieval.py:474-478` | Compounds #1 |
| **6** | **Double recency** — RRF-stage tie-break (0.005) + keyword channel's own +2.0 positional recency bonus. | `retrieval.py:440`; `fossils.py:1379` | Minor recency bias in the tail |

---

## Refuted / de-prioritized hypotheses (stop chasing these)

- **Task-type mismatch** — REFUTED. Queries use `retrieval_query`, docs `retrieval_document` (`providers.py:46,159`). Geometry is clean.
- **Missing/bad normalization or MRL truncation** — REFUTED. L2-normalized both sides; no dimension truncation; full 3072-dim (`store.py:437-439,631-633`).
- **Coverage / "~8% of snapshots unrecallable"** — REFUTED for the active store. Chunker covers the whole doc, no truncation; live missing=0. (Only true for the *inactive* schema-1 whole-doc store.)
- **`semantic_threshold` (0.60/0.62) is the floor** — REFUTED. It is display/log-only everywhere (`search.py:52-56`, `fossils.py:463-471`). Tuning it changes nothing.
- **The "arguments hub" leaks via semantic similarity** — REFUTED on the active store (cos 0.5143, rank 2188, never delivered). That was the old whole-doc store's pathology.
- **Cross-model vector contamination** — REFUTED for normal search (single active store, dims-guarded). Bounded residual = migration "raced" rows only.
- **`gemini-embedding-2` = `gemini-embedding-002`** — REFUTED. The slug maps to API literal `models/gemini-embedding-2` (`registry.py:55`). *Action:* confirm against Google's real catalog whether this is a distinct multimodal/preview endpoint vs the GA text model — a different text-retrieval geometry could subtly widen the band. (Google's GA text model is `gemini-embedding-001`, deprecating 2026-07-14.)

---

## Fix levers to evaluate (ranked; DO NOT implement before measuring)

- **A — Demote keyword to a de-dup-only lane (highest leverage).** Keyword no longer contributes to the relevance ranking; it only (a) dedups and (b) optionally rescues *exact-identifier* queries via a gated union that must still clear a semantic cosine gate before entering results. Directly implements Brandon's ask; the probe attributes ~all delivered noise to this. Trade-off: recall on pure-lexical queries (rare IDs, error strings) — mitigate with a gated union, not full removal.
- **B — Kill fill-to-k; allow returning fewer than k.** Add a genuine output relevance floor and let the delivered count float below k. "3 rock-solid results" beats "3 + 7 distractors."
- **C — Preserve the reranker's absolute relevance instead of flattening to rank-space,** and apply a *calibrated* absolute floor (Vertex 0-1). Caveat from data: Vertex scores currently overlap and false-negative long snapshots — likely aggravated by the 4096-char head-cut passage; fix passage construction (or send the best-chunk text) before trusting a rerank floor.
- **D — Enable `registry_floor_enabled` so the active model uses its calibrated 0.55 (cheap, minor).** Buries the truly-off-topic (~0.51) tail; will NOT resolve the 0.70–0.74 overlap. One-line config lever, useful in combination, not alone.
- **E — Remove the MMR zero-vector exemption / double recency** (secondary cleanups).

`A + B` together are the core of the fix; `C` is the precision multiplier once passages/calibration are sound; `D/E` are cheap adjuncts.

---

## Test methodology — why the current harness is blind, and what to build

**Existing harness (`eval/run_bench.py`, 503-query labeled set):** measures **RECALL only** — each query is synthesized from ONE gold snapshot's own text; success = "does gold appear in top-10." With a single gold per query it **structurally cannot see the noise tail** (2 golds + 8 junk scores identically to 2 golds + 8 blanks). Every "gate passed" to date says nothing about precision. It *does* already drive the real `retrieve()` and can run the semantic-only lane in isolation (`include_keyword=False`), and can sweep recency/mmr/candidate_n — but **junk_floor is the one knob it can't sweep** and there is **no negative/noise instrument** at all.

**Build three things (in order):**

1. **A precision / noise-tail set (the missing measurement).** Take 20–30 *real* Brandon queries (the NSA one, the semantic-search one, etc.), run the live pipeline, and hand-label each returned snapshot relevant/irrelevant → gives **precision@k** and a literal **noise-tail count** the single-gold set cannot produce. This is the metric that maps to the complaint.
2. **A negative-distribution / noise-floor harness (Brandon's "where does relevance live").** Sample random `(query, unrelated-snapshot)` pairs *and* capture the actual tail the live pipeline returns, score cosines + Vertex rerank through the SAME provider/store, and plot signal vs noise distributions. **Honest expectation from the probe: the distributions OVERLAP — there is no clean valley.** That negative result is itself the finding that redirects effort from "tune the floor" to "fix the fusion/fill-to-k architecture."
3. **A/B lane arms (already wired).** Compare `gemini2-semantic` (include_keyword=False) vs `gemini2-hybrid` (True) on the new precision set to quantify exactly how much noise the keyword lane's RRF fusion injects — the direct test of lever A. Add a `--junk-floor` sweep (follow the existing `--recency-sweep` in-process-CFG pattern) paired with the precision metric.

Run with `Orchestrator/venv/bin/python eval/run_bench.py` (direct import, no live service needed; cached arms are key-free, new negative pairs need GOOGLE_API_KEY).

---

## Measured results (harness run, 2026-07-07)

Built `eval/noise_probe.py` (+ `eval/noise_queries.jsonl`) — a read-only precision/noise-tail harness that replicates `retrieve()`'s candidate generation and attributes every *delivered* result to its channel. Run over **15 real Brandon queries** (150 delivered slots) against the live pipeline:

| Metric | Value |
|---|---|
| **Keyword-only (lexical) delivered** | **100/150 = 67%** (mean per-query 67%) |
| **Genuine semantic hits evicted by keyword fusion** | **115** (~7.7/query) |
| Vertex rerank score — keyword-only vs semantic (median) | 0.017 vs 0.063 (overlap: kw max 0.225 > sem min 0.009) |

Two-thirds of what Brandon receives is keyword-only lexical noise that never cleared any semantic gate; per-query keyword-only ran from 30% up to **90%** (e.g. "operator picker device unassign", "generation ember backdrop"). The reranker's absolute scores *do* separate on the median (semantic ~4× the keyword-only median) but overlap at the edges — a post-rerank floor (~0.03–0.05) is a promising lever but not a perfectly clean cut. Full worksheet: `eval/results/2026-07-07-noise-tail.md`.

**Signal/noise calibration** (`--calibrate`, 200 positive/negative pairs):

| pctile | positive (query→gold) | negative (query→random) |
|---|---|---|
| 5% | 0.6113 | 0.4600 |
| 10% | 0.6306 | 0.4790 |
| 50% | 0.7218 | 0.5508 |
| 90% | 0.7788 | 0.6189 |
| 95% | 0.7936 | 0.6393 |

Verdict: **OVERLAP — no clean floor** (neg p90 0.619 ≈ pos p10 0.631; real overlap 0.61–0.64). Current `junk_floor=0.40` sits at the ~1st percentile of *random noise* — it drops nothing. A cosine floor ~0.60 is the ceiling of what thresholding can do (secondary lever D); positives are leakage-biased so calibrate conservatively and gate on recall. Data: `eval/results/2026-07-07-noise-calibration.json`.

**Harness usage:**
```bash
Orchestrator/venv/bin/python eval/noise_probe.py                 # noise-tail + channel attribution + worksheet
Orchestrator/venv/bin/python eval/noise_probe.py --calibrate --neg 200   # signal/noise distributions
```

## Ground-truth config reference (live values)

| Knob | Code default | config.ini | Effective (live) | Scale |
|---|---|---|---|---|
| `candidate_n` | 40 | 40 | 40 | per-channel pull depth |
| `rrf_c` | 60 | 60 | 60 | RRF `1/(c+rank)`; span across 40 ≈ 0.0066 |
| `recency_weight` | 0.005 | 0.005 | 0.005 | additive tie-break |
| `mmr_lambda` | 0.85 | 0.85 | 0.85 | relevance↔diversity |
| `mmr_protect_top` | 3 | 3 | 3 (hybrid) / 0 (semantic-only) | seeded top ranks |
| `junk_floor` | 0.40 | 0.40 | **0.40 (the only live floor; raw cosine)** | cosine, semantic lane only |
| `registry_floor_enabled` | False | false | **false → per-model 0.55 INERT** | bool |
| `rerank_enabled` | false | false | **TRUE via `Manifest/embeddings/rerank.json` sidecar** | bool |
| rerank provider | null | null | **vertex** (`semantic-ranker-default-004`) | enum |
| `rerank_candidate_n` | 40 | 40 | 40 | rerank pool |
| `passage_chars` | 4096 | 4096 | 4096 | rerank passage head-cut |
| `semantic_threshold` (ctx 0.60 / registry 0.62) | — | 0.60 | **INERT (display/log-only)** | cosine, dead |
| `final_k` | — | 10 | **DEAD KNOB (unread)** | — |
| chat: semantic/keyword/recent/checkpoint per user | 8/4/5/2 | 6/3/5/2 | 6/3/5/2 | chat-context counts (SEPARATE lanes) |

**Note — the chat-context path is different and already correct:** `build_fossil_context()` runs semantic (`include_keyword=False`, cosine-floored) and keyword as **separate labelled sections** and returns short rather than padding. So the fix locus is the **`search_snapshots` tool / `retrieve(include_keyword=True)` path**, not chat-context assembly.

---

## Appendix — prior-session claims adjudicated against code

| ID | Claim | Verdict |
|---|---|---|
| C1 | `semantic_threshold` is dead code in ranking | **CONFIRMED** |
| C2 | per-model junk_floor gated behind `registry_floor_enabled=false` → "no floor active" | **PARTIALLY TRUE** — per-model floor inert, but global 0.40 IS active |
| C3 | junk_floor operates on fused RRF rank-space | **REFUTED** — it's raw cosine, pre-fusion |
| C4 | reranker only reorders, no absolute cutoff, fill-to-k | **CONFIRMED** |
| C5 | reranker disabled by default / Cohere | **REFUTED** — live ON via sidecar, provider = Vertex |
| C6 | keyword lane has no floor; RRF promotes lexical-only matches | **CONFIRMED** (primary) |
| C7 | recency 0.005 / mmr 0.85 / protect 3; MMR injects off-topic at ranks 4+ | **CONFIRMED** |
| C8 | query=retrieval_query, doc=retrieval_document | **CONFIRMED** |
| C9 | `similarity` field is fused/RRF, not cosine | **CONFIRMED** |
| C10 | chunked snapshot score = MAX over chunks | **CONFIRMED** (incl. whole-doc vector at ordinal 0) |
| C11 | `gemini-embedding-2` = `gemini-embedding-002` | **REFUTED** — maps to `models/gemini-embedding-2` |
| C12 | ~8% unrecallable over char limit | **REFUTED** for active chunked store |
