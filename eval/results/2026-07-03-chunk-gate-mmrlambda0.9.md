# M6f chunk-store gate — 2026-07-03 (runbook step 3)

Candidate: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Manifest/embeddings/_build2/gemini-embedding-2` (slug gemini-embedding-2, schema 2, rows 32756, snapshots 7601, generation 60).
Gate baselines: `fresh same-config active-store runs (pipeline overrides active; the sweep-JSON baselines were measured at the default config)` w=0.005 entry (gates 1–3) + fresh same-config active-store runs (gate 4 tail-third — the sweep JSON carries no position strata). 503 labeled rows, k=10, full retrieve() pipeline via the M4 store-override seam; active store at 7906 snapshots during the run.
IN-PROCESS pipeline overrides: `{'mmr_lambda': 0.9}` (config.ini untouched, byte-asserted; BOTH the candidate and the fresh baseline arms — and the gate-6 pytest run — executed at these values, so every gate compares identical pipeline config).

## Gates

| # | gate | baseline | candidate | verdict |
|---|---|---|---|---|
| 1 | hybrid overall r@10 >= baseline (no regression) | 0.5348 | 0.5229 | FAIL |
| 2 | semantic overall r@10 >= baseline (no regression) | 0.5408 | 0.5507 | PASS |
| 3a | hybrid >10k-band r@10 MUST IMPROVE (>) | 0.6867 | 0.72 | PASS |
| 3b | semantic >10k-band r@10 MUST IMPROVE (>) | 0.4333 | 0.5867 | PASS |
| 4a | hybrid tail-third r@10 MUST IMPROVE (>) | 0.6019 | 0.6408 | PASS |
| 4b | semantic tail-third r@10 MUST IMPROVE (>) | 0.5437 | 0.6117 | PASS |
| 5a | hybrid human holdout pairs still hit (hits@10) | 3/3 | 1/3 | FAIL |
| 5b | semantic human holdout pairs still hit (hits@10) | 3/3 | 3/3 | PASS |
| 6 | golden + lean-profile suites vs candidate (test_retrieval_golden.py + test_local_lean_retrieval.py) | all pass | FAILURES | FAIL |

**VERDICT: GATE FAILED — STOP, no cutover**

Notes:
- gate 4a: baseline measured fresh at the gate weight (sweep JSON has no position strata)
- gate 4b: baseline measured fresh at the gate weight (sweep JSON has no position strata)

## By span position (r@10 / MRR / n)

| run | None | head | middle | tail |
|---|---|---|---|---|
| baseline-hybrid | 1.0 / 1.0 / 3 | 0.5333 / 0.4003 / 120 | 0.5054 / 0.3843 / 277 | 0.6019 / 0.4668 / 103 |
| baseline-semantic | 1.0 / 1.0 / 3 | 0.4 / 0.2697 / 120 | 0.5957 / 0.3981 / 277 | 0.5437 / 0.3695 / 103 |
| candidate-hybrid | 0.3333 / 0.3333 / 3 | 0.5167 / 0.4314 / 120 | 0.4838 / 0.3286 / 277 | 0.6408 / 0.4683 / 103 |
| candidate-semantic | 1.0 / 0.5556 / 3 | 0.4917 / 0.3625 / 120 | 0.5487 / 0.3551 / 277 | 0.6117 / 0.4026 / 103 |

## By length band (r@10 / MRR / n)

| run | 6-10k | <6k | >10k |
|---|---|---|---|
| baseline-hybrid | 0.6782 / 0.5821 / 87 | 0.4023 / 0.3098 / 266 | 0.6867 / 0.4834 / 150 |
| baseline-semantic | 0.5517 / 0.4173 / 87 | 0.5977 / 0.402 / 266 | 0.4333 / 0.2698 / 150 |
| candidate-hybrid | 0.6322 / 0.4948 / 87 | 0.3759 / 0.2665 / 266 | 0.72 / 0.5207 / 150 |
| candidate-semantic | 0.6552 / 0.4437 / 87 | 0.4962 / 0.3405 / 266 | 0.5867 / 0.372 / 150 |

## Holdout (human-verified pairs, rank@10 or miss)

| run | hits@10 | detail |
|---|---|---|
| baseline-hybrid | 3/3 | SNAP-20260606-6930: 1, SNAP-20260427-6316: 1, SNAP-20260427-6313: 1 |
| baseline-semantic | 3/3 | SNAP-20260606-6930: 1, SNAP-20260427-6316: 1, SNAP-20260427-6313: 1 |
| candidate-hybrid | 1/3 | SNAP-20260606-6930: miss, SNAP-20260427-6316: miss, SNAP-20260427-6313: 1 |
| candidate-semantic | 3/3 | SNAP-20260606-6930: 6, SNAP-20260427-6316: 2, SNAP-20260427-6313: 1 |
