"""Tests for availability.default_provider_tool — the resolver behind the on-device
HEADLESS `web_search` alias (/local/tools/execute). It must ALWAYS return a tool
whose key requirement is satisfied (keyless DuckDuckGo floor at worst), so the
phone model's web search can never resolve to an unrunnable provider."""
from Orchestrator.toolvault import availability as av


def test_keyless_floor_when_no_config(monkeypatch):
    # No default + no keys -> the always-runnable keyless DuckDuckGo floor.
    monkeypatch.setattr(av, "_read_env", lambda: {})
    assert av.default_provider_tool("web_search") == "duckduckgo_web_search"


def test_configured_default_when_keyed(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {
        "WEB_SEARCH_DEFAULT": "perplexity", "PERPLEXITY_API_KEY": "sk-x",
    })
    assert av.default_provider_tool("web_search") == "perplexity_web_search"


def test_configured_default_missing_key_falls_to_floor(monkeypatch):
    # m4: a default naming a provider whose key is absent must NOT yield an
    # unrunnable tool — fall through to the keyless floor.
    monkeypatch.setattr(av, "_read_env", lambda: {"WEB_SEARCH_DEFAULT": "perplexity"})
    assert av.default_provider_tool("web_search") == "duckduckgo_web_search"


def test_prefers_keyed_synthesized_provider_when_no_default(monkeypatch):
    # No explicit default, but a synthesized-answer provider is keyed -> prefer it
    # over the keyless snippet floor (tighter answers suit the small on-device window).
    monkeypatch.setattr(av, "_read_env", lambda: {"OPENAI_API_KEY": "sk-x"})
    assert av.default_provider_tool("web_search") == "openai_web_search"


def test_always_returns_a_known_web_search_tool(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {})
    assert av.default_provider_tool("web_search") in av.WEB_SEARCH_TOOLS
