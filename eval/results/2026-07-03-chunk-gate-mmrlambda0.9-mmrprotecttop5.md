# M6f chunk-store gate — 2026-07-03 (runbook step 3)

Candidate: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Manifest/embeddings/_build2/gemini-embedding-2` (slug gemini-embedding-2, schema 2, rows 32756, snapshots 7601, generation 60).
Gate baselines: `fresh same-config active-store runs (pipeline overrides active; the sweep-JSON baselines were measured at the default config)` w=0.005 entry (gates 1–3) + fresh same-config active-store runs (gate 4 tail-third — the sweep JSON carries no position strata). 503 labeled rows, k=10, full retrieve() pipeline via the M4 store-override seam; active store at 7906 snapshots during the run.
IN-PROCESS pipeline overrides: `{'mmr_lambda': 0.9, 'mmr_protect_top': 5}` (config.ini untouched, byte-asserted; BOTH the candidate and the fresh baseline arms — and the gate-6 pytest run — executed at these values, so every gate compares identical pipeline config).

## Gates

| # | gate | baseline | candidate | verdict |
|---|---|---|---|---|
| 1 | hybrid overall r@10 >= baseline (no regression) | 0.67 | 0.6839 | PASS |
| 2 | semantic overall r@10 >= baseline (no regression) | 0.66 | 0.7058 | PASS |
| 3a | hybrid >10k-band r@10 MUST IMPROVE (>) | 0.78 | 0.8533 | PASS |
| 3b | semantic >10k-band r@10 MUST IMPROVE (>) | 0.5267 | 0.7067 | PASS |
| 4a | hybrid tail-third r@10 MUST IMPROVE (>) | 0.7476 | 0.7573 | PASS |
| 4b | semantic tail-third r@10 MUST IMPROVE (>) | 0.7087 | 0.7573 | PASS |
| 5a | hybrid human holdout pairs still hit (hits@10) | 3/3 | 3/3 | PASS |
| 5b | semantic human holdout pairs still hit (hits@10) | 3/3 | 2/3 | FAIL |
| 6 | golden + lean-profile suites vs candidate (test_retrieval_golden.py + test_local_lean_retrieval.py) | all pass | FAILURES | FAIL |

**VERDICT: GATE FAILED — STOP, no cutover**

Notes:
- gate 4a: baseline measured fresh at the gate weight (sweep JSON has no position strata)
- gate 4b: baseline measured fresh at the gate weight (sweep JSON has no position strata)

## By span position (r@10 / MRR / n)

| run | None | head | middle | tail |
|---|---|---|---|---|
| baseline-hybrid | 1.0 / 1.0 / 3 | 0.6167 / 0.4672 / 120 | 0.6606 / 0.4679 / 277 | 0.7476 / 0.5503 / 103 |
| baseline-semantic | 1.0 / 1.0 / 3 | 0.525 / 0.342 / 120 | 0.6968 / 0.4704 / 277 | 0.7087 / 0.4661 / 103 |
| candidate-hybrid | 1.0 / 0.5556 / 3 | 0.65 / 0.5111 / 120 | 0.6679 / 0.4334 / 277 | 0.7573 / 0.5539 / 103 |
| candidate-semantic | 0.6667 / 0.4167 / 3 | 0.6417 / 0.429 / 120 | 0.7148 / 0.442 / 277 | 0.7573 / 0.4639 / 103 |

## By length band (r@10 / MRR / n)

| run | 6-10k | <6k | >10k |
|---|---|---|---|
| baseline-hybrid | 0.7816 / 0.6571 / 87 | 0.5714 / 0.3927 / 266 | 0.78 / 0.5581 / 150 |
| baseline-semantic | 0.7241 / 0.5164 / 87 | 0.7143 / 0.4802 / 266 | 0.5267 / 0.3312 / 150 |
| candidate-hybrid | 0.7931 / 0.6092 / 87 | 0.5526 / 0.3583 / 266 | 0.8533 / 0.612 / 150 |
| candidate-semantic | 0.8046 / 0.5137 / 87 | 0.6729 / 0.4305 / 266 | 0.7067 / 0.4249 / 150 |

## Holdout (human-verified pairs, rank@10 or miss)

| run | hits@10 | detail |
|---|---|---|
| baseline-hybrid | 3/3 | SNAP-20260606-6930: 1, SNAP-20260427-6316: 1, SNAP-20260427-6313: 1 |
| baseline-semantic | 3/3 | SNAP-20260606-6930: 1, SNAP-20260427-6316: 1, SNAP-20260427-6313: 1 |
| candidate-hybrid | 3/3 | SNAP-20260606-6930: 3, SNAP-20260427-6316: 3, SNAP-20260427-6313: 1 |
| candidate-semantic | 2/3 | SNAP-20260606-6930: miss, SNAP-20260427-6316: 4, SNAP-20260427-6313: 1 |
