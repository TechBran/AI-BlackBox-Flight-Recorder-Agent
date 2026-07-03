# MS-02 Ultra — GPU embedding speed test (qwen3-embedding-8b, chunked)

**Box:** bbx-MS-02-Ultra @ 192.168.1.153 (customer-tier unit). RTX 2000 Ada Generation,
16 GB VRAM, 128 GB RAM. Ledger = copy of the dev box's SNAPSHOT_VOLUME (7,611 snapshots).
Code = current `main` (unzipped): M2/M5/M6 present (tokenization, chunker, vendored tokenizers,
per-model max_input_tokens, schema-2 store).

## Measured (2026-07-03, mid-run, in-service schema-2 rebuild)
- Job: `kind=rebuild, activate=false, target=qwen3-embedding-8b` → building the schema-2
  chunked candidate under `_build/` (active store untouched — correct M6d behavior).
- **Throughput: 24.0 snapshots/min (0.40 snap/s)**, measured over 75 s off the job counter
  (done 3513 → 3543). Steady across samples.
- Full chunked rebuild of 7,611 snapshots ≈ **5.3 h** wall clock (≈2.8 h remaining from 46%).
- GPU state during embed: **SM 91–100 %, memory-controller 17–30 %** → compute-bound, not
  bandwidth-bound. llama-server (embed) 7.0 GB + vLLM reranker 3.3 GB co-resident = 10.3/16 GB.
  vLLM idle (no rerank traffic) → NOT the throttle; this is the card's real ceiling for 8B.

## Comparison to dev-box baseline
| Env | Model | Throughput | Full-ledger chunked rebuild |
|---|---|---|---|
| Dev box (AMD 7600X, CPU) | qwen3-8b | 46 tok/s (~0.045 chunk/s) | ~99 h (projected) |
| MS-02 Ultra (RTX 2000 Ada) | qwen3-8b | ~1.7 chunk/s (24 snap/min) | ~5.3 h (measured) |
| — speedup | | **~20–40× (chunk basis)** | ~18× wall-clock |
| Cloud (gemini-embedding-2) | — | — | ~$2–3, <1 h |

Token/s on the Ultra not directly logged (ollama journal quiet); chunk-rate is the measured
figure, token estimate ~800–1,700 tok/s depending on chunk fill.

## Notes / levers
- **8B is the max-quality local model and the heaviest.** qwen3-0.6b (the phone/lean store's
  model) would embed multiples faster on the same card — if the goal is a fast local rebuild,
  0.6b is the lever; 8B is the quality ceiling.
- vLLM reranker is deployed as `vllm-reranker.service` (Qwen3-Reranker 0.6B) and co-resides in
  VRAM as the audit predicted (10.3/16 GB with the 8B embedder). Stopping it during a bulk
  build frees 3.3 GB but does NOT speed embedding (it's idle/compute-free while quiet).
- The rebuild is `activate=false` — it will NOT cut over automatically; the candidate must be
  gated + swapped per the M6f runbook once complete.
