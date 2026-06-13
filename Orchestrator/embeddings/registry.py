"""Embedding-model registry — the SINGLE source of truth for embedding models.

This file is the ONLY place embedding-model literals (slugs, provider model
ids, dims) may live. Everything else — providers, stores, routes, migration,
frontends — derives from EMBEDDING_MODELS. A Task-16 guard test enforces
that no embedding-model literal appears elsewhere in the tree.

Same config-as-data pattern as CU_MODEL_FILTERS in Orchestrator/config.py:
when a new embedding model ships, add an entry here — no code changes.

Never repoint model_id under an existing slug — slugs key persistent vector
stores and the ToolVault cache.
"""

EMBEDDING_MODELS = {
    "gemini-embedding-001": {
        "provider": "gemini", "model_id": "models/gemini-embedding-001", "dims": 3072,
        "label": "Gemini (cloud)", "ram_gb": 0.0, "cost_per_1m_tokens": 0.15,
        "privacy": "cloud", "quality_note": "Current default; auto-tracked for deprecation",
        "query_instruction": None, "keep_alive": None, "semantic_threshold": 0.60,
    },
    "openai-text-embedding-3-large": {
        "provider": "openai", "model_id": "text-embedding-3-large", "dims": 3072,
        "label": "OpenAI (cloud)", "ram_gb": 0.0, "cost_per_1m_tokens": 0.13,
        "privacy": "cloud", "quality_note": "Second cloud option (BYOK OpenAI key)",
        "query_instruction": None, "keep_alive": None,
    },
    "qwen3-embedding-0.6b": {
        "provider": "ollama", "model_id": "qwen3-embedding:0.6b", "dims": 1024,
        "label": "Qwen3 0.6B (local, light)", "ram_gb": 1.0, "cost_per_1m_tokens": 0.0,
        "privacy": "local", "quality_note": "Fast on CPU; fully offline",
        "query_instruction": "Instruct: Given a search query, retrieve relevant conversation snapshots\nQuery: ",
        "keep_alive": "-1m",  # negative duration = stay loaded; bare "-1" fails Go ParseDuration
        "semantic_threshold": 0.54,
    },
    "qwen3-embedding-8b": {
        "provider": "ollama", "model_id": "qwen3-embedding:8b", "dims": 4096,
        "label": "Qwen3 8B (local, max quality)", "ram_gb": 6.0, "cost_per_1m_tokens": 0.0,
        "privacy": "local", "quality_note": "MTEB #1 open-source; slow re-embeds on CPU",
        "query_instruction": "Instruct: Given a search query, retrieve relevant conversation snapshots\nQuery: ",
        "keep_alive": "5m",
    },
}
EMBEDDING_MAX_CHARS = 10000  # truncate document text before embedding (existing behavior)

# The store that historical inline 3072-dim index vectors transcode into
# (every inline vector ever written came from the legacy Gemini default).
# Deliberately NOT the active slug — an operator may already have pointed
# the active model elsewhere by the time the transcode runs.
LEGACY_INLINE_SLUG = "gemini-embedding-001"
