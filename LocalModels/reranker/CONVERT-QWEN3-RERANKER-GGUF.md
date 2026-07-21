# Qwen3-Reranker-8B → GGUF self-conversion runbook (Milestone 4, D13)

> **D13:** the retrieval group is sequential (`swap: true`), so the reranker gets
> the whole card alone and we run the top-of-family **Qwen3-Reranker-8B @ Q8_0**
> (the 0.6B stays the CPU-tier fallback only). Same convert/quantize procedure as
> any Qwen3-Reranker — only the source repo and the Q8_0 quantize step differ.

Community reranker GGUFs are frequently broken: a conversion that drops
`cls.output.weight` yields degenerate `~1e-28` relevance scores (the ranker is
effectively random). We convert ourselves from a **pinned llama.cpp build that
post-dates the Qwen3-Reranker `convert_hf_to_gguf.py` fix** (llama.cpp #16407,
resolved: detects Qwen3-Reranker, extracts `cls.output.weight`, sets
`pooling_type=RANK` + the yes/no classifier labels). The output is validated by
the G2 gate (`eval/rerank_g2.py`) before the wizard is allowed to select the
`qwen3-reranker-8b-local` model.

Run this on the GPU box (MS02). `${LOCALSTACK_MODELS}` is the llama-swap
models-dir the installer substitutes into
`installer/templates/llama-swap-config.yaml.template` (the reranker member reads
`${models-dir}/Qwen3-Reranker-8B-Q8_0.gguf`), e.g. `~/.blackbox/localstack/models`.

## 1. Clone + pin llama.cpp (post-#16407)

```bash
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
# Pin to a tagged release that INCLUDES PR #16407. Resolve the exact tag once,
# then hard-pin it here (record it in the commit message). As of 2026-07 use the
# latest b-tag and VERIFY the fix is present before trusting the build:
git checkout <PINNED_LLAMACPP_TAG>   # e.g. b6600+ — must post-date #16407
```

Verify the convert script actually handles Qwen3-Reranker (abort if these do not
match — the pin is too old and WILL produce ~1e-28 GGUFs):

```bash
grep -nE "Qwen3.*[Rr]erank|cls\.output\.weight|pooling_type.*RANK|classifier" \
  convert_hf_to_gguf.py
```
Expected: at least one hit referencing the Qwen3-Reranker head / `cls.output.weight`.

## 2. Conversion venv

```bash
python3 -m venv .conv-venv
. .conv-venv/bin/activate
pip install -r requirements/requirements-convert_hf_to_gguf.txt
pip install -U "huggingface_hub[cli]"
```

## 3. Download the HF weights (CausalLM checkpoint)

```bash
huggingface-cli download Qwen/Qwen3-Reranker-8B \
  --local-dir ./Qwen3-Reranker-8B
```

## 4. Convert to f16 GGUF, then QUANTIZE to Q8_0 (output name is load-bearing)

First convert to an f16 intermediate:

```bash
python3 convert_hf_to_gguf.py ./Qwen3-Reranker-8B \
  --outfile "${LOCALSTACK_MODELS}/Qwen3-Reranker-8B-f16.gguf" \
  --outtype f16
```

Then quantize to **Q8_0** — this is MANDATORY for the 8B (D13): the f16 8B GGUF
is ~16GB and will not fit the sequential retrieval group's ~10–11GB budget,
whereas Q8_0 is ~8.1GB (same "highest quality that fits in one shot" rule as the
embedder). The final filename MUST be exactly `Qwen3-Reranker-8B-Q8_0.gguf` — the
llama-swap member `rerank-qwen3-8b` loads it by that path.

```bash
cmake --build build --target llama-quantize   # if not already built
./build/bin/llama-quantize \
  "${LOCALSTACK_MODELS}/Qwen3-Reranker-8B-f16.gguf" \
  "${LOCALSTACK_MODELS}/Qwen3-Reranker-8B-Q8_0.gguf" Q8_0
rm -f "${LOCALSTACK_MODELS}/Qwen3-Reranker-8B-f16.gguf"   # drop the ~16GB intermediate
```

## 5. Quick metadata pre-check (NOT the authoritative gate)

```bash
./build/bin/llama-gguf "${LOCALSTACK_MODELS}/Qwen3-Reranker-8B-Q8_0.gguf" \
  | grep -iE "pooling|cls\.output|classifier|rank"
```
Expected: a `pooling_type` = RANK / `cls.output.weight` tensor is present. A
missing `cls.output.weight` here means a broken conversion — go back to step 1
and use a newer pin.

## 6. Authoritative validity gate — G2

Serve the member (llama-swap on `:9098`, or a standalone `llama-server
--model … --reranking --pooling rank -c 8192`) and run the G2 harness:

```bash
Orchestrator/venv/bin/python eval/rerank_g2.py            # served-vs-golden gate
Orchestrator/venv/bin/python eval/rerank_g2.py --hf-reference \
  --hf-model-dir ./llama.cpp/Qwen3-Reranker-8B          # + HF cross-check
```
The GGUF is only trustworthy once G2 exits 0 (rank-order agreement + no
degenerate `~1e-28` scores). Only then does the wizard flip the sidecar to
`qwen3-reranker-8b-local`.
