"""Central token accounting. ONE seam for every token count/clamp in the system.

Backends per model, resolved lazily; NEVER raises from count paths — exactness
degrades to the calibrated conservative floor (chars/2) rather than erroring.
Policy (audit WI-11): exact-LOCAL where free (tiktoken, HF tokenizers, both
vendored); exact-REMOTE (Gemini countTokens, Anthropic count_tokens) only from
explicitly-invoked preflight/calibration helpers — never in hot paths.
"""
import logging
import math
import os
from pathlib import Path

from Orchestrator.embeddings.registry import EMBEDDING_MODELS

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN_FLOOR = 2.0  # measured: corpus mean 2.9, code-dense 2.12, hexdump 1.14

# Vendored tokenizer assets, populated ONCE at build time by
# scripts/vendor_tokenizers.py and committed — local backends load from here
# ONLY, so exact counting works with networking disabled.
VENDORED_DIR = Path(__file__).resolve().parent / "tokenizers_vendored"


# ── local backends (lazy, vendored-only, fail-to-floor) ─────────────────────

def _load_tiktoken_cl100k():
    # Force the cache dir to the vendored copy BEFORE first use: an inherited
    # TIKTOKEN_CACHE_DIR (or an empty cache) would make tiktoken try the
    # network, breaking the offline guarantee this module exists to provide.
    os.environ["TIKTOKEN_CACHE_DIR"] = str(VENDORED_DIR / "tiktoken_cache")
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")

    def encode(text: str) -> list[int]:
        # disallowed_special=() — special-token text in user content must
        # count, not raise (never-raise contract).
        return enc.encode(text, disallowed_special=())

    return encode, enc.decode


def _load_hf_qwen3():
    from tokenizers import Tokenizer

    tok = Tokenizer.from_file(str(VENDORED_DIR / "qwen3" / "tokenizer.json"))

    def encode(text: str) -> list[int]:
        # Default add_special_tokens=True matches Ollama's prompt_eval_count
        # exactly (live probe 2026-07-02: both said 38 for the 200-char
        # reference string in test_tokenization.py).
        return tok.encode(text).ids

    def decode(ids: list[int]) -> str:
        return tok.decode(ids, skip_special_tokens=True)

    return encode, decode


# ONE table: registry "tokenizer" spec → lazy loader returning
# (encode: str -> list[int], decode: list[int] -> str).
# "remote:*" specs deliberately have NO entry here — remote exactness lives
# only in count_tokens_remote (explicit-call-only policy).
_BACKEND_LOADERS = {
    "tiktoken:cl100k_base": _load_tiktoken_cl100k,
    "hf:qwen3": _load_hf_qwen3,
}

_backends: dict = {}  # spec → (encode, decode) | None (None = load failed → floor)


def _resolve_backend(model_key):
    """(encode, decode) for model_key's local tokenizer, else None (→ floor).
    Load failures log once per spec and pin None — never raise."""
    if not model_key:
        return None
    try:
        spec = EMBEDDING_MODELS.get(model_key, {}).get("tokenizer")
    except Exception:
        return None
    if spec not in _BACKEND_LOADERS:
        return None  # remote:* / None / unknown spec → floor
    if spec not in _backends:
        try:
            _backends[spec] = _BACKEND_LOADERS[spec]()
        except Exception as exc:
            _backends[spec] = None
            logger.warning(
                "[TOKENIZATION] backend %s failed to load (%s) — "
                "falling back to chars/%s floor", spec, exc, CHARS_PER_TOKEN_FLOOR,
            )
    return _backends[spec]


# ── shared helpers ───────────────────────────────────────────────────────────

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


def _floor_clamp(text: str, budget: int) -> tuple[str, int]:
    if _floor_estimate(text) <= budget:
        return text, _floor_estimate(text)
    clamped = text[: int(budget * CHARS_PER_TOKEN_FLOOR)]
    return clamped, _floor_estimate(clamped)


def _exact_clamp(text: str, budget: int, encode, decode) -> tuple[str, int]:
    """Head-preserving exact clamp: keep the first `budget` token ids. Decoding
    a truncated id sequence can re-encode slightly differently, so shrink until
    the re-encode fits — the returned estimate is the real count of the
    returned text."""
    ids = encode(text)
    if len(ids) <= budget:
        return text, len(ids)
    take = budget
    while take > 0:
        candidate = decode(ids[:take])
        n = len(encode(candidate))
        if n <= budget:
            return candidate, n
        take -= max(1, n - budget)
    return "", 0


