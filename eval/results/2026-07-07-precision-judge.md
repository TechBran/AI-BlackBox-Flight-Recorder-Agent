# Precision judge × channel attribution — 2026-07-07

Blind LLM-judge (15 agents, Opus) rated each of the 150 delivered results relevant / borderline / irrelevant **without knowing the retrieval channel**; joined afterward to the channel/cosine/rerank attribution from `noise_probe`.

## Precision by channel

| channel | n | relevant | borderline | irrelevant |
|---|---|---|---|---|
| **both** (semantic ∧ keyword agree) | 40 | **85%** | 12% | 2% |
| semantic-only | 10 | 10% | 30% | 60% |
| **keyword-only** | 100 | 10% | 30% | **60%** |
| **ALL** | 150 | 30% | 25% | **45%** |

## Headline
- **Precision@10 (relevant only): 30%.** Nearly half — **45% — of everything delivered is irrelevant noise.**
- **90% of all noise is keyword-only** (60 of 67 irrelevant results).
- Keyword-only split: **60% true noise, 30% borderline, 10% genuine recovery.** So demoting/gating keyword removes almost all noise while risking only ~10% genuine recovery (40% if borderline counts).
- **Cross-channel agreement is the strongest precision signal:** items in BOTH lanes are 85% relevant. Keyword's job should be to *agree/boost*, not to *inject*.

## The reranker score DOES discriminate (median raw Vertex score)

| channel × label | n | median | p25 | p75 |
|---|---|---|---|---|
| keyword irrelevant (noise) | 60 | **0.013** | 0.010 | 0.019 |
| keyword borderline | 30 | 0.030 | 0.025 | 0.047 |
| keyword relevant (recovery) | 10 | 0.040 | 0.016 | 0.106 |
| both relevant | 34 | **0.104** | 0.051 | 0.205 |
| semantic irrelevant | 6 | 0.010 | 0.010 | 0.011 |

**A rerank absolute floor ~0.02–0.025 drops ~75–80% of the keyword noise (and the semantic near-miss noise) while keeping the real hits** (both-relevant median 0.104, keyword-recovery median 0.040). Not perfectly clean (keyword-relevant p25=0.016 overlaps noise), but the bulk separates — and the floor is calibratable from these distributions.

## Decision → fix architecture (to confirm in the sandbox)
1. **C — preserve the reranker's absolute score as the relevance signal + apply a calibrated floor (~0.02–0.03).** The linchpin: it's the single most effective *and* self-calibratable lever, and it's a per-reranker property → portable ("pick your reranker, it self-calibrates").
2. **A — keyword becomes agreement/dedup-only (gated), never injects.** Keeps the 85%-precision "both" items; kills the 60%-noise keyword-only injection; prevents eviction.
3. **B — variable-k: return fewer than k when few clear the floor** (45% noise means padding is the enemy).
4. **D — semantic cosine floor ~0.60 + reranker-absent fallback** (for boxes with no reranker, e.g. on-device: A+B+D carry the load).
5. Passage-construction fix (best-chunk text vs 4096-char head-cut) to sharpen the reranker's own discrimination.
