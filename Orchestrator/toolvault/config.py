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
EMBEDDING_MODEL = "models/gemini-embedding-001"
EMBEDDING_TASK_TYPE_DOC = "retrieval_document"   # For indexing tool descriptions
EMBEDDING_TASK_TYPE_QUERY = "retrieval_query"     # For search queries
EMBEDDING_DIMENSIONS = 3072                       # Gemini embedding-001 output size
EMBEDDING_MAX_CHARS = 10000                       # Truncate before embedding
EMBEDDING_MAX_RETRIES = 3

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
