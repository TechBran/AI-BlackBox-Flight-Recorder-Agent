"""Pluggable embeddings — registry shape + config section (Task 1).

Per docs/plans/2026-06-11-pluggable-embeddings.md Task 1: the registry in
Orchestrator/embeddings/registry.py is the SINGLE source of truth for
embedding-model data (same config-as-data pattern as CU_MODEL_FILTERS).
"""
import re

import pytest

from Orchestrator import config
from Orchestrator.embeddings.registry import EMBEDDING_MODELS

VALID_PROVIDERS = {"gemini", "openai", "ollama"}
VALID_PRIVACY = {"cloud", "local"}


def test_registry_not_empty():
    assert isinstance(EMBEDDING_MODELS, dict) and EMBEDDING_MODELS


@pytest.mark.parametrize("slug", list(EMBEDDING_MODELS))
def test_entry_has_required_fields(slug):
    entry = EMBEDDING_MODELS[slug]
    assert entry["provider"] in VALID_PROVIDERS
    assert isinstance(entry["model_id"], str) and entry["model_id"]
    assert isinstance(entry["dims"], int) and entry["dims"] > 0
    assert isinstance(entry["label"], str) and entry["label"]
    assert isinstance(entry["ram_gb"], float)
    assert isinstance(entry["cost_per_1m_tokens"], float)
    assert entry["privacy"] in VALID_PRIVACY
    assert isinstance(entry["quality_note"], str)
    assert entry["query_instruction"] is None or isinstance(entry["query_instruction"], str)
    assert entry["keep_alive"] is None or isinstance(entry["keep_alive"], str)
    # WI-11: every model must declare its tokenizer backend spec (None = floor)
    assert "tokenizer" in entry, f"{slug}: missing WI-11 tokenizer key"
    tok = entry["tokenizer"]
    assert tok is None or re.fullmatch(r"(tiktoken|hf|remote):[a-z0-9_.\-]+", tok), (
        f"{slug}: tokenizer {tok!r} is not a valid backend spec"
    )


@pytest.mark.parametrize("slug", list(EMBEDDING_MODELS))
def test_cloud_zero_ram_local_zero_cost(slug):
    entry = EMBEDDING_MODELS[slug]
    if entry["privacy"] == "cloud":
        assert entry["ram_gb"] == 0.0, f"{slug}: cloud models must declare ram_gb=0"
    else:
        assert entry["cost_per_1m_tokens"] == 0.0, f"{slug}: local models must declare cost=0"


@pytest.mark.parametrize("slug,dims", [
    ("gemini-embedding-001", 3072),
    ("openai-text-embedding-3-large", 3072),
    ("qwen3-embedding-0.6b", 1024),
    ("qwen3-embedding-8b", 4096),
])
def test_exact_slugs_present_with_dims(slug, dims):
    assert slug in EMBEDDING_MODELS
    assert EMBEDDING_MODELS[slug]["dims"] == dims


@pytest.mark.parametrize("slug", list(EMBEDDING_MODELS))
def test_slugs_are_kebab_case(slug):
    assert re.fullmatch(r"[a-z0-9.\-]+", slug), f"slug {slug!r} is not kebab-case"


def test_config_active_default_is_registry_slug():
    assert config.EMBEDDINGS_ACTIVE_DEFAULT in EMBEDDING_MODELS


def test_config_stores_dir_ends_with_manifest_embeddings():
    assert isinstance(config.EMBEDDINGS_STORES_DIR, str)
    assert config.EMBEDDINGS_STORES_DIR.endswith("Manifest/embeddings")


def test_config_ollama_base_url():
    assert isinstance(config.OLLAMA_BASE_URL, str)
    assert config.OLLAMA_BASE_URL.startswith("http")


def test_every_model_declares_explicit_semantic_threshold():
    """A model-agnostic retriever requires each model to own its similarity
    floor. Inheriting the global 0.60 silently mis-cuts a model whose score
    distribution differs (regression guard for gemini-embedding-2 / F1)."""
    missing = [slug for slug, e in EMBEDDING_MODELS.items()
               if e.get("semantic_threshold") is None]
    assert missing == [], f"models without an explicit semantic_threshold: {missing}"


def test_every_model_declares_max_input_tokens():
    """WI-1: the token-aware embedding clamp derives each model's budget from
    the registry. A missing/invalid limit would silently disable clamping and
    reopen the Ollama 4,095-token silent-truncation hole."""
    bad = [slug for slug, e in EMBEDDING_MODELS.items()
           if not isinstance(e.get("max_input_tokens"), int)
           or e.get("max_input_tokens") <= 0]
    assert bad == [], f"models without a positive int max_input_tokens: {bad}"