# ── public API ───────────────────────────────────────────────────────────────

def estimate_tokens(text: str, model_key: str | None = None) -> int:
    """Fast, never-network, never-raise. Exact if a local tokenizer is vendored
    for model_key, else ceil(len(text)/CHARS_PER_TOKEN_FLOOR) (over-estimates =
    safe for clamping)."""
    try:
        clean = _coerce_text(text)
        backend = _resolve_backend(model_key)
        if backend is not None:
            try:
                return len(backend[0](clean))
            except Exception:
                pass  # unencodable input — degrade to the floor, never raise
        return _floor_estimate(clean)
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
        backend = _resolve_backend(model_key)
        if backend is not None:
            try:
                return _exact_clamp(clean, budget, backend[0], backend[1])
            except Exception:
                pass  # unencodable input — degrade to the floor, never raise
        return _floor_clamp(clean, budget)
    except Exception:
        return "", 0


# ── remote counters (WI-11 part 3) — explicit-call-only, None on failure ────
#
# Transport seams (module-level so tests fake them and the policy spy can
# prove hot paths never touch them). Lazy imports keep hot-path module load
# free of requests/anthropic.

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
REMOTE_COUNT_TIMEOUT_S = 15


def _http_post(url, **kwargs):
    import requests

    return requests.post(url, **kwargs)


def _anthropic_client_factory(api_key):
    import anthropic

    return anthropic.Anthropic(api_key=api_key)


def _anthropic_default_model_id() -> str:
    # Read at call time: config owns the chat-default choice (WI-10 window
    # math counts against whatever the box is actually configured to run).
    from Orchestrator import config

    return config.ANTHROPIC_MODEL_DEFAULT


# Non-registry remote count targets: model_key → (provider, model_id resolver).
# Embedding-registry keys route via their "tokenizer" spec instead; this table
# is ONLY for keys that have no registry entry (chat models for WI-10).
REMOTE_COUNT_KEYS = {
    "anthropic-default": ("anthropic", _anthropic_default_model_id),
}


def _count_gemini(model_id: str, text: str) -> int | None:
    from Orchestrator import config

    api_key = (getattr(config, "GEMINI_API_KEY", "")
               or getattr(config, "GOOGLE_API_KEY", ""))
    if not api_key:
        return None
    resp = _http_post(
        f"{GEMINI_API_BASE}/{model_id}:countTokens",
        headers={"x-goog-api-key": api_key},
        json={"contents": [{"parts": [{"text": text}]}]},
        timeout=REMOTE_COUNT_TIMEOUT_S,
    )
    if resp.status_code != 200:
        return None
    total = resp.json().get("totalTokens")
    return int(total) if total is not None else None


def _count_anthropic(model_id: str, text: str) -> int | None:
    from Orchestrator import config

    api_key = getattr(config, "ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    client = _anthropic_client_factory(api_key)
    result = client.messages.count_tokens(
        model=model_id,
        messages=[{"role": "user", "content": text}],
    )
    tokens = getattr(result, "input_tokens", None)
    return int(tokens) if tokens is not None else None


def count_tokens_remote(text: str, model_key: str) -> int | None:
    """Exact remote count (Gemini countTokens / Anthropic count_tokens).
    EXPLICIT-CALL ONLY (preflight, calibration, WI-10 verification). None on any failure."""
    try:
        clean = _coerce_text(text)
        entry = EMBEDDING_MODELS.get(model_key) if isinstance(model_key, str) else None
        if entry is not None:
            spec = entry.get("tokenizer") or ""
            if spec == "remote:gemini":
                return _count_gemini(entry["model_id"], clean)
            return None  # local-backend model: the exact answer is already free
        mapped = REMOTE_COUNT_KEYS.get(model_key)
        if mapped is not None:
            provider, resolve_model_id = mapped
            if provider == "anthropic":
                return _count_anthropic(resolve_model_id(), clean)
        return None
    except Exception:
        return None  # no key / network down / bad body / unknown key — all None
