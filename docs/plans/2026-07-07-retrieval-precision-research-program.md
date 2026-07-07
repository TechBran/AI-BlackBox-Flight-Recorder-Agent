# Retrieval Precision — Research Program (to a portable, production-quality fix)

**Owner:** Brandon-DEV · **Started:** 2026-07-07 · **Status:** IN PROGRESS (no production pipeline code changed until the fix is planned & approved)

## Goal (Brandon's words, made precise)

Install the system on *any* box, pick an embedding model + a reranker, and get **correct search results with no per-box hand-tuning** — high **recall** (the right snapshot is always retrievable) *and* high **precision** (no irrelevant tail). The fix must be an **architecture that self-calibrates per (embedding model, reranker)**, not a magic threshold tuned to this box. The audit proved thresholds are box/model-specific and overlapping, so "set junk_floor=0.60 here" is a non-answer.

## What the audit already established (see `docs/onboarding/2026-07-07-semantic-search-pipeline-audit.md`)

Measured root cause of the noise, on 15 real queries: **67% of delivered results are keyword-only lexical injection** (RRF-fused keyword lane, `retrieval.py:426-429`), **115 genuine semantic hits evicted**, no output relevance floor (fill-to-k, `:473-488`), and the **reranker's absolute score is computed then discarded** (`:283`). Semantic geometry itself is clean (task-types, normalization, coverage all correct). No clean cosine valley exists (relevant 0.63–0.79 vs noise 0.48–0.62, overlapping).

## Instruments

| Instrument | Purpose | Status |
|---|---|---|
| `eval/noise_probe.py` | deterministic channel attribution + eviction + calibration on real queries | **built + run** |
| Blind LLM-judge panel | split the 67% keyword-only into true-noise vs genuine-recovery (precision@k on real corpus) | **Phase 0 running** |
| `eval/run_bench.py` | recall@k / MRR (existing; drives real `retrieve()` via store/query_vector seam) | exists |
| **Ground-truth benchmark corpus** | controlled corpus with KNOWN relevance + designed distractors → true precision/recall/nDCG | **Phase 1 (to build)** |
| **Ranking sandbox** | parameterized re-impl of the pipeline (every lever a knob), validated == production on default | **Phase 2 (to build)** |

## Phases

### Phase 0 — Noise vs recovery (running)
Blind LLM-judge the 150 delivered results; cross-tab relevance × channel. Output: what fraction of the keyword-only 67% is *irrelevant noise* vs *genuine recovery the semantic lane missed*. **Decides the primary lever**: mostly-noise → demote/gate keyword (A); meaningful-recovery → keep keyword but add a rerank floor (C).

