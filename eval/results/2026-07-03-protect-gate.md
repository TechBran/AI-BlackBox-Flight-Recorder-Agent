# Whole+chunk re-gate with MMR top-rank protect — 2026-07-03 (M6f iteration 3)

Candidate: `Manifest/embeddings/_build2/gemini-embedding-2` (schema 2, rows 32756,
snapshots 7601, generation 60 — the iteration-2 whole+chunk store, unchanged).
Code under gate: the iteration-3 MMR top-rank protect (`mmr_select(protect=P)`,
`[retrieval] mmr_protect_top`, code fallback 3, commit 82cfd2d) — the fused
top-P is seeded into the MMR picked set in rank order and cannot be dropped as
a near-duplicate. This changes production ranking for BOTH store schemas by
design; every gate below compares candidate-vs-baseline running the SAME code
at the SAME in-process config (config.ini byte-asserted untouched on every run).

Gate runs: `eval/run_bench.py --gate --candidate-dir Manifest/embeddings/_build2
--candidate-slug gemini-embedding-2 --out-date 2026-07-03 --mmr-lambda <λ>
--mmr-protect <P>`. Full artifacts:
`2026-07-03-chunk-gate-mmrlambda{0.9-mmrprotecttop3,0.9-mmrprotecttop5,0.85-mmrprotecttop3}.{md,json}`.

## Six-gate tables

### (λ=0.9, P=3) — the predicted winner

| # | gate | baseline | candidate | verdict |
|---|---|---|---|---|
| 1 | hybrid overall r@10 >= baseline | 0.6402 | 0.6362 | FAIL |
| 2 | semantic overall r@10 >= baseline | 0.6103 | 0.6481 | PASS |
| 3a | hybrid >10k r@10 must improve | 0.78 | 0.8133 | PASS |
| 3b | semantic >10k r@10 must improve | 0.4733 | 0.6333 | PASS |
| 4a | hybrid tail-third must improve | 0.7282 | 0.7282 | FAIL (tied) |
| 4b | semantic tail-third must improve | 0.6505 | 0.6893 | PASS |
| 5a | hybrid holdout hits@10 | 3/3 | 3/3 | PASS |
| 5b | semantic holdout hits@10 | 3/3 | 2/3 (6316 miss) | FAIL |
| 6 | golden+lean suites vs candidate | all pass | all pass (10/10) | PASS |

### (λ=0.9, P=5)

| # | gate | baseline | candidate | verdict |
|---|---|---|---|---|
| 1 | hybrid overall | 0.67 | 0.6839 | PASS |
| 2 | semantic overall | 0.66 | 0.7058 | PASS |
| 3a | hybrid >10k | 0.78 | 0.8533 | PASS |
| 3b | semantic >10k | 0.5267 | 0.7067 | PASS |
| 4a | hybrid tail | 0.7476 | 0.7573 | PASS |
| 4b | semantic tail | 0.7087 | 0.7573 | PASS |
| 5a | hybrid holdout | 3/3 | 3/3 | PASS |
| 5b | semantic holdout | 3/3 | 2/3 (6930 miss) | FAIL |
| 6 | golden+lean | all pass | 1 FAILURE (see below) | FAIL |

Gate-6 detail at P=5: the ONE failure is Half-A
`control_phone delegate device task tailscale` — its candidate top-5 contains
SNAP-20260702-7929 and SNAP-20260702-7920 (July 2, genuinely fresh) but
`test_retrieval_golden.py:83` hardcodes the `SNAP-202606` prefix, so July
snapshots don't count. A calendar artifact of the golden test, not a freshness
regression (the bench freshness convention — current OR previous month — passes
this list); recorded as FAIL because the gate is the gate.

### (λ=0.85, P=3) — closest: fails ONLY 5b

