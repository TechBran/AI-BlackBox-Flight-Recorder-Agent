# benchmarks

`digest_ab/` is the digest-vs-perspective retrieval A/B harness: LLM-generated queries are scored with hit@1/3/5 + MRR over a synthetic corpus to compare snapshot text strategies.

Run with: `Orchestrator/venv/bin/python benchmarks/digest_ab/run_ab.py`

`artifacts.json` caches the generated queries for deterministic re-runs; `results.json` holds the latest scored run.
