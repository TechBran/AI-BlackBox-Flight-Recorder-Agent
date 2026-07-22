"""Embedding-model registry — the SINGLE source of truth for embedding models.

This file is the ONLY place embedding-model literals (slugs, provider model
ids, dims) may live. Everything else — providers, stores, routes, migration,
frontends — derives from EMBEDDING_MODELS. A Task-16 guard test enforces
that no embedding-model literal appears elsewhere in the tree.

Same config-as-data pattern as CU_MODEL_FILTERS in Orchestrator/config.py:
when a new embedding model ships, add an entry here — no code changes.

Never repoint model_id under an existing slug — slugs key persistent vector
stores and the ToolVault cache.

tokenizer (WI-11): backend spec consumed by Orchestrator/tokenization.py.
"tiktoken:<encoding>" / "hf:<vendored-dir>" count exactly and locally from
Orchestrator/tokenizers_vendored/ (offline-safe); "remote:<provider>" means
exact counts exist only via the explicit-only count_tokens_remote seam —
hot paths use the calibrated floor. None = floor always.

max_input_tokens (WI-1): the per-model input limit the token-aware clamp in
providers.py budgets against (clamp budget = 90% of this; Ollama also sends
it verbatim as options.num_ctx with truncate:false). Every entry MUST declare
it (guard-tested) — a missing limit would silently disable clamping.

junk_floor (WI-3/M9, audit A8): nullable per-model NOISE floor for the
canonical retriever. Consumed by Orchestrator/retrieval.py ONLY when
[retrieval] registry_floor_enabled is true (default false — inert); null =
the global [retrieval] junk_floor applies. Calibrated from
scripts/calibrate_threshold.py NOISE bands (off-topic top-1 ceiling): it
drops obvious junk and is NEVER relevance selection — ranking (RRF + recency
+ MMR) does relevance. Guard-tested present on every entry, numeric when
non-null, and STRICTLY below semantic_threshold (noise floors sit under
relevance bands by definition).
"""

