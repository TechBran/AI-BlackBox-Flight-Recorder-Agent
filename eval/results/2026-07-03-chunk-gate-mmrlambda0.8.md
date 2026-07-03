# M6f chunk-store gate — 2026-07-03 (runbook step 3)

Candidate: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Manifest/embeddings/_build2/gemini-embedding-2` (slug gemini-embedding-2, schema 2, rows 32756, snapshots 7601, generation 60).
Gate baselines: `fresh same-config active-store runs (pipeline overrides active; the sweep-JSON baselines were measured at the default config)` w=0.005 entry (gates 1–3) + fresh same-config active-store runs (gate 4 tail-third — the sweep JSON carries no position strata). 503 labeled rows, k=10, full retrieve() pipeline via the M4 store-override seam; active store at 7906 snapshots during the run.
IN-PROCESS pipeline overrides: `{'mmr_lambda': 0.8}` (config.ini untouched, byte-asserted; BOTH the candidate and the fresh baseline arms — and the gate-6 pytest run — executed at these values, so every gate compares identical pipeline config).

## Gates

| # | gate | baseline | candidate | verdict |
|---|---|---|---|---|
| 1 | hybrid overall r@10 >= baseline (no regression) | 0.507 | 0.4851 | FAIL |
| 2 | semantic overall r@10 >= baseline (no regression) | 0.5109 | 0.5308 | PASS |
| 3a | hybrid >10k-band r@10 MUST IMPROVE (>) | 0.66 | 0.6733 | PASS |
| 3b | semantic >10k-band r@10 MUST IMPROVE (>) | 0.44 | 0.58 | PASS |
| 4a | hybrid tail-third r@10 MUST IMPROVE (>) | 0.5631 | 0.6408 | PASS |
| 4b | semantic tail-third r@10 MUST IMPROVE (>) | 0.5049 | 0.5825 | PASS |
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
| baseline-hybrid | 1.0 / 1.0 / 3 | 0.5167 / 0.3971 / 120 | 0.4765 / 0.3766 / 277 | 0.5631 / 0.4643 / 103 |
| baseline-semantic | 1.0 / 1.0 / 3 | 0.3917 / 0.2631 / 120 | 0.5596 / 0.3887 / 277 | 0.5049 / 0.3582 / 103 |
| candidate-hybrid | 0.3333 / 0.3333 / 3 | 0.4917 / 0.4273 / 120 | 0.426 / 0.3195 / 277 | 0.6408 / 0.4639 / 103 |
| candidate-semantic | 1.0 / 0.5556 / 3 | 0.475 / 0.3552 / 120 | 0.5307 / 0.3466 / 277 | 0.5825 / 0.3924 / 103 |

## By length band (r@10 / MRR / n)

| run | 6-10k | <6k | >10k |
|---|---|---|---|
| baseline-hybrid | 0.6552 / 0.5781 / 87 | 0.3722 / 0.302 / 266 | 0.66 / 0.481 / 150 |
| baseline-semantic | 0.5287 / 0.4103 / 87 | 0.5451 / 0.3897 / 266 | 0.44 / 0.2652 / 150 |
| candidate-hybrid | 0.6092 / 0.4882 / 87 | 0.3383 / 0.2605 / 266 | 0.6733 / 0.5118 / 150 |
| candidate-semantic | 0.6322 / 0.4348 / 87 | 0.4699 / 0.3299 / 266 | 0.58 / 0.3675 / 150 |

## Holdout (human-verified pairs, rank@10 or miss)

| run | hits@10 | detail |
|---|---|---|
| baseline-hybrid | 3/3 | SNAP-20260606-6930: 1, SNAP-20260427-6316: 1, SNAP-20260427-6313: 1 |
| baseline-semantic | 3/3 | SNAP-20260606-6930: 1, SNAP-20260427-6316: 1, SNAP-20260427-6313: 1 |
| candidate-hybrid | 1/3 | SNAP-20260606-6930: miss, SNAP-20260427-6316: miss, SNAP-20260427-6313: 1 |
| candidate-semantic | 3/3 | SNAP-20260606-6930: 6, SNAP-20260427-6316: 2, SNAP-20260427-6313: 1 |
