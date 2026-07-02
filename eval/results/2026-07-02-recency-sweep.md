# Recency-weight sweep — 2026-07-02 (M4b, measurement only)

503 labeled rows, both gemini-embedding-2 arms, full retrieve() pipeline, k=10. recency_weight set on the in-process CFG only — config.ini untouched (byte-asserted). Freshness guard: the 4 human recurring-topic queries (test_retrieval_golden Half-A) must keep a SNAP-202607/SNAP-202606 snapshot in the production top-5.

## gemini2-hybrid

| weight | r@1 | r@3 | r@5 | r@10 | MRR | >10k r@10 |
|---|---|---|---|---|---|---|
| 0.05 | 0.1431 | 0.1869 | 0.2087 | 0.2684 | 0.174 | 0.3867 |
| 0.03 | 0.2207 | 0.2584 | 0.2883 | 0.3539 | 0.2508 | 0.4733 |
| 0.02 | 0.3161 | 0.33 | 0.3459 | 0.4115 | 0.3334 | 0.5267 |
| 0.01 | 0.3579 | 0.3678 | 0.3877 | 0.4513 | 0.3746 | 0.5933 |
| 0.005 | 0.3817 | 0.3936 | 0.4155 | 0.4891 | 0.4009 | 0.6333 |
| 0.0 | 0.3917 | 0.4095 | 0.4394 | 0.5109 | 0.4146 | 0.64 |

## gemini2-semantic

| weight | r@1 | r@3 | r@5 | r@10 | MRR | >10k r@10 |
|---|---|---|---|---|---|---|
| 0.05 | 0.1074 | 0.1471 | 0.1988 | 0.3439 | 0.1531 | 0.3333 |
| 0.03 | 0.1332 | 0.1909 | 0.2406 | 0.3857 | 0.186 | 0.3867 |
| 0.02 | 0.169 | 0.2107 | 0.2724 | 0.4056 | 0.2165 | 0.3933 |
| 0.01 | 0.2366 | 0.2704 | 0.326 | 0.4414 | 0.279 | 0.3867 |
| 0.005 | 0.3201 | 0.3499 | 0.3936 | 0.497 | 0.3556 | 0.42 |
| 0.0 | 0.4453 | 0.4771 | 0.499 | 0.5567 | 0.4722 | 0.42 |

## Freshness guard (recent snapshot in top-5, production path)

| weight | Q1 | Q2 | Q3 | Q4 | pass |
|---|---|---|---|---|---|
| 0.05 | PASS | PASS | PASS | PASS | 4/4 |
| 0.03 | PASS | PASS | PASS | PASS | 4/4 |
| 0.02 | PASS | PASS | PASS | PASS | 4/4 |
| 0.01 | PASS | PASS | PASS | PASS | 4/4 |
| 0.005 | PASS | PASS | PASS | PASS | 4/4 |
| 0.0 | PASS | PASS | PASS | PASS | 4/4 |

Queries: Q1="pluggable embeddings model migration reembed"; Q2="control_phone delegate device task tailscale"; Q3="on-device gemma phone agent native tool loop"; Q4="streaming speech to text multi provider"

## Reading (data summary — no recommendation; config decision is Brandon's)

- Labeled recall improves monotonically as the weight drops, on BOTH arms and
  at every k. Max labeled recall is at w=0.0: hybrid r@10 0.2684 -> 0.5109
  (+0.24 absolute, +90% relative; >10k band 0.3867 -> 0.64), semantic r@10
  0.3439 -> 0.5567 (MRR 0.1531 -> 0.4722).
- ALL freshness checks pass at EVERY weight, including w=0.0 — on these 4
  recurring-topic queries the recent snapshots surface on relevance alone, so
  this guard does not discriminate among the swept values. (It bounds the harm
  case; it does not certify that other "latest state of X" queries keep their
  freshness behavior at low weights.)
- Structural observation: the current 0.05 weight is ~7.6x the entire
  40-candidate RRF relevance span (1/60 - 1/99 ~ 0.00657), so post-fusion it
  acts as a primary sort key, not a tie-break. A weight in the ~0.005 range is
  proportionate to rank-space (0.005 ~ 0.76x the span — recency then flips
  only genuine near-ties). w=0.005 keeps most of the labeled-recall gain
  (hybrid 0.4891, semantic 0.497) with a nonzero freshness prior.
- Internal consistency: the w=0.05 sweep row reproduces the committed baseline
  to 4 decimals on both arms.
