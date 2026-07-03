# Whole+chunk candidate re-gate — 2026-07-03 (M6f iteration 2)

Candidate: `Manifest/embeddings/_build2/gemini-embedding-2` (schema 2, rows 32756,
snapshots 7601, generation 60) — the iteration-1 chunk candidate augmented with a
WHOLE-snapshot vector at ordinal 0 of every multi-chunk group (max(whole, chunks)
scoring), built by `scripts/augment_candidate_wholevec.py` from the read-only
`_build` candidate. Transform actuals: 7,010 whole-doc embeds (439 provider calls,
38.7M chars ≈ ~10M tokens ≈ $2 at $0.20/1M), 591 single-chunk groups copied as-is,
0 quarantined, 0 body-missing, wall time 2,974s (~50 min). Verified: rows
32756 = 25746 old + 7010 whole vecs; index coverage 7601/7601 (0 late mints); all
7,601 group shapes checked (multi = n+1 rows, ordinals contiguous); 200 random
groups vector-verified (chunk rows byte-preserved at ordinals 1..n, whole ≠ chunk0).

Gate runs: `eval/run_bench.py --gate --candidate-dir Manifest/embeddings/_build2
--candidate-slug gemini-embedding-2 --out-date 2026-07-03 [--mmr-lambda ...]`.
Baselines for the λ-override runs are fresh same-config active-store runs (the
committed plumbing rebases them); config.ini byte-asserted untouched on every run.
Full artifacts: `2026-07-03-chunk-gate{,-mmrlambda0.8,-mmrlambda0.9}.{md,json}`.

## Six-gate tables

### BASE config (λ=0.7; gates 1–3 baselines = committed w=0.005 sweep)

| # | gate | baseline | candidate | verdict |
|---|---|---|---|---|
| 1 | hybrid overall r@10 >= baseline | 0.4891 | 0.4652 | FAIL |
| 2 | semantic overall r@10 >= baseline | 0.497 | 0.5209 | PASS |
| 3a | hybrid >10k r@10 must improve | 0.6333 | 0.66 | PASS |
| 3b | semantic >10k r@10 must improve | 0.42 | 0.5733 | PASS |
| 4a | hybrid tail-third must improve | 0.5437 | 0.6117 | PASS |
| 4b | semantic tail-third must improve | 0.4951 | 0.5728 | PASS |
| 5a | hybrid holdout hits@10 | 3/3 | 1/3 | FAIL |
| 5b | semantic holdout hits@10 | 3/3 | 3/3 | PASS |
| 6 | golden+lean suites vs candidate | all pass | 2 golden FAILURES | FAIL |

### λ=0.8 override (baselines rebased at 0.8)

| # | gate | baseline | candidate | verdict |
|---|---|---|---|---|
| 1 | hybrid overall | 0.507 | 0.4851 | FAIL |
| 2 | semantic overall | 0.5109 | 0.5308 | PASS |
| 3a | hybrid >10k | 0.66 | 0.6733 | PASS |
| 3b | semantic >10k | 0.44 | 0.58 | PASS |
| 4a | hybrid tail | 0.5631 | 0.6408 | PASS |
| 4b | semantic tail | 0.5049 | 0.5825 | PASS |
| 5a | hybrid holdout | 3/3 | 1/3 | FAIL |
| 5b | semantic holdout | 3/3 | 3/3 | PASS |
| 6 | golden+lean | all pass | 2 golden FAILURES | FAIL |

### λ=0.9 override (baselines rebased at 0.9)

| # | gate | baseline | candidate | verdict |
|---|---|---|---|---|
| 1 | hybrid overall | 0.5348 | 0.5229 | FAIL |
| 2 | semantic overall | 0.5408 | 0.5507 | PASS |
| 3a | hybrid >10k | 0.6867 | 0.72 | PASS |
| 3b | semantic >10k | 0.4333 | 0.5867 | PASS |
| 4a | hybrid tail | 0.6019 | 0.6408 | PASS |
| 4b | semantic tail | 0.5437 | 0.6117 | PASS |
| 5a | hybrid holdout | 3/3 | 1/3 | FAIL |
| 5b | semantic holdout | 3/3 | 3/3 | PASS |
| 6 | golden+lean | all pass | 2 golden FAILURES | FAIL |

## What iteration 2 fixed — and what it exposed

FIXED (the iteration-1 structural blocker): the SEMANTIC side is fully green at
every λ. The whole-doc vector recovered the short/diffuse-snapshot signal —
SNAP-20260606-6930 semantic: iteration-1 miss → rank 7 (λ=0.7); holdout 5b went
1–2/3 → 3/3 everywhere; gate 2/3b/4b margins widened.

STILL FAILING (all three configs, HYBRID arm only — gates 1, 5a, 6):
decomposed probe through production retrieve() against the candidate:

| query → gold | arm | λ=0.7 | λ=0.9 | λ=1.0 (MMR off) |
|---|---|---|---|---|
| AudioRecord SIGABRT → SNAP-20260606-6930 | hybrid | miss | miss | **3** |
| AudioRecord SIGABRT → SNAP-20260606-6930 | semantic | 7 | 6 | miss |
| UGV ZUPT → SNAP-20260427-6316 | hybrid | miss | miss | **3** |
| UGV ZUPT → SNAP-20260427-6316 | semantic | 2 | 2 | 4 |

Both golds sit at post-RRF hybrid rank 3 and are discarded by the MMR step at
every λ < 1.0 tested. Mechanism: search_with_vectors returns the group's
max-cosine BEST row as the MMR diversity vector — with whole-doc retention that
row is now frequently the ordinal-0 whole-doc vector, and whole-doc vectors of
near-duplicate sibling sessions (e.g. SNAP-20260630-7864 supersession chain,
SNAP-20260426-6293 adjacent ZUPT session) are highly mutually similar, so the
diversity penalty exceeds what rank-3 relevance can survive. Same mechanism
explains gate 1: candidate-hybrid trails its same-λ baseline by 0.024/0.022/0.012
(λ=0.7/0.8/0.9 — shrinking as MMR weakens) while candidate-semantic LEADS its
baseline everywhere. λ=1.0 is not a fix: it flips the semantic arm (6930 → miss
without MMR de-duplication) and iteration-1 showed 4a fails there.

## Recommendation

NO swept config passes all six gates — no cutover. The residual failure is
isolated and mechanical: MMR diversity should not be computed on ordinal-0
whole-doc vectors. Iteration-3 candidate fix (small, store-layer): on v2 stores,
`search_with_vectors` keeps max(whole, chunks) for the SCORE but returns the best
CHUNK row (best non-ordinal-0 row of multi-row groups) as the diversity vector —
decoupling the scoring win (proven above) from the MMR regression. Alternative:
RRF-agreement exemption (an item present in BOTH channels is immune to MMR drop).
