# M6f chunk-store gate — 2026-07-03 (runbook step 3)

Candidate: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Manifest/embeddings/_build2/gemini-embedding-2` (slug gemini-embedding-2, schema 2, rows 32756, snapshots 7601, generation 60).
Gate baselines: `fresh same-config active-store runs (pipeline overrides active; the sweep-JSON baselines were measured at the default config)` w=0.005 entry (gates 1–3) + fresh same-config active-store runs (gate 4 tail-third — the sweep JSON carries no position strata). 503 labeled rows, k=10, full retrieve() pipeline via the M4 store-override seam; active store at 7906 snapshots during the run.
IN-PROCESS pipeline overrides: `{'mmr_lambda': 0.9, 'mmr_protect_top': 3}` (config.ini untouched, byte-asserted; BOTH the candidate and the fresh baseline arms — and the gate-6 pytest run — executed at these values, so every gate compares identical pipeline config).

## Gates

| # | gate | baseline | candidate | verdict |
|---|---|---|---|---|
| 1 | hybrid overall r@10 >= baseline (no regression) | 0.6402 | 0.6362 | FAIL |
| 2 | semantic overall r@10 >= baseline (no regression) | 0.6103 | 0.6481 | PASS |
| 3a | hybrid >10k-band r@10 MUST IMPROVE (>) | 0.78 | 0.8133 | PASS |
| 3b | semantic >10k-band r@10 MUST IMPROVE (>) | 0.4733 | 0.6333 | PASS |
| 4a | hybrid tail-third r@10 MUST IMPROVE (>) | 0.7282 | 0.7282 | FAIL |
| 4b | semantic tail-third r@10 MUST IMPROVE (>) | 0.6505 | 0.6893 | PASS |
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
| baseline-hybrid | 1.0 / 1.0 / 3 | 0.6 / 0.4609 / 120 | 0.6209 / 0.4565 / 277 | 0.7282 / 0.5443 / 103 |
| baseline-semantic | 1.0 / 1.0 / 3 | 0.475 / 0.3307 / 120 | 0.6498 / 0.4555 / 277 | 0.6505 / 0.4519 / 103 |
| candidate-hybrid | 1.0 / 0.5556 / 3 | 0.6167 / 0.5035 / 120 | 0.6065 / 0.4162 / 277 | 0.7282 / 0.5449 / 103 |
| candidate-semantic | 0.6667 / 0.381 / 3 | 0.6 / 0.4193 / 120 | 0.6534 / 0.4247 / 277 | 0.6893 / 0.4481 / 103 |

## By length band (r@10 / MRR / n)

| run | 6-10k | <6k | >10k |
|---|---|---|---|
| baseline-hybrid | 0.7701 / 0.6537 / 87 | 0.5188 / 0.3792 / 266 | 0.78 / 0.5539 / 150 |
| baseline-semantic | 0.6897 / 0.506 / 87 | 0.6617 / 0.4643 / 266 | 0.4733 / 0.3193 / 150 |
| candidate-hybrid | 0.7701 / 0.6003 / 87 | 0.4925 / 0.3421 / 266 | 0.8133 / 0.6019 / 150 |
| candidate-semantic | 0.7471 / 0.4984 / 87 | 0.6241 / 0.416 / 266 | 0.6333 / 0.4084 / 150 |

## Holdout (human-verified pairs, rank@10 or miss)

| run | hits@10 | detail |
|---|---|---|
| baseline-hybrid | 3/3 | SNAP-20260606-6930: 1, SNAP-20260427-6316: 1, SNAP-20260427-6313: 1 |
| baseline-semantic | 3/3 | SNAP-20260606-6930: 1, SNAP-20260427-6316: 1, SNAP-20260427-6313: 1 |
| candidate-hybrid | 3/3 | SNAP-20260606-6930: 3, SNAP-20260427-6316: 3, SNAP-20260427-6313: 1 |
| candidate-semantic | 2/3 | SNAP-20260606-6930: 7, SNAP-20260427-6316: miss, SNAP-20260427-6313: 1 |
