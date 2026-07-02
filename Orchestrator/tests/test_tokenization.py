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
import socket

import pytest

from Orchestrator import tokenization
from Orchestrator.embeddings.registry import EMBEDDING_MODELS
from Orchestrator.tokenization import (
    CHARS_PER_TOKEN_FLOOR,
    clamp_to_tokens,
    count_tokens_remote,
    estimate_tokens,
)

OPENAI_SLUG = "openai-text-embedding-3-large"
QWEN_SLUG = "qwen3-embedding-0.6b"
QWEN_8B_SLUG = "qwen3-embedding-8b"

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


# ── WI-11 part 2: vendored local backends (exact, offline) ───────────────────

# Reference pinned from ONE live probe (2026-07-02, this box):
#   curl localhost:11434/api/embed -d '{"model":"qwen3-embedding:0.6b",
#     "input":"<QWEN_PROBE_TEXT below>"}'
#   → prompt_eval_count: 38
# The vendored tokenizer (default encode, add_special_tokens=True) also
# returned 38 — exact match. This test does NOT hit the network; it asserts
# against the pinned constant.
QWEN_PROBE_TEXT = (
    "The BlackBox flight recorder mints immutable conversation snapshots. "
    "Retrieval fuses semantic embeddings with keyword rank so past sessions "
    "resurface fast. Token math must be real, never guessed!! Yes"
)
QWEN_PROBE_PROMPT_EVAL_COUNT = 38


def test_probe_text_is_the_fixed_200_char_string():
    assert len(QWEN_PROBE_TEXT) == 200


def test_qwen_embedding_count_matches_live_probe_reference():
    est = estimate_tokens(QWEN_PROBE_TEXT, QWEN_SLUG)
    assert abs(est - QWEN_PROBE_PROMPT_EVAL_COUNT) <= 2, (
        f"vendored Qwen tokenizer says {est}, live prompt_eval_count was "
        f"{QWEN_PROBE_PROMPT_EVAL_COUNT} (tolerance ±2)"
    )
    # prove this took the exact path, not the floor (floor would say 100)
    assert est <= 50


def test_qwen_8b_embedding_shares_the_family_tokenizer():
    assert estimate_tokens(QWEN_PROBE_TEXT, QWEN_8B_SLUG) == estimate_tokens(
        QWEN_PROBE_TEXT, QWEN_SLUG
    )


@pytest.mark.parametrize("text,expected", [
    ("hello world", 2),
    ("The quick brown fox jumps over the lazy dog.", 10),
    (QWEN_PROBE_TEXT, 37),  # pinned from vendored cl100k_base encode
])
def test_tiktoken_exact_counts_for_openai_embedding_model(text, expected):
    assert estimate_tokens(text, OPENAI_SLUG) == expected


def test_tiktoken_never_raises_on_special_token_text():
    """cl100k encode() raises on '<|endoftext|>' unless specials are allowed —
    the count path must survive it (disallowed_special=())."""
    est = estimate_tokens("before <|endoftext|> after", OPENAI_SLUG)
    assert isinstance(est, int) and est > 0


@pytest.mark.parametrize("slug", [OPENAI_SLUG, QWEN_SLUG])
def test_local_clamp_respects_token_budget_exactly(slug):
    text = ("Snapshots persist every development session so future agents "
            "recall decisions, bugs, and fixes without re-solving them. ") * 20
    budget = 25
    clamped, est = clamp_to_tokens(text, budget, slug)
    assert clamped, "clamp emptied the text"
    assert est <= budget
    assert est == estimate_tokens(clamped, slug)
    # head-preserving (pure-ASCII input decodes back to an exact prefix)
    assert text.startswith(clamped)


def test_gemini_embedding_slugs_use_floor_in_hot_path():
    """remote:* backends never run in count paths — exact Gemini counts exist
    only via the explicit-only count_tokens_remote seam."""
    remote_slugs = [s for s, e in EMBEDDING_MODELS.items()
                    if str(e.get("tokenizer", "")).startswith("remote:")]
    assert remote_slugs, "expected at least one remote-tokenizer model"
    text = "y" * 300
    for slug in remote_slugs:
        assert estimate_tokens(text, slug) == math.ceil(
            len(text) / CHARS_PER_TOKEN_FLOOR
        )


