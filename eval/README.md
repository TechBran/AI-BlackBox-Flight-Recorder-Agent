# Retrieval eval harness (WI-6 Phase A)

Offline eval for the canonical retriever (`Orchestrator/retrieval.py:retrieve`).
The baseline numbers produced here **gate the M6 chunk-store swap** (plan:
`docs/plans/2026-07-01-retrieval-upgrade-implementation.md` M4; protocol:
audit A10 in `docs/plans/2026-07-01-retrieval-upgrade-spec-audit.md`).

## Files

| file | what |
|---|---|
| `build_labeled_set.py` | Stratified labeled-set builder (encodes the FULL A10 protocol). |
| `labeled_set.jsonl` | Committed artifact: ~500 generated rows + 3 human holdout rows. |
| `run_bench.py` | Bench runner — every row through the full `retrieve()` pipeline per arm. |
| `bench_cache.json` | Per-(arm, query, operator) result cache; delete to force a re-run. |
| `results/` | Dated baseline reports (`.md` tables + `.json` raw). |

## Row schema (`labeled_set.jsonl`)

```json
{"query": "...", "gold_snap_id": "SNAP-...", "span_start": 123, "span_end": 1456,
 "position_third": "head|middle|tail", "length_band": "<6k|6-10k|>10k",
 "operator": "...", "age_quartile": "Q1..Q4", "validate": false,
 "source": "generated|holdout"}
```

`validate: true` marks every 10th generated row for hand validation. Holdout
rows are the human-verified pairs from
`Orchestrator/tests/test_retrieval_golden.py` (span fields null).

## Regenerating the labeled set

```bash
Orchestrator/venv/bin/python eval/build_labeled_set.py --dry-run   # strata report, no cost
Orchestrator/venv/bin/python eval/build_labeled_set.py             # generates missing queries
```

- Deterministic: sampling seed 20260702; spans use a per-snapshot RNG, so a
  snapshot's span never shifts as the corpus grows.
- The index is LIVE — new mints change the sample on re-runs; already-generated
  queries are cached in the jsonl (keyed by gold_snap_id + span) and reused, so
  a re-run only pays for genuinely new rows.
- **Cost**: query generation is ~500 `claude-haiku-4-5` calls over 1–2k-char
  spans ≈ **$0.45** ($1/MTok in, $5/MTok out). The script aborts if the
  projection exceeds $10. Calls go direct through the Anthropic SDK
  (NON-minting — never `/chat`).
- Generated queries are secret-scanned (sk-/AIza/xoxb/ghp_/AKIA/PEM/JWT
  patterns) before writing; hits are dropped and reported.

## Running the bench

```bash
Orchestrator/venv/bin/python eval/run_bench.py
```

Arms are defined in `run_bench.py` (`ARMS`). Non-active arms embed queries with
their OWN model via `Orchestrator/embeddings/providers.get_provider(slug)` and
pass the vector through `retrieve(..., store=, query_vector=)` — the M4 eval
seam. Read-only: **never** heal store gaps via `POST /embeddings/migrate` (it
auto-cuts-over the active model); uncovered gold is reported as such instead.
