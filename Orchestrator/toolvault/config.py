"""
ToolVault Configuration - Paths and constants.

The ToolVault (v2) defines tools as Python modules and indexes them with
semantic embeddings cached in an embeddings.json store for O(k) retrieval:
  Tool modules  →  Embedding vectors store  →  Hybrid search
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Project root (matches existing config.py pattern)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # blackbox_poc/

# ---------------------------------------------------------------------------
# Data paths (at project root, alongside Volumes/ and Manifest/)
# ---------------------------------------------------------------------------
TOOLVAULT_DIR = PROJECT_ROOT / "ToolVault"

# ---------------------------------------------------------------------------
# Embedding configuration
# ---------------------------------------------------------------------------
# Model, dims, truncation and retries all live in the SHARED embedding layer
# (Orchestrator/embeddings/registry.py + providers.py). ToolVault resolves
# the active model lazily via Orchestrator.embeddings.search.get_active_slug()
# — never hardcode an embedding-model literal here (guard-tested in Task 16).

# ---------------------------------------------------------------------------
# Search configuration
# ---------------------------------------------------------------------------
KEYWORD_WEIGHT = 0.4    # 40% keyword score
SEMANTIC_WEIGHT = 0.6   # 60% semantic score
DEFAULT_SEARCH_LIMIT = 10
SIMILARITY_THRESHOLD = 0.5  # Minimum cosine similarity for retrieval

# ---------------------------------------------------------------------------
# Tier definitions
# ---------------------------------------------------------------------------
# Tier 1: Always loaded into every context (core capabilities)
# Tier 2: Semantically retrieved on-demand (most tools)
# Tier 3: Self-minted, requires human approval before going live

TIER_1 = 1  # Always in context
TIER_2 = 2  # Semantic retrieval
TIER_3 = 3  # Approval-gated

DEFAULT_TIER = TIER_2
