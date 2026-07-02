"""WI-11 central tokenization module — retrieval-upgrade M2.

Per docs/plans/2026-07-01-retrieval-upgrade-implementation.md M2:
`Orchestrator/tokenization.py` is the ONE seam for every token count/clamp
in the system. Count paths NEVER raise and NEVER touch the network —
exactness degrades to the calibrated conservative floor (chars/2).

Floor semantics (audit WI-11): CHARS_PER_TOKEN_FLOOR=2.0 was measured
against this corpus (mean 2.9 chars/token, code-dense 2.12, hexdump 1.14).
The floor OVER-estimates token counts for typical prose/code (safe for
clamping); pathological hexdump-style text can exceed the token estimate,
so the floor path's hard guarantee is the CHAR budget (max_tokens * 2),
not the token budget — only local-tokenizer backends guarantee the token
budget exactly.
"""
import math

import pytest

from Orchestrator.tokenization import (
    CHARS_PER_TOKEN_FLOOR,
    clamp_to_tokens,
    count_tokens_remote,
    estimate_tokens,
)

# Strings with well-known true cl100k_base token counts, used to show the
# floor OVER-estimates typical text (true counts pinned from tiktoken).
KNOWN_STRINGS = [
    ("hello world", 2),
    ("The quick brown fox jumps over the lazy dog.", 10),
    ("def estimate_tokens(text: str) -> int:\n    return len(text) // 2\n", 19),
]


def test_floor_constant_is_calibrated_value():
    assert CHARS_PER_TOKEN_FLOOR == 2.0


# ── estimate_tokens: floor path ──────────────────────────────────────────────

@pytest.mark.parametrize("text,true_tokens", KNOWN_STRINGS)
def test_floor_overestimates_known_strings(text, true_tokens):
    """chars/2 must over-estimate typical prose/code (true ≈ chars/2.9-4)."""
    est = estimate_tokens(text)  # no model_key → floor
    assert est >= true_tokens, f"floor {est} under-estimates true {true_tokens}"


@pytest.mark.parametrize("text", [t for t, _ in KNOWN_STRINGS])
def test_floor_formula_is_ceil_chars_over_two(text):
    assert estimate_tokens(text) == math.ceil(len(text) / CHARS_PER_TOKEN_FLOOR)


def test_empty_text_estimates_zero():
    assert estimate_tokens("") == 0


def test_none_model_key_and_unknown_model_key_both_use_floor():
    text = "x" * 100
    assert estimate_tokens(text, None) == 50
    assert estimate_tokens(text, "no-such-model-slug") == 50


# ── clamp_to_tokens: floor path ──────────────────────────────────────────────

def test_clamp_under_budget_is_identity():
    text = "short text"
    clamped, est = clamp_to_tokens(text, 1000)
    assert clamped == text
    assert est == estimate_tokens(text)


def test_clamp_over_budget_respects_char_budget_and_preserves_head():
    text = "word " * 1000  # 5000 chars → floor estimate 2500 tokens
    max_tokens = 100
    clamped, est = clamp_to_tokens(text, max_tokens)
    # floor-path hard guarantee: char budget
    assert len(clamped) <= max_tokens * CHARS_PER_TOKEN_FLOOR
    # head-preserving: result is a prefix of the input
    assert text.startswith(clamped)
    # returned estimate describes the returned text and fits the budget
    assert est == estimate_tokens(clamped)
    assert est <= max_tokens


def test_clamp_hexdump_style_still_respects_char_budget():
    """Hexdump text (measured 1.14 chars/token) beats the 2.0 floor, but the
    clamp is by ESTIMATE so the char budget still binds — never overflows."""
    hexdump = "0a 1b 2c 3d 4e 5f 60 71 " * 200  # 4800 chars
    max_tokens = 50
    clamped, est = clamp_to_tokens(hexdump, max_tokens)
    assert len(clamped) <= max_tokens * CHARS_PER_TOKEN_FLOOR
    assert hexdump.startswith(clamped)
    assert est <= max_tokens


def test_clamp_empty_text():
    assert clamp_to_tokens("", 10) == ("", 0)


@pytest.mark.parametrize("max_tokens", [0, -1, -100])
def test_clamp_zero_or_negative_budget_returns_empty(max_tokens):
    clamped, est = clamp_to_tokens("some text", max_tokens)
    assert clamped == ""
    assert est == 0


# ── never-raise guarantee on garbage input ───────────────────────────────────

GARBAGE = [None, 12345, 3.14, b"raw bytes", ["a", "list"], {"a": "dict"}]


@pytest.mark.parametrize("garbage", GARBAGE, ids=[repr(g) for g in GARBAGE])
def test_estimate_tokens_never_raises_on_garbage(garbage):
    result = estimate_tokens(garbage)
    assert isinstance(result, int)
    assert result >= 0


@pytest.mark.parametrize("garbage", GARBAGE, ids=[repr(g) for g in GARBAGE])
def test_clamp_never_raises_on_garbage(garbage):
    clamped, est = clamp_to_tokens(garbage, 10)
    assert isinstance(clamped, str)
    assert isinstance(est, int)


def test_estimate_handles_lone_surrogates_and_control_chars():
    weird = "\ud800\x00\x1f�" + "normal tail"
    result = estimate_tokens(weird)
    assert isinstance(result, int) and result > 0
    clamped, est = clamp_to_tokens(weird, 2)
    assert isinstance(clamped, str) and est <= 2


# ── count_tokens_remote: part-1 contract (explicit-only, None on failure) ────

def test_count_tokens_remote_unknown_model_returns_none():
    assert count_tokens_remote("hello", "no-such-model-slug") is None
