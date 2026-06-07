"""
ToolVault Configuration - Paths, patterns, and constants.

The ToolVault is an immutable, append-only volume of tool definitions
indexed by byte offset with semantic embeddings for O(k) retrieval.

Architecture mirrors the proven snapshot system:
  Volume file  →  Byte-offset manifest  →  Embedding vectors  →  Hybrid search
"""

import re
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
# Anchor format
# ---------------------------------------------------------------------------
# Human-readable anchors with numbered markers + em-dash (—)
# Format: === START TOOL 001 — tool_name ===
#         === END TOOL 001 — tool_name ===
#
# Numbers auto-increment on mint. Belt-and-suspenders:
# name + number for bulletproof START/END pairing.

EM_DASH = "\u2014"  # — (UTF-8: \xe2\x80\x94)

def tool_start_anchor(number: int, name: str) -> str:
    return f"=== START TOOL {number:03d} {EM_DASH} {name} ==="

def tool_end_anchor(number: int, name: str) -> str:
    return f"=== END TOOL {number:03d} {EM_DASH} {name} ==="

# ---------------------------------------------------------------------------
# Regex patterns (text mode - for parsing decoded strings)
# ---------------------------------------------------------------------------
START_RX = re.compile(
    r'^=== START TOOL (\d{3}) \u2014 (.+?) ===$',
    re.MULTILINE
)
END_RX = re.compile(
    r'^=== END TOOL (\d{3}) \u2014 (.+?) ===$',
    re.MULTILINE
)

# Byte-level regex (for scanning raw volume bytes without decoding)
START_RX_B = re.compile(
    rb'^=== START TOOL (\d{3}) \xe2\x80\x94 (.+?) ===$',
    re.MULTILINE
)
END_RX_B = re.compile(
    rb'^=== END TOOL (\d{3}) \xe2\x80\x94 (.+?) ===$',
    re.MULTILINE
)

# Field extraction (within a tool block)
FIELD_RX = re.compile(r'^([A-Z_]+):\s*(.*)$', re.MULTILINE)
JSON_SCHEMA_RX = re.compile(
    r'^JSON_SCHEMA:\s*\n(.+?)(?=\n[A-Z_]+:|\n=== END TOOL)',
    re.MULTILINE | re.DOTALL
)

# ---------------------------------------------------------------------------
# Tool block fields (order matters for formatting)
# ---------------------------------------------------------------------------
BLOCK_FIELDS = [
    "NAME",
    "DESCRIPTION",
    "CATEGORY",
    "GROUPS",
    "PARAMETERS",
    "RETURNS",
    "EXAMPLE",
    "NOTES",
    "JSON_SCHEMA",
]

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
