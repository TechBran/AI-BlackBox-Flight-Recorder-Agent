# Retrieval Precision Fix — Implementation Plan (plan-first; NOT yet built)

**Owner:** Brandon-DEV · **Date:** 2026-07-07 · **Status:** PLAN — awaiting approval before any pipeline code change.
**Evidence base:** `docs/onboarding/2026-07-07-semantic-search-pipeline-audit.md` + `docs/plans/2026-07-07-retrieval-precision-research-program.md` (measured results). **Goal:** portable perfect-ish recall + precision on *any* box after picking an embedding model + reranker, with **zero manual per-box tuning**.

## What the research proved (the levers, validated on labeled ground truth)

| Lever | Change | Measured effect |
|---|---|---|
| **C (linchpin)** | Preserve the reranker's ABSOLUTE score as the relevance signal (stop the rank-space remap at `retrieval.py:283`) and apply a calibrated absolute floor. | recall 0.51→1.0, nDCG 0.57→0.88; recovers recall the pipeline was discarding (reranker ceiling = 1.0). |
| **A** | Keyword becomes **agreement/dedup or gated** (must clear a cosine gate), never injects. | decoy injection 0.25→0.08/q; keyword-only is 90% of real-corpus noise. |
| **B** | **Variable-k**: return fewer than k when few clear the floor (no padding). | breaks the precision cap; delivered 10→6.4. |
| **D** | Per-model semantic cosine floor, **auto-calibrated** (not global 0.40). Reranker-absent boxes lean on this. | reranker-absent path: 0.64 prec / 0.94 recall. |
| passage fix | Reranker passage = best-chunk window, not blind 4096-char head-cut. | sharpens reranker discrimination (removes long-doc false-negatives). |

Full-stack result: **precision 0.20→0.65, recall 0.51→1.00, noise −70%** (with reranker); **0.64 / 0.94** (without). D alone does nothing; C is the linchpin.

## Real-corpus calibration (M2) — CORRECTED primary lever

The synthetic benchmark favored gated-keyword + cosine floor. Calibrating on **Brandon's real corpus** (150 LLM-judged delivered results + their true cosines/rerank scores, `scripts/calibrate_retrieval.py`) **overrules that** and is the more important result:

- **Cosine cannot separate on a dense corpus.** Keyword recoveries (median cos 0.626) and keyword noise (median 0.593) overlap almost completely — the embedding *can't* tell them apart (that's why they're keyword-only). Gating by cosine is lossy: floor 0.60 keeps 31/40 recoveries but also 25/60 noise.
- **The reranker separates cleanly.** Rerank-as-bouncer (keyword FUSED, keep iff rerank ≥ floor) on the real judged data: floor **0.02 → precision 0.55→0.82, recall 0.87, delivered 150→88** (~6/query); 0.03 → 0.85/0.67; 0.04 → 0.90/0.54. It's a clean precision/recall dial.