EMBEDDING_MODELS = {
    "gemini-embedding-001": {
        "provider": "gemini", "model_id": "models/gemini-embedding-001", "dims": 3072,
        "label": "Gemini (cloud)", "ram_gb": 0.0, "cost_per_1m_tokens": 0.15,
        "privacy": "cloud", "quality_note": "Current default; auto-tracked for deprecation",
        "query_instruction": None, "keep_alive": None, "semantic_threshold": 0.60,
        # v1 whole-snapshot store, never calibrated on chunk-max scoring — no
        # measured noise band to ship; the global [retrieval] junk_floor
        # applies even with registry_floor_enabled on.
        "junk_floor": None,
        "tokenizer": "remote:gemini",
        "max_input_tokens": 2048,  # provider-documented input limit for embedding-001
        # NOTE: via the chars/2 floor (no local tokenizer) the 2048 budget clamps to
        # 3,686 chars ≈ ~900-1,270 real Gemini tokens — the fresh-box default embeds
        # LESS head text than the legacy 10k-char cap effectively did. Diagnosable
        # marker for any recall report on a gemini-001 box; real fix = post-M6
        # fail-loud/calibration, out of M5 scope.
    },
    "gemini-embedding-2": {
        "provider": "gemini", "model_id": "models/gemini-embedding-2", "dims": 3072,
        "label": "Gemini 2 (cloud, multimodal)", "ram_gb": 0.0, "cost_per_1m_tokens": 0.20,
        "privacy": "cloud", "quality_note": "Newest Gemini embedding (multimodal); re-embed required to switch",
        "query_instruction": None, "keep_alive": None,
        # Recalibrated 2026-07-02 on the LIVE schema-2 chunk-max store
        # (scripts/calibrate_threshold.py --schema 2): relevance band
        # 0.6256–0.7899 (worst relevant top-10 hit 0.6256), noise ceiling
        # 0.6125 — 0.62 sits above every off-topic top-1 and just under the
        # worst relevant hit. Display/log-only in ranking since Phase 3b-2
        # (feeds semantic_retrieve's retained-but-unused threshold param); the
        # live ranking floor is junk_floor below. (The previous 0.55 was the
        # 2026-06-21 v1 whole-snapshot calibration — superseded by the chunk
        # store cutover.)
        "semantic_threshold": 0.62,
        # Chunk-max calibration 2026-07-02 (same run as above): noise band
        # 0.529–0.6125, relevance ≥0.6256. 0.55 drops sub-noise junk while
        # keeping every relevant hit with 0.07+ margin; deliberately NOT in
        # the 0.58–0.62 discrimination zone — the relevance/noise band gap is
        # only +0.013 (the calibration script itself warned "bands too close,
        # pick manually"), far too thin to select on. Ranking, not this floor,
        # does relevance.
        "junk_floor": 0.55,
        "tokenizer": "remote:gemini",
        "max_input_tokens": 8192,  # provider-documented input limit for gemini-embedding-2
    },
    "openai-text-embedding-3-large": {
        "provider": "openai", "model_id": "text-embedding-3-large", "dims": 3072,
        "label": "OpenAI (cloud)", "ram_gb": 0.0, "cost_per_1m_tokens": 0.13,
        "privacy": "cloud", "quality_note": "Second cloud option (BYOK OpenAI key)",
        "query_instruction": None, "keep_alive": None,
        "semantic_threshold": 0.55,  # documented default (no BYOK key to live-measure)
        "junk_floor": None,  # no store on this box — nothing to calibrate; global floor applies
        "tokenizer": "tiktoken:cl100k_base",
        "max_input_tokens": 8191,  # provider-documented input limit for text-embedding-3-large
    },
    "qwen3-embedding-0.6b": {
        "provider": "ollama", "model_id": "qwen3-embedding:0.6b", "dims": 1024,
        "label": "Qwen3 0.6B (local, light)", "ram_gb": 1.0, "cost_per_1m_tokens": 0.0,
        "privacy": "local", "quality_note": "Fast on CPU; fully offline",
        "query_instruction": "Instruct: Given a search query, retrieve relevant conversation snapshots\nQuery: ",
        "keep_alive": "-1m",  # negative duration = stay loaded; bare "-1" fails Go ParseDuration
        "semantic_threshold": 0.54,
        # v1 whole-snapshot NOISE measurement (audit A8, 2026-07-01): qwen
        # scores run low — on-topic top-1 hits land ~0.45, so a gemini-band
        # floor (0.54/0.55) returns EMPTY on the phone-lean semantic-only
        # profile (the documented wipe scenario; permanent regression test in
        # test_retrieval_junk_floor.py). Measured noise band ≈0.35–0.40; 0.35
        # keeps every on-topic hit. This store is still schema 1, so the
        # v1-era band applies — re-measure on chunk-max if/when it rebuilds.
        "junk_floor": 0.35,
        "tokenizer": "hf:qwen3",
        # model supports 32,768 but we provision 8,192: num_ctx KV allocation at
        # 32k ≈ 3.7GB CPU RAM per loaded model; 8,192 covers p99 whole snapshots
        # (~7k tokens) pre-chunking and ALL chunks post-WI-2; raise post-GPU if
        # measured need.
        "max_input_tokens": 8192,
    },
    "qwen3-embedding-8b": {
        "provider": "ollama", "model_id": "qwen3-embedding:8b", "dims": 4096,
        "label": "Qwen3 8B (Ollama · Q4)", "ram_gb": 6.0, "cost_per_1m_tokens": 0.0,
        "privacy": "local", "quality_note": "MTEB #1 open-source; Ollama-served Q4_K_M (~6GB) — the on-box Q8_0 build is higher fidelity",
        "query_instruction": "Instruct: Given a search query, retrieve relevant conversation snapshots\nQuery: ",
        "keep_alive": "5m",
        "semantic_threshold": 0.50,  # documented default; local Qwen scores run low (0.6b uses 0.54), 16-row store not live-measurable
        # Same v1-era qwen noise band as the 0.6b entry (audit A8): local Qwen
        # scores run low, 0.35 protects the phone-lean profile from the wipe
        # scenario. Store is v1 (16 rows, not independently measurable) —
        # re-measure on chunk-max if/when it rebuilds.
        "junk_floor": 0.35,
        "tokenizer": "hf:qwen3",  # sample-encode-verified identical to the 0.6B tokenizer (scripts/vendor_tokenizers.py)
        # model supports 32,768 but we provision 8,192: num_ctx KV allocation at
        # 32k ≈ 3.7GB CPU RAM per loaded model; 8,192 covers p99 whole snapshots
        # (~7k tokens) pre-chunking and ALL chunks post-WI-2; raise post-GPU if
        # measured need.
        "max_input_tokens": 8192,
    },
    "qwen3-embedding-8b-local": {
        "provider": "localstack", "model_id": "embed-qwen3-8b", "dims": 4096,
        "label": "Qwen3 8B (on-box CUDA · Q8_0 · max quality)", "ram_gb": 8.1, "cost_per_1m_tokens": 0.0,
        "privacy": "local",
        "quality_note": "MTEB #1 open-source; GPU-served on-box via llama-swap (Q8_0 — highest-fidelity local build)",
        "query_instruction": "Instruct: Given a search query, retrieve relevant conversation snapshots\nQuery: ",
        # On-box keep-warm is the llama-swap member ttl (0 = warm), read by
        # store.get_keep_alive — NOT keep_alive.json. Registry default = cold.
        "keep_alive": None,
        # Seeded from the Ollama qwen3-embedding-8b entry pending G1 recalibration
        # on the RTX 2000 Ada Q8_0 store (per-model thresholds are mandatory).
        "semantic_threshold": 0.50,
        "junk_floor": 0.35,
        # Same Qwen3 tokenizer as the Ollama qwen entries (vendored hf:qwen3);
        # mandatory — llama-server pooling needs exact-length inputs.
        "tokenizer": "hf:qwen3",
        # llama-server launches with -c/-b/-ub 8192 (non-causal last-token
        # pooling forces ub >= full input seq); covers p99 whole snapshots.
        "max_input_tokens": 8192,
    },
    "qwen3-embedding-0.6b-local": {
        "provider": "localstack", "model_id": "embed-qwen3-0.6b", "dims": 1024,
        "label": "Qwen3 0.6B (on-box, light / CPU tier)", "ram_gb": 1.0, "cost_per_1m_tokens": 0.0,
        "privacy": "local",
        "quality_note": "Fast on CPU; on-box CPU-tier default via llama-swap",
        "query_instruction": "Instruct: Given a search query, retrieve relevant conversation snapshots\nQuery: ",
        "keep_alive": None,
        "semantic_threshold": 0.54,
        "junk_floor": 0.35,
        "tokenizer": "hf:qwen3",
        "max_input_tokens": 8192,
    },
}

# The store that historical inline 3072-dim index vectors transcode into
# (every inline vector ever written came from the legacy Gemini default).
# Deliberately NOT the active slug — an operator may already have pointed
# the active model elsewhere by the time the transcode runs.
LEGACY_INLINE_SLUG = "gemini-embedding-001"