| # | gate | baseline | candidate | verdict |
|---|---|---|---|---|
| 1 | hybrid overall | 0.6302 | 0.6302 | PASS (>=, tied) |
| 2 | semantic overall | 0.6064 | 0.6362 | PASS |
| 3a | hybrid >10k | 0.7533 | 0.8133 | PASS |
| 3b | semantic >10k | 0.4733 | 0.6267 | PASS |
| 4a | hybrid tail | 0.7087 | 0.7184 | PASS |
| 4b | semantic tail | 0.6505 | 0.6699 | PASS |
| 5a | hybrid holdout | 3/3 | 3/3 | PASS |
| 5b | semantic holdout | 3/3 | 2/3 (6316 miss) | FAIL |
| 6 | golden+lean | all pass | all pass (10/10) | PASS |

**VERDICT: NO config passes all six gates — no cutover.**

## What the protect fixed — and the residual, at rank level

FIXED (the iteration-2 target): gate 5a is 3/3 at every protected config (was
1/3) — both hybrid golds sat at post-RRF rank 3, inside the protect. Gate 6 is
green at P=3 (was 2 golden failures). And the protect is a large ABSOLUTE lift
on both stores independent of the swap: at λ=0.9, baseline-hybrid r@10
0.5348 → 0.6402 and candidate-hybrid 0.5229 → 0.6362 vs the P=0 iteration-2
runs (semantic 0.5408 → 0.6103 / 0.5507 → 0.6481).

STILL FAILING — 5b, the candidate-SEMANTIC arm, and it is structural.
Decomposed probe (production pipeline steps, candidate store, semantic-only):

| gold | pre-MMR fused rank | λ=0.9 P=0 | P=1 | P=2 | P=3 | P=4 | P=5 |
|---|---|---|---|---|---|---|---|
| SNAP-20260427-6316 (ZUPT) | 4 | 2 | 2 | miss | miss | 4 | 4 |
| SNAP-20260606-6930 (AudioRecord) | 11 | 6 | 6 | 7 | 7 | miss | miss |
| SNAP-20260427-6313 (e-stop) | 1 | 1 | 1 | 1 | 1 | 1 | 1 |

(λ=0.85 identical; λ=0.95 kills 6930 at every P.)

Mechanism, plainly:

* **6316**: its killer sibling SNAP-20260426-6293 (cos 0.859) sits at fused
  rank 3, the gold at rank 4. UNPROTECTED greedy MMR picks the GOLD second —
  it has the lowest similarity to the first pick among the leaders — and the
  penalty falls on the sibling instead. Seeding the top-3 reverses the order
  of elimination: the sibling is inside the picked set before the gold is
  considered, the gold eats the 0.859 penalty (0.086 at λ=0.9 vs the ~0.006
  full RRF span), and drops out of top-10. The protect does not only ADD
  strong items — when the gold sits just OUTSIDE the boundary with its
  near-dup just INSIDE, it actively re-aims MMR's veto at the gold.
* **6930**: fused rank 11, a diffuse near-dup of the ENTIRE fused top-6
  (cos 0.755–0.805 to all of them — whole-doc vectors of related sessions).
  It only reaches top-10 because MMR drops leaders; every protected slot
  removes one rescue slot, so it dies at P>=4.
* Constraint set on this candidate: semantic 5b needs P<=3 (6930) AND P<=1 or
  P>=4 (6316) => P<=1; hybrid 5a needs P>=3 (both golds at hybrid fused
  rank 3). **Empty intersection: no (λ, P) exists for the top-P protect on
  the _build2 candidate.** The baseline (v1 whole-doc) store has no such
  conflict — all three golds are semantic rank 1 there — so this is a
  candidate-store rank-structure problem (whole-vec sibling clusters), not a
  protect-design problem per se.

## Half-A freshness sanity (work item 4)

At (λ=0.9, P=3), the 4 recurring-topic golden queries against BOTH stores
(active production path + candidate via the store seam), pass = current- or
previous-month snapshot in top-5: **8/8 PASS** (4/4 active, 4/4 candidate);
config.ini byte-identical before/after. The protect does not bury freshness.