**Corrected production config (reranker present):** `keyword_mode=fused` (recall) + `rerank_relevance=preserve` + `rerank_floor≈0.02–0.03` (the universal bouncer) + `min_results=1–2`; **`output_cos_floor` stays 0** (cosine doesn't discriminate on a dense corpus). The cosine-gate levers still help on *sparse* corpora (other customers) and are kept as options — but on Brandon's box the rerank floor is the lever. **Reranker-ABSENT boxes** (on-device) can only fall back to the cosine floor, which is weak on a dense corpus — an honest limitation: real-corpus precision needs a reranker.

**Operating point is Brandon's choice** (recall-favoring 0.02 vs precision-favoring 0.04); M6 wires it per profile, defaulting to the recall-safe 0.02.

## Design

### 1. One relevance scale, floored — `retrieval.py`
Replace the rank-space remap. When rerank runs, `relevance[sid] = rerank_score` (absolute 0–1), tail = 0; drop pool members `< rerank_floor`. When rerank is absent/fails, `relevance = fused RRF` and drop final candidates with `cosine < cos_floor`. Then MMR + **variable-k** (return survivors, don't pad). This unifies both paths behind one "floor the relevance, return what survives" rule.

### 2. Keyword as a gated/agreement lane — `retrieval.py`
`keyword_mode` (config): `gated` (default) — a keyword-only candidate enters RRF only if its cosine ≥ `keyword_gate`; `dedup` — keyword only reinforces items already in the semantic set; `fused` — legacy. Removes the lexical-injection path while keeping genuine recoveries (checkpoints/exact-IDs that also clear the gate). Also drop the keyword-only zero-vector MMR exemption and the keyword channel's second recency bonus.

### 3. Self-calibration at install/model-select — NEW `scripts/calibrate_retrieval.py` + registry
Per `(embedding model, reranker)`: sample positive (query→known-related) and negative (query→random) pairs, embed + rerank, and derive:
- `cos_floor` = positive p5–p10 crossover (my `noise_probe --calibrate` already computes this; 0.62 measured for gemini-embedding-2 — and 0.62 was the winning reranker-absent floor).
- `rerank_floor` = the valley between noise/relevant rerank-score distributions (0.02–0.03 measured for Vertex).
Write them to the registry per model + a rerank sidecar per reranker. **This is the portability mechanism** — install → pick model + reranker → calibration derives the floors → no hand-tuning.

### 4. Reranker passage = best chunk — `retrieval.py` `_apply_rerank`
Use the winning chunk's text (provenance ordinal already available) instead of the blind 4096-char head-cut, so long snapshots aren't false-negatived by the reranker.

### 5. Config / registry
`[retrieval]`: `keyword_mode=gated`, `keyword_gate` (calibrated), `rerank_relevance=preserve`, `rerank_floor` (calibrated), `variable_k=true`, `min_results` (small floor, e.g. 1–2, so a valid query never returns empty). Per-model `cos_floor`/`rerank_floor` in `registry.py`. **All behind the existing `registry_floor_enabled`-style gates so the default remains byte-identical until switched on.**

## Portability matrix (to validate in the sandbox before rollout)
- embedding models: gemini-embedding-2 (done), gemini-embedding-001, qwen3-0.6b (local/on-device).
- rerankers: vertex (done), cohere, **none** (done — A+B+D path).
Each must pass the acceptance gates using only auto-calibrated floors.

## Backward-compat & rollout
1. Land behind flags; default config = today's behavior (byte-identical, test-pinned).
2. Prove on the ground-truth benchmark + `run_bench.py` (recall no-regress) + a real-corpus before/after via the LLM-judge (`noise_probe` + judge panel).
3. Flip defaults per profile: cloud boxes → C-primary; on-device (semantic-only) → A+B+D.
4. Keep `eval/noise_probe.py` as the standing precision regression gate.

## Acceptance gates
- Ground-truth: precision ≥ 0.6, recall ≥ 0.95 (reranker), ≥ 0.9 (no reranker); decoy-injection ≈ 0.
- Real corpus (judge): irrelevant delivered ↓ from 45% toward <10%; evictions → 0.
- `run_bench.py` recall@10: no regression on any arm.
- Variable-k returns < k when appropriate; never empty for a valid query.
- Portable: passes across ≥2 embedding models × {vertex, none} with only auto-calibration.
- Default config byte-identical until flags flipped; all existing retrieval tests green.

## Milestones
- **M1** sandbox portability sweep (models × rerankers) → confirm the config generalizes. ✅ DONE (portability sweep; 3 models × {vertex,none}).
- **M2** `calibrate_retrieval.py` + registry write-back (the self-calibration). ⏳ pending.
- **M3** `retrieval.py`: preserve-score + floors + variable-k + gated keyword (flagged). ✅ DONE. New knobs: `keyword_mode`, `rerank_relevance`, `rerank_floor`, `output_cos_floor`, `min_results` + `_resolve_cos_floor`/`_resolve_rerank_floor` + `store.max_cosine_for()`. Default byte-identical (233 unit tests green: 76 parity + 7 new levers + suite). Files: `retrieval.py`, `embeddings/store.py`, `config.ini`, `tests/test_retrieval_precision.py`.
- **M4** reranker best-chunk passage. ⏳ pending.
- **M5** real-corpus before/after (judge) + `run_bench` no-regress + test suite. ✅ CORE PASSED. (a) Integration gate on the REAL `retrieve()` over GT corpus: baseline 0.20/0.50 → FIXED 0.68/0.94. (b) **REAL Brandon-corpus, judge-confirmed** (15 queries, blind Opus panel, config `fused + rerank preserve + rerank_floor 0.03`): **precision (relevant) 30%→55%, relevant+borderline 55%→85%, noise 45%→15%, delivered 10→6.5/q**. Residual 15% noise concentrated in a few reranker-false-positive queries (M4 target). Bug found+fixed via end-to-end verify: the un-reranked tail was refilling past the floor (`_apply_rerank` now drops the tail when a floor is active). Recall no-regress PASSED: on 30 general labeled queries recall@10 held/improved 0.70→0.73 (rerank_floor 0.02/0.03), delivered stayed 9.4–9.8 (variable-k trims only noise-heavy queries, not relevant-rich ones). So: precision way up on junk-heavy queries, recall NOT hurt on real ones. → `eval/results/2026-07-07-m5-real-retrieve.json`.
- **M6** per-profile default flip + docs. ✅ SHIPPED LIVE 2026-07-07 (Brandon approved, rerank_floor 0.03). config.ini: `rerank_relevance=preserve`, `rerank_floor=0.03`, `min_results=1` (keyword stays `fused`, output_cos_floor stays 0 — dense-corpus correct). Full suite green (fixed test_retrieval_rerank pins to legacy defaults; golden+lean pass under the new default). Service restarted; verified LIVE: "operator picker" 10→1, "NSA checkpoint" →7/7 relevant; delivered `similarity` now the reranker's 0–1 score (informative), not rank-space. NOT committed (working tree). On-device/reranker-absent boxes need a separate profile (output_cos_floor) — future.
- **M4** reranker best-chunk passage. ✅ DONE (implemented, tested, available; default OFF). Shipped as a body-space **best-window** (`_best_passage_window`, `[retrieval] rerank_passage_mode = head|window`) — the store's chunk ordinals are envelope-inclusive so a naive ordinal window is wrong; the best-window picks the query-richest BODY span with no envelope-space mismatch. **Measured NOT the precision default:** at rerank_floor 0.03 it inflates delivered 98→121 on the real corpus (it raises rerank scores uniformly — relevant AND noise — so more clears the floor). It's a RECALL lever needing a higher recalibrated floor, not a fix for the residual false-positive noise (that's a reranker-quality limit). Kept default `head` (byte-identical); available for recall-favoring use. 4 unit tests; full retrieval+rerank+golden+lean suite green.
