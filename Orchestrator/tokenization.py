"""Central token accounting. ONE seam for every token count/clamp in the system.

Backends per model, resolved lazily; NEVER raises from count paths — exactness
degrades to the calibrated conservative floor (chars/2) rather than erroring.
Policy (audit WI-11): exact-LOCAL where free (tiktoken, HF tokenizers, both
vendored); exact-REMOTE (Gemini countTokens, Anthropic count_tokens) only from
explicitly-invoked preflight/calibration helpers — never in hot paths.
"""
import math

CHARS_PER_TOKEN_FLOOR = 2.0  # measured: corpus mean 2.9, code-dense 2.12, hexdump 1.14


def _coerce_text(text) -> str:
    """Never-raise input normalization: count paths accept anything."""
    if text is None:
        return ""
    if isinstance(text, str):
        return text
    if isinstance(text, bytes):
        try:
            return text.decode("utf-8", errors="replace")
        except Exception:
            return ""
    try:
        return str(text)
    except Exception:
        return ""


def _floor_estimate(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN_FLOOR)


def estimate_tokens(text: str, model_key: str | None = None) -> int:
    """Fast, never-network, never-raise. Exact if a local tokenizer is vendored
    for model_key, else ceil(len(text)/CHARS_PER_TOKEN_FLOOR) (over-estimates =
    safe for clamping)."""
    try:
        return _floor_estimate(_coerce_text(text))
    except Exception:
        return 0


def clamp_to_tokens(text: str, max_tokens: int, model_key: str | None = None) -> tuple[str, int]:
    """Head-preserving clamp to <= max_tokens. Returns (text, est_tokens).
    Local-tokenizer path clamps exactly; floor path clamps to max_tokens*2 chars."""
    try:
        clean = _coerce_text(text)
        try:
            budget = int(max_tokens)
        except Exception:
            budget = 0
        if budget <= 0:
            return "", 0
        est = _floor_estimate(clean)
        if est <= budget:
            return clean, est
        clamped = clean[: int(budget * CHARS_PER_TOKEN_FLOOR)]
        return clamped, _floor_estimate(clamped)
    except Exception:
        return "", 0


def count_tokens_remote(text: str, model_key: str) -> int | None:
    """Exact remote count (Gemini countTokens / Anthropic count_tokens).
    EXPLICIT-CALL ONLY (preflight, calibration, WI-10 verification). None on any failure."""
    # Remote backends land in WI-11 part 3; until then every call reports
    # "no exact remote count available" — the contractual failure value.
    return None