def test_every_embedding_registry_slug_declares_tokenizer_backend():
    for slug, entry in EMBEDDING_MODELS.items():
        assert "tokenizer" in entry, f"{slug} missing WI-11 tokenizer key"


def test_registry_local_tokenizer_specs_resolve_in_backend_table():
    """Every non-remote tokenizer spec in the registry must have a loader —
    a typo'd spec would silently degrade that model to the floor forever."""
    for slug, entry in EMBEDDING_MODELS.items():
        spec = entry["tokenizer"]
        if spec is not None and not spec.startswith("remote:"):
            assert spec in tokenization._BACKEND_LOADERS, (
                f"{slug} names tokenizer {spec!r} with no loader"
            )


def test_vendored_assets_are_present():
    """The exactness guarantee rides on committed assets — a deleted vendored
    dir must fail loudly here, not silently floor every count."""
    cache = tokenization.VENDORED_DIR / "tiktoken_cache"
    assert cache.is_dir() and any(p.is_file() for p in cache.iterdir())
    assert (tokenization.VENDORED_DIR / "qwen3" / "tokenizer.json").is_file()


def test_offline_every_embedding_slug_still_counts_and_clamps(monkeypatch):
    """The OFFLINE guarantee: with sockets dead and cold backend caches, every
    registered slug still counts (exact-local or floor) and clamps."""
    monkeypatch.setattr(tokenization, "_backends", {})

    def _no_network(*args, **kwargs):
        raise AssertionError("tokenization count path touched the network")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)

    long_text = "offline check " * 50
    for slug in EMBEDDING_MODELS:
        est = estimate_tokens(long_text, slug)
        assert isinstance(est, int) and est > 0
        clamped, clamp_est = clamp_to_tokens(long_text, 10, slug)
        assert isinstance(clamped, str)
        assert clamp_est <= 10


def test_local_backend_never_raises_on_unencodable_text():
    """Lone surrogates can't cross into a real tokenizer — exactness degrades
    to the floor rather than raising."""
    weird = "\ud800abc"
    est = estimate_tokens(weird, OPENAI_SLUG)
    assert isinstance(est, int) and est > 0
    clamped, clamp_est = clamp_to_tokens(weird * 20, 5, OPENAI_SLUG)
    assert isinstance(clamped, str) and clamp_est <= 5


# ── WI-11 part 3: remote counters — explicit-only, fakes only ────────────────

GEMINI_SLUG = "gemini-embedding-2"
ANTHROPIC_KEY = "anthropic-default"


class _FakeHTTPResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


@pytest.fixture
def gemini_key(monkeypatch):
    from Orchestrator import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-gemini-key", raising=False)
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "test-gemini-key", raising=False)


@pytest.fixture
def anthropic_key(monkeypatch):
    from Orchestrator import config

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test-anthropic-key", raising=False)


def test_remote_gemini_success_parses_total_tokens(monkeypatch, gemini_key):
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["json"] = kwargs.get("json")
        return _FakeHTTPResponse(200, {"totalTokens": 7})

    monkeypatch.setattr(tokenization, "_http_post", fake_post)
    assert count_tokens_remote("hello world", GEMINI_SLUG) == 7
    # the call went to the registry model's countTokens endpoint
    assert seen["url"].endswith(
        EMBEDDING_MODELS[GEMINI_SLUG]["model_id"] + ":countTokens"
    )
    assert seen["json"]["contents"][0]["parts"][0]["text"] == "hello world"


def test_remote_gemini_non_200_returns_none(monkeypatch, gemini_key):
    monkeypatch.setattr(
        tokenization, "_http_post",
        lambda url, **k: _FakeHTTPResponse(429, {"error": "rate limited"}),
    )
    assert count_tokens_remote("hello", GEMINI_SLUG) is None


