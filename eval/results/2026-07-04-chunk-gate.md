# M6f chunk-store gate — 2026-07-04 (runbook step 3)

Candidate: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/Manifest/embeddings/_build/gemini-embedding-2` (slug gemini-embedding-2, schema 2, rows 26225, snapshots 7648, generation 7648).
Gate baselines: `/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/eval/results/2026-07-02-recency-sweep.json` w=0.005 entry (gates 1–3) + fresh same-config active-store runs (gate 4 tail-third — the sweep JSON carries no position strata). 503 labeled rows, k=10, full retrieve() pipeline via the M4 store-override seam; active store at 7648 snapshots during the run.

## Gates

| # | gate | baseline | candidate | verdict |
|---|---|---|---|---|
| 1 | hybrid overall r@10 >= baseline (no regression) | 0.4891 | 0.6123 | PASS |
| 2 | semantic overall r@10 >= baseline (no regression) | 0.497 | 0.5626 | PASS |
| 3a | hybrid >10k-band r@10 MUST IMPROVE (>) | 0.6333 | 0.8067 | PASS |
| 3b | semantic >10k-band r@10 MUST IMPROVE (>) | 0.42 | 0.5467 | PASS |
| 4a | hybrid tail-third r@10 MUST IMPROVE (>) | 0.7087 | 0.6893 | FAIL |
| 4b | semantic tail-third r@10 MUST IMPROVE (>) | 0.6117 | 0.5534 | FAIL |
| 5a | hybrid human holdout pairs still hit (hits@10) | 3/3 | 3/3 | PASS |
| 5b | semantic human holdout pairs still hit (hits@10) | 3/3 | 3/3 | PASS |
| 6 | golden + lean-profile suites vs candidate (test_retrieval_golden.py + test_local_lean_retrieval.py) | all pass | all pass | PASS |

**VERDICT: GATE FAILED — STOP, no cutover**

Notes:
- gate 4a: baseline measured fresh at the gate weight (sweep JSON has no position strata)
- gate 4b: baseline measured fresh at the gate weight (sweep JSON has no position strata)

## By span position (r@10 / MRR / n)

| run | None | head | middle | tail |
|---|---|---|---|---|
| baseline-hybrid | 1.0 / 1.0 / 3 | 0.625 / 0.5118 / 120 | 0.6065 / 0.4164 / 277 | 0.7087 / 0.521 / 103 |
| baseline-semantic | 1.0 / 0.5556 / 3 | 0.5 / 0.3605 / 120 | 0.5451 / 0.3612 / 277 | 0.6117 / 0.4035 / 103 |
| candidate-hybrid | 1.0 / 1.0 / 3 | 0.6083 / 0.503 / 120 | 0.5812 / 0.4203 / 277 | 0.6893 / 0.538 / 103 |
| candidate-semantic | 1.0 / 1.0 / 3 | 0.55 / 0.3887 / 120 | 0.5668 / 0.3707 / 277 | 0.5534 / 0.4051 / 103 |

## By length band (r@10 / MRR / n)

| run | 6-10k | <6k | >10k |
|---|---|---|---|
| baseline-hybrid | 0.7816 / 0.5966 / 87 | 0.4962 / 0.3443 / 266 | 0.7933 / 0.5995 / 150 |
| baseline-semantic | 0.6437 / 0.4406 / 87 | 0.5038 / 0.348 / 266 | 0.58 / 0.371 / 150 |
| candidate-hybrid | 0.7471 / 0.6166 / 87 | 0.4586 / 0.3337 / 266 | 0.8067 / 0.6185 / 150 |
| candidate-semantic | 0.6782 / 0.5212 / 87 | 0.5338 / 0.3555 / 266 | 0.5467 / 0.3607 / 150 |

## Holdout (human-verified pairs, rank@10 or miss)

| run | hits@10 | detail |
|---|---|---|
| baseline-hybrid | 3/3 | SNAP-20260606-6930: 1, SNAP-20260427-6316: 1, SNAP-20260427-6313: 1 |
| baseline-semantic | 3/3 | SNAP-20260606-6930: 6, SNAP-20260427-6316: 2, SNAP-20260427-6313: 1 |
| candidate-hybrid | 3/3 | SNAP-20260606-6930: 1, SNAP-20260427-6316: 1, SNAP-20260427-6313: 1 |
| candidate-semantic | 3/3 | SNAP-20260606-6930: 1, SNAP-20260427-6316: 1, SNAP-20260427-6313: 1 |
