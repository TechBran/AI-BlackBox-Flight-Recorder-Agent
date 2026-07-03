# M6f chunk-store gate — 2026-07-03 (runbook step 3)

Candidate: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Manifest/embeddings/_build2/gemini-embedding-2` (slug gemini-embedding-2, schema 2, rows 32756, snapshots 7601, generation 60).
Gate baselines: `fresh same-config active-store runs (pipeline overrides active; the sweep-JSON baselines were measured at the default config)` w=0.005 entry (gates 1–3) + fresh same-config active-store runs (gate 4 tail-third — the sweep JSON carries no position strata). 503 labeled rows, k=10, full retrieve() pipeline via the M4 store-override seam; active store at 7906 snapshots during the run.
IN-PROCESS pipeline overrides: `{'mmr_lambda': 0.85, 'mmr_protect_top': 3}` (config.ini untouched, byte-asserted; BOTH the candidate and the fresh baseline arms — and the gate-6 pytest run — executed at these values, so every gate compares identical pipeline config).

## Gates

| # | gate | baseline | candidate | verdict |
|---|---|---|---|---|
| 1 | hybrid overall r@10 >= baseline (no regression) | 0.6302 | 0.6302 | PASS |
| 2 | semantic overall r@10 >= baseline (no regression) | 0.6064 | 0.6362 | PASS |
| 3a | hybrid >10k-band r@10 MUST IMPROVE (>) | 0.7533 | 0.8133 | PASS |
| 3b | semantic >10k-band r@10 MUST IMPROVE (>) | 0.4733 | 0.6267 | PASS |
| 4a | hybrid tail-third r@10 MUST IMPROVE (>) | 0.7087 | 0.7184 | PASS |
| 4b | semantic tail-third r@10 MUST IMPROVE (>) | 0.6505 | 0.6699 | PASS |
| 5a | hybrid human holdout pairs still hit (hits@10) | 3/3 | 3/3 | PASS |
| 5b | semantic human holdout pairs still hit (hits@10) | 3/3 | 2/3 | FAIL |
| 6 | golden + lean-profile suites vs candidate (test_retrieval_golden.py + test_local_lean_retrieval.py) | all pass | all pass | PASS |

**VERDICT: GATE FAILED — STOP, no cutover**

Notes:
- gate 4a: baseline measured fresh at the gate weight (sweep JSON has no position strata)
- gate 4b: baseline measured fresh at the gate weight (sweep JSON has no position strata)

## By span position (r@10 / MRR / n)

| run | None | head | middle | tail |
|---|---|---|---|---|
| baseline-hybrid | 1.0 / 1.0 / 3 | 0.5833 / 0.4594 / 120 | 0.6173 / 0.4559 / 277 | 0.7087 / 0.5423 / 103 |
| baseline-semantic | 1.0 / 1.0 / 3 | 0.4667 / 0.33 / 120 | 0.6462 / 0.4538 / 277 | 0.6505 / 0.4518 / 103 |
| candidate-hybrid | 1.0 / 0.5556 / 3 | 0.6167 / 0.5035 / 120 | 0.5993 / 0.4148 / 277 | 0.7184 / 0.5443 / 103 |
| candidate-semantic | 0.6667 / 0.381 / 3 | 0.5917 / 0.4176 / 120 | 0.6426 / 0.4232 / 277 | 0.6699 / 0.4452 / 103 |

## By length band (r@10 / MRR / n)

| run | 6-10k | <6k | >10k |
|---|---|---|---|
| baseline-hybrid | 0.7701 / 0.6537 / 87 | 0.515 / 0.3786 / 266 | 0.7533 / 0.5512 / 150 |
| baseline-semantic | 0.6897 / 0.5054 / 87 | 0.6541 / 0.4622 / 266 | 0.4733 / 0.3195 / 150 |
| candidate-hybrid | 0.7586 / 0.599 / 87 | 0.485 / 0.341 / 266 | 0.8133 / 0.6015 / 150 |
| candidate-semantic | 0.7126 / 0.4937 / 87 | 0.6165 / 0.415 / 266 | 0.6267 / 0.4066 / 150 |

## Holdout (human-verified pairs, rank@10 or miss)

| run | hits@10 | detail |
|---|---|---|
| baseline-hybrid | 3/3 | SNAP-20260606-6930: 1, SNAP-20260427-6316: 1, SNAP-20260427-6313: 1 |
| baseline-semantic | 3/3 | SNAP-20260606-6930: 1, SNAP-20260427-6316: 1, SNAP-20260427-6313: 1 |
| candidate-hybrid | 3/3 | SNAP-20260606-6930: 3, SNAP-20260427-6316: 3, SNAP-20260427-6313: 1 |
| candidate-semantic | 2/3 | SNAP-20260606-6930: 7, SNAP-20260427-6316: miss, SNAP-20260427-6313: 1 |
