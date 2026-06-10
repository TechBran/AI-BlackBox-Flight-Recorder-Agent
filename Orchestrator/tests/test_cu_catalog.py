"""CU production pass — model catalog config + filter rules.

Per docs/plans/2026-06-10-cu-production-pass-design.md §1.
"""
import re

import pytest

from Orchestrator.config import (
    CU_MODEL_DEFAULT,
    CU_GEMINI_MODEL_DEFAULT,
    CU_MODEL_FILTERS,
    CU_NATIVE_MODE,
    CU_CHROME_PATH,
    CU_MAX_ITERATIONS,
    CU_SESSION_TIMEOUT,
)


def test_cu_config_values_exist_and_typed():
    assert isinstance(CU_MODEL_DEFAULT, str) and CU_MODEL_DEFAULT.startswith("claude-")
    assert "computer-use" in CU_GEMINI_MODEL_DEFAULT
    assert isinstance(CU_NATIVE_MODE, bool)
    assert isinstance(CU_CHROME_PATH, str)
    assert CU_MAX_ITERATIONS > 0
    assert CU_SESSION_TIMEOUT > 0


@pytest.mark.parametrize("backend,model_id,expected", [
    # Anthropic: 4+-series opus/sonnet pass, haiku and 3.x fail
    ("anthropic", "claude-opus-4-6", True),
    ("anthropic", "claude-opus-4-8", True),
    ("anthropic", "claude-sonnet-4-6", True),
    ("anthropic", "claude-opus-5", True),            # future-shaped
    ("anthropic", "claude-sonnet-5-2", True),        # future-shaped
    ("anthropic", "claude-haiku-4-5-20251001", False),
    ("anthropic", "claude-3-5-sonnet-20241022", False),
    # Google: id must contain computer-use
    ("google", "gemini-2.5-computer-use-preview-10-2025", True),
    ("google", "gemini-3-computer-use-preview", True),  # future-shaped
    ("google", "gemini-2.5-flash", False),
    ("google", "gemini-3.1-pro-preview", False),
    # OpenAI: computer-use-preview family only
    ("openai", "computer-use-preview", True),
    ("openai", "computer-use-preview-2025-03-11", True),
    ("openai", "gpt-5.1", False),
])
def test_cu_filter_rules(backend, model_id, expected):
    pattern = CU_MODEL_FILTERS[backend]
    assert bool(re.match(pattern, model_id)) is expected, (
        f"{backend} filter {pattern!r} on {model_id!r}: expected {expected}"
    )
