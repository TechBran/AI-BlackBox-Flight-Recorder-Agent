# M6f chunk-store gate — 2026-07-02 (runbook step 3)

Candidate: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Manifest/embeddings/_build/gemini-embedding-2` (slug gemini-embedding-2, schema 2, rows 25746, snapshots 7601, generation 7601).
Gate baselines: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/eval/results/2026-07-02-recency-sweep.json` w=0.005 entry (gates 1–3) + fresh same-config active-store runs (gate 4 tail-third — the sweep JSON carries no position strata). 503 labeled rows, k=10, full retrieve() pipeline via the M4 store-override seam; active store at 7906 snapshots during the run.

## Gates

| # | gate | baseline | candidate | verdict |
|---|---|---|---|---|
| 1 | hybrid overall r@10 >= baseline (no regression) | 0.4891 | 0.4751 | FAIL |
| 2 | semantic overall r@10 >= baseline (no regression) | 0.497 | 0.5169 | PASS |
| 3a | hybrid >10k-band r@10 MUST IMPROVE (>) | 0.6333 | 0.6867 | PASS |
| 3b | semantic >10k-band r@10 MUST IMPROVE (>) | 0.42 | 0.5467 | PASS |
| 4a | hybrid tail-third r@10 MUST IMPROVE (>) | 0.5437 | 0.6117 | PASS |
| 4b | semantic tail-third r@10 MUST IMPROVE (>) | 0.4951 | 0.5825 | PASS |
| 5a | hybrid human holdout pairs still hit (hits@10) | 3/3 | 1/3 | FAIL |
| 5b | semantic human holdout pairs still hit (hits@10) | 3/3 | 2/3 | FAIL |
| 6 | golden + lean-profile suites vs candidate (test_retrieval_golden.py + test_local_lean_retrieval.py) | all pass | FAILURES | FAIL |

**VERDICT: GATE FAILED — STOP, no cutover**

Notes:
- gate 4a: baseline measured fresh at the gate weight (sweep JSON has no position strata)
- gate 4b: baseline measured fresh at the gate weight (sweep JSON has no position strata)

## By span position (r@10 / MRR / n)

| run | None | head | middle | tail |
|---|---|---|---|---|
| baseline-hybrid | 1.0 / 1.0 / 3 | 0.5 / 0.3943 / 120 | 0.4621 / 0.3743 / 277 | 0.5437 / 0.4619 / 103 |
| baseline-semantic | 1.0 / 1.0 / 3 | 0.375 / 0.261 / 120 | 0.5379 / 0.3801 / 277 | 0.4951 / 0.3541 / 103 |
| candidate-hybrid | 0.3333 / 0.3333 / 3 | 0.475 / 0.4262 / 120 | 0.426 / 0.3169 / 277 | 0.6117 / 0.4553 / 103 |
| candidate-semantic | 0.6667 / 0.5 / 3 | 0.4833 / 0.3571 / 120 | 0.5054 / 0.3348 / 277 | 0.5825 / 0.3629 / 103 |

## By length band (r@10 / MRR / n)

| run | 6-10k | <6k | >10k |
|---|---|---|---|
| baseline-hybrid | 0.6322 / 0.5753 / 87 | 0.3609 / 0.2999 / 266 | 0.64 / 0.4784 / 150 |
| baseline-semantic | 0.5172 / 0.4078 / 87 | 0.5301 / 0.381 / 266 | 0.4133 / 0.2618 / 150 |
| candidate-hybrid | 0.5402 / 0.4698 / 87 | 0.3346 / 0.258 / 266 | 0.6867 / 0.5154 / 150 |
| candidate-semantic | 0.6322 / 0.4263 / 87 | 0.4624 / 0.3131 / 266 | 0.5467 / 0.3605 / 150 |

## Holdout (human-verified pairs, rank@10 or miss)

| run | hits@10 | detail |
|---|---|---|
| baseline-hybrid | 3/3 | SNAP-20260606-6930: 1, SNAP-20260427-6316: 1, SNAP-20260427-6313: 1 |
| baseline-semantic | 3/3 | SNAP-20260606-6930: 1, SNAP-20260427-6316: 1, SNAP-20260427-6313: 1 |
| candidate-hybrid | 1/3 | SNAP-20260606-6930: miss, SNAP-20260427-6316: miss, SNAP-20260427-6313: 1 |
| candidate-semantic | 2/3 | SNAP-20260606-6930: miss, SNAP-20260427-6316: 2, SNAP-20260427-6313: 1 |