### Phase 1 — Ground-truth benchmark corpus
The real corpus has no relevance labels; synthesized-from-gold queries are leakage-biased. Build a **controlled corpus** (dropped under the `test` operator so it's isolated + rebuildable, or embedded into a scratch store via the run_bench seam) that reproduces the real corpus's pathologies:
- **Topic clusters**: N snapshots genuinely about a topic (the gold set for that topic's queries).
- **Lexical decoys**: snapshots that share rare tokens/proper-nouns with a topic but are about something else (stress the keyword-injection failure — these must NOT be returned).
- **Near-miss neighbors**: adjacent-topic snapshots (stress the cosine overlap zone).
- **Hub/broad snapshots**: long, topically-diffuse (stress hubness).
- **Length + envelope realism**: short/long, real BlackBox envelope (embedded content_mode=full), checkpoints.
- **Queries authored independently** of snapshot bodies (from topic intent, ideally a *different* model — Gemini 3.5 Flash, free) to avoid leakage bias; each with a KNOWN gold set + known distractor set.
Yields true **precision@k, recall@k, nDCG@k, noise-count, eviction-count**.

### Phase 2 — Ranking sandbox
A pure-function, parameterized re-implementation of the pipeline (reusing `retrieval.py`'s `rrf_fuse`/`apply_recency_tiebreak`/`mmr_select`) exposing every lever as a knob, **validated to reproduce production `retrieve()` byte-for-byte on the default config**. Lets us A/B levers offline against Phase-1 without touching production.

### Phase 3 — Lever sweep → best config
Evaluate each lever + combination on the Phase-1 benchmark (and cross-check on the real corpus via the Phase-0 judge):

| Lever | Variants to test |
|---|---|
| **A — keyword role** | fused RRF (current) · **gated** (keyword candidate must clear a semantic cosine gate to enter ranking) · **dedup-only** (keyword only merges/boosts already-semantic hits, never contributes rank) · off |
| **B — output floor + variable-k** | none (fill-to-k) · relevance floor → return fewer than k |
| **C — rerank as relevance** | discard score (current) · **preserve absolute rerank score as relevance** · **absolute rerank floor** (calibrated) · passage-construction fix (best-chunk window vs 4096-char head-cut) |
| **D — semantic admission** | global 0.40 · per-model calibrated floor (registry_floor_enabled) · pooling: max (current) vs mean-of-top-k chunks |
| **E — cleanups** | MMR zero-vector exemption for keyword-only ids · keyword-channel double-recency (+2.0) |

Metric set per config: precision@k, recall@k, nDCG@k, delivered-noise%, eviction-count, delivered-count-distribution.

### Phase 4 — Portability matrix (the production requirement)
Run the winning architecture across **embedding models × rerankers**: `gemini-embedding-2`, `gemini-embedding-001`, `qwen3-0.6b` (local) × `vertex`, `cohere`, `none`. The method must pass on ≥2 models × ≥2 rerankers using **only auto-calibration** — i.e. an install-time calibration probe (extend `noise_probe --calibrate` + a rerank-score calibration) that derives the floors per (model, reranker) from a handful of pairs, writing them to the registry. No manual per-box tuning.

### Phase 5 — Real-corpus validation + the fix PLAN
Re-run the noise_probe + judge before/after the winning config on the real corpus (external validity). Then write the **production fix plan** (plan-first, superpowers: brainstorm → writing-plans) with the winning architecture, the self-calibration step, per-box portability, and the harness numbers as **acceptance gates**.

## RESULTS — Phases 0–3 (measured)

**Phase 0 (precision judge, real corpus):** precision@10 = **30%**; **45% outright noise**, of which **90% keyword-only**; cross-channel agreement = **85% relevant**; reranker score separates noise (median 0.013) from real hits (0.04–0.10). → `eval/results/2026-07-07-precision-judge.md`.

**Phase 1 (ground-truth corpus):** 64 snapshots (48 golds / 12 engineered lexical decoys / 4 hubs), 24 labeled queries, Gemini-3.5-Flash authored. `eval/ground_truth/`.

**Phase 3 (lever sweep on ground truth, `eval/rank_sandbox.py`, gemini-embedding-2 + Vertex):**

| config | precision | recall | nDCG | noise/q | decoy/q | delivered |
|---|---|---|---|---|---|---|
| baseline (production) | 0.20 | 0.51 | 0.57 | 7.96 | 0.25 | 10.0 |
| A: keyword=dedup | 0.20 | 0.51 | 0.57 | 7.96 | **0.08** | 10.0 |
| D: semantic_floor=.60 | 0.20 | 0.50 | 0.57 | 8.00 | 0.25 | 10.0 |
| C: rerank preserve+floor.02+varK | 0.40 | **0.99** | **0.88** | 6.00 | 0.29 | 10.0 |
| A+B+C dedup+floor.02+varK | 0.50 | 1.00 | 0.88 | 4.33 | 0.29 | 8.3 |
| **FULL gated+floor.03+semfloor.55+varK** | **0.65** | **1.00** | **0.88** | **2.42** | 0.17 | **6.4** |

*(precision is capped at 0.40 for the fixed-k configs because each query has 4 golds and k=10 — only variable-k can exceed it, which is the point.)*

**Findings:**
- **C (preserve rerank score + absolute floor) is the transformative lever:** recall 0.51→~1.0, nDCG 0.57→0.88. It recovers recall the pipeline was discarding.
- **A (keyword dedup/gated) removes decoy injection** (0.25→0.08/query) — the keyword-only noise.
- **B (variable-k) is what breaks the precision cap** and shrinks delivered 10→6.4.
- **FULL stack:** precision **3.3×**, recall **0.51→1.00**, noise **−70%**.
- **D (semantic floor) alone does nothing** and can *worsen* keyword noise (fewer semantic candidates → keyword dominates the fused list).

**Mechanism validation (rules out artifact):** on the same corpus, the **Vertex reranker's own top-10 (rerank all 64) = recall 1.00** — it identifies every gold — while the production pipeline (rank-space remap + MMR) delivers only **0.36–0.51**. The pipeline discards recall the reranker already had; `preserve` recovers it. → `eval/results/2026-07-07-sandbox-sweep.json`.

**Phase 4 (portability + auto-calibration, `eval/portability_sweep.py`) — DONE.** Each model embeds the corpus into its own store, derives its OWN floors (cos_floor = gold-cosine p10), and is tested with only those numbers:

| model | geometry gold/nongold med | cos_floor (auto) | baseline p/r | FIXED+rerank p/r | FIXED-noRerank p/r |
|---|---|---|---|---|---|
| gemini-embedding-2 | 0.71 / 0.51 | 0.63 | 0.20 / 0.51 | 0.73 / 0.86 | 0.64 / 0.91 |
| gemini-embedding-001 | 0.72 / 0.59 | 0.68 | 0.22 / 0.54 | 0.74 / 0.85 | 0.67 / 0.91 |
| qwen3-0.6b (local) | 0.62 / 0.22 | 0.49 | 0.20 / 0.50 | 0.73 / 0.84 | 0.67 / 0.91 |

**The fix is portable: identical ~0.73/0.85 (reranker) and ~0.67/0.91 (no reranker) across three very different geometries, using only per-model auto-derived floors.** A hardcoded floor would break qwen (gold p10=0.49 vs Gemini 0.63). Auto rerank_floor (gold-p10=0.083) favors precision (0.73) at a small recall cost — the calibration *percentile* is the precision/recall dial. → `eval/results/2026-07-07-portability.json`.

**Program status: Phases 0–4 COMPLETE. Fix validated + portable. Remaining: Phase 5 (build per the fix plan, on approval) + real-corpus before/after via the judge + candidate_n sizing on the 7.7k corpus.**

## Acceptance gates (definition of done)
- Delivered-noise (irrelevant per LLM-judge / benchmark) → **~0%** (from 67%).
- Eviction of genuine semantic hits → **0** (from 115).
- Recall@k on `run_bench.py` and Phase-1 benchmark → **no regression**.
- Variable-k: returns **fewer than k** when fewer are relevant (no padding).
- Portable: same architecture + auto-calibration passes across ≥2 embedding models × ≥2 rerankers.
- Zero manual per-box tuning; install → pick model + reranker → correct results.

## Guardrails
- **No production pipeline edits** until Phase 5 plan is approved (levers are tested in the sandbox / behind seams only).
- This is the dev/staging box — free to embed, drop test-operator snapshots, restart, probe.
- Prove with data; every claim cites a measured number.
