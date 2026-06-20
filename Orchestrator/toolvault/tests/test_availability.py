from Orchestrator.toolvault import availability as av


def test_no_gate_means_available():
    assert av.is_available({"name": "roll_dice"}, enabled=set(), env={}) is True


def test_gate_requires_env_present():
    entry = {"name": "grok_web_search",
             "x-availability": {"provider": "grok", "requires_env": ["XAI_API_KEY"]}}
    assert av.is_available(entry, enabled={"grok"}, env={"XAI_API_KEY": "k"}) is True
    assert av.is_available(entry, enabled={"grok"}, env={}) is False           # key missing
    assert av.is_available(entry, enabled=set(), env={"XAI_API_KEY": "k"}) is False  # not enabled


def test_duckduckgo_no_key_but_needs_enable():
    entry = {"name": "duckduckgo_web_search",
             "x-availability": {"provider": "duckduckgo", "requires_env": []}}
    assert av.is_available(entry, enabled={"duckduckgo"}, env={}) is True
    assert av.is_available(entry, enabled=set(), env={}) is False


def test_enabled_default_when_pref_unset_is_all_with_keys(monkeypatch):
    # WEB_SEARCH_ENABLED unset -> every provider with a key is enabled + duckduckgo
    monkeypatch.setattr(av, "_read_env", lambda: {"PERPLEXITY_API_KEY": "k", "XAI_API_KEY": "k"})
    enabled = av.enabled_web_search_providers()
    assert "perplexity" in enabled and "grok" in enabled and "grok_x" in enabled
    assert "duckduckgo" in enabled
    assert "openai" not in enabled  # no OPENAI key


def test_explicit_enabled_pref_overrides_default(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {
        "PERPLEXITY_API_KEY": "k", "XAI_API_KEY": "k",
        "WEB_SEARCH_ENABLED": "perplexity,duckduckgo"})
    enabled = av.enabled_web_search_providers()
    assert enabled == {"perplexity", "duckduckgo"}


def test_filter_available_passes_ungated_and_drops_unavailable(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {})  # no keys, no pref
    entries = [
        {"name": "roll_dice"},  # no gate -> kept
        {"name": "grok_web_search", "x-availability": {"provider": "grok", "requires_env": ["XAI_API_KEY"]}},  # dropped (no key)
        {"name": "duckduckgo_web_search", "x-availability": {"provider": "duckduckgo", "requires_env": []}},  # kept (ddg default-enabled)
    ]
    names = {e["name"] for e in av.filter_available(entries)}
    assert "roll_dice" in names
    assert "grok_web_search" not in names
    assert "duckduckgo_web_search" in names


def test_gemini_gate_accepts_gemini_api_key_alias(tmp_path, monkeypatch):
    # The executor's effective gemini key is GEMINI_API_KEY or GOOGLE_API_KEY;
    # the gate (keyed on GOOGLE_API_KEY) must pass when ONLY GEMINI_API_KEY is set.
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("GEMINI_API_KEY=testkey\n")
    monkeypatch.setattr(av, "_ROOT", str(tmp_path))
    env = av._read_env()
    assert env.get("GOOGLE_API_KEY") == "testkey"  # aliased from GEMINI_API_KEY
    assert "gemini" in av.enabled_web_search_providers()