def test_remote_gemini_transport_exception_returns_none(monkeypatch, gemini_key):
    def boom(url, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(tokenization, "_http_post", boom)
    assert count_tokens_remote("hello", GEMINI_SLUG) is None


def test_remote_gemini_malformed_body_returns_none(monkeypatch, gemini_key):
    monkeypatch.setattr(
        tokenization, "_http_post",
        lambda url, **k: _FakeHTTPResponse(200, {"unexpected": True}),
    )
    assert count_tokens_remote("hello", GEMINI_SLUG) is None


def test_remote_gemini_no_key_returns_none(monkeypatch):
    from Orchestrator import config

    monkeypatch.setattr(config, "GEMINI_API_KEY", "", raising=False)
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "", raising=False)

    def fail_if_called(url, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("transport called without a key")

    monkeypatch.setattr(tokenization, "_http_post", fail_if_called)
    assert count_tokens_remote("hello", GEMINI_SLUG) is None


def test_remote_anthropic_success_parses_input_tokens(monkeypatch, anthropic_key):
    from types import SimpleNamespace

    from Orchestrator import config

    seen = {}

    class FakeMessages:
        def count_tokens(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(input_tokens=12)

    monkeypatch.setattr(
        tokenization, "_anthropic_client_factory",
        lambda api_key: SimpleNamespace(messages=FakeMessages()),
    )
    assert count_tokens_remote("hello world", ANTHROPIC_KEY) == 12
    # WI-10 window math counts against the configured chat default
    assert seen["model"] == config.ANTHROPIC_MODEL_DEFAULT
    assert seen["messages"][0]["content"] == "hello world"


def test_remote_anthropic_sdk_exception_returns_none(monkeypatch, anthropic_key):
    class FakeMessages:
        def count_tokens(self, **kwargs):
            raise RuntimeError("api error")

    from types import SimpleNamespace

    monkeypatch.setattr(
        tokenization, "_anthropic_client_factory",
        lambda api_key: SimpleNamespace(messages=FakeMessages()),
    )
    assert count_tokens_remote("hello", ANTHROPIC_KEY) is None


def test_remote_anthropic_no_key_returns_none(monkeypatch):
    from Orchestrator import config

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "", raising=False)

    def fail_if_called(api_key):  # pragma: no cover - must not run
        raise AssertionError("client built without a key")

    monkeypatch.setattr(tokenization, "_anthropic_client_factory", fail_if_called)
    assert count_tokens_remote("hello", ANTHROPIC_KEY) is None


def test_remote_local_backend_slug_returns_none(monkeypatch, gemini_key, anthropic_key):
    """Registry models with local tokenizers have no remote counter — the
    exact answer is already free and local."""
    assert count_tokens_remote("hello", OPENAI_SLUG) is None
    assert count_tokens_remote("hello", QWEN_SLUG) is None


def test_remote_counter_never_raises_on_garbage(monkeypatch, gemini_key):
    monkeypatch.setattr(
        tokenization, "_http_post",
        lambda url, **k: _FakeHTTPResponse(200, {"totalTokens": 3}),
    )
    assert count_tokens_remote(None, GEMINI_SLUG) in (None, 3)
    assert count_tokens_remote("x", None) is None
    assert count_tokens_remote("x", 12345) is None


def test_hot_paths_never_invoke_remote_transport_for_any_embedding_slug(
    monkeypatch, gemini_key, anthropic_key
):
    """THE WI-11 policy spy: estimate_tokens/clamp_to_tokens must never touch
    a remote transport for any model key — even with keys configured and cold
    backend caches."""
    calls = []

    def spy_post(url, **kwargs):
        calls.append(("http", url))
        raise AssertionError("hot path called the Gemini transport")

    def spy_factory(api_key):
        calls.append(("anthropic", api_key))
        raise AssertionError("hot path built an Anthropic client")

    monkeypatch.setattr(tokenization, "_http_post", spy_post)
    monkeypatch.setattr(tokenization, "_anthropic_client_factory", spy_factory)
    monkeypatch.setattr(tokenization, "_backends", {})

    text = "hot path text " * 100
    for key in list(EMBEDDING_MODELS) + [ANTHROPIC_KEY, None, "no-such-model"]:
        estimate_tokens(text, key)
        clamp_to_tokens(text, 20, key)

    assert calls == [], f"hot paths touched remote transports: {calls}"
