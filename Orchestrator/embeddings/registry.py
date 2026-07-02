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
"""

EMBEDDING_MODELS = {
    "gemini-embedding-001": {
        "provider": "gemini", "model_id": "models/gemini-embedding-001", "dims": 3072,
        "label": "Gemini (cloud)", "ram_gb": 0.0, "cost_per_1m_tokens": 0.15,
        "privacy": "cloud", "quality_note": "Current default; auto-tracked for deprecation",
        "query_instruction": None, "keep_alive": None, "semantic_threshold": 0.60,
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
        # Calibrated 2026-06-21 via scripts/calibrate_threshold.py over the live
        # 7176-row store: worst real top-10 hit 0.5963, suggested floor 0.5463
        # (worst - 0.05); 0.55 stays clear of every strong match and well under
        # p10 (0.6291). Inheriting gemini-001's 0.60 was silently cutting good hits.
        "semantic_threshold": 0.55,
        "tokenizer": "remote:gemini",
        "max_input_tokens": 8192,  # provider-documented input limit for gemini-embedding-2
    },
    "openai-text-embedding-3-large": {
        "provider": "openai", "model_id": "text-embedding-3-large", "dims": 3072,
        "label": "OpenAI (cloud)", "ram_gb": 0.0, "cost_per_1m_tokens": 0.13,
        "privacy": "cloud", "quality_note": "Second cloud option (BYOK OpenAI key)",
        "query_instruction": None, "keep_alive": None,
        "semantic_threshold": 0.55,  # documented default (no BYOK key to live-measure)
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
        "tokenizer": "hf:qwen3",
        # model supports 32,768 but we provision 8,192: num_ctx KV allocation at
        # 32k ≈ 3.7GB CPU RAM per loaded model; 8,192 covers p99 whole snapshots
        # (~7k tokens) pre-chunking and ALL chunks post-WI-2; raise post-GPU if
        # measured need.
        "max_input_tokens": 8192,
    },
    "qwen3-embedding-8b": {
        "provider": "ollama", "model_id": "qwen3-embedding:8b", "dims": 4096,
        "label": "Qwen3 8B (local, max quality)", "ram_gb": 6.0, "cost_per_1m_tokens": 0.0,
        "privacy": "local", "quality_note": "MTEB #1 open-source; slow re-embeds on CPU",
        "query_instruction": "Instruct: Given a search query, retrieve relevant conversation snapshots\nQuery: ",
        "keep_alive": "5m",
        "semantic_threshold": 0.50,  # documented default; local Qwen scores run low (0.6b uses 0.54), 16-row store not live-measurable
        "tokenizer": "hf:qwen3",  # sample-encode-verified identical to the 0.6B tokenizer (scripts/vendor_tokenizers.py)
        # model supports 32,768 but we provision 8,192: num_ctx KV allocation at
        # 32k ≈ 3.7GB CPU RAM per loaded model; 8,192 covers p99 whole snapshots
        # (~7k tokens) pre-chunking and ALL chunks post-WI-2; raise post-GPU if
        # measured need.
        "max_input_tokens": 8192,
    },
}

# The store that historical inline 3072-dim index vectors transcode into
# (every inline vector ever written came from the legacy Gemini default).
# Deliberately NOT the active slug — an operator may already have pointed
# the active model elsewhere by the time the transcode runs.
LEGACY_INLINE_SLUG = "gemini-embedding-001"
