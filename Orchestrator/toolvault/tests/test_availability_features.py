"""Feature-aware availability gate tests (image + web_search; extensible).

Verifies the generalized FEATURES registry: per-feature enabled sets, the
image feature having NO keyless floor (unlike web_search's duckduckgo floor),
mixed-catalog filtering gating each entry against ITS feature, and back-compat
(an entry without a `feature` key resolves to web_search).
"""
from Orchestrator.toolvault import availability as av


# --- image gate ------------------------------------------------------------

def _image_entry(provider="openai", requires=("OPENAI_API_KEY",)):
    return {"name": f"{provider}_image",
            "x-availability": {"feature": "image", "provider": provider,
                               "requires_env": list(requires)}}


def test_image_available_when_key_present_and_pref_unset(monkeypatch):
    # IMAGE_ENABLED unset + key present -> available
    monkeypatch.setattr(av, "_read_env", lambda: {"OPENAI_API_KEY": "k"})
    assert av.is_available(_image_entry("openai")) is True


def test_image_unavailable_when_key_missing(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {})
    assert av.is_available(_image_entry("openai")) is False


def test_image_respects_explicit_image_enabled_pref(monkeypatch):
    # key present but provider NOT in IMAGE_ENABLED -> unavailable
    monkeypatch.setattr(av, "_read_env", lambda: {
        "OPENAI_API_KEY": "k", "GOOGLE_API_KEY": "k",
        "IMAGE_ENABLED": "gemini"})
    assert av.is_available(_image_entry("openai")) is False
    assert av.is_available(_image_entry("gemini", ("GOOGLE_API_KEY",))) is True


def test_image_has_no_keyless_floor(monkeypatch):
    # IMAGE_ENABLED unset + NO keys -> NO image providers (NOT duckduckgo)
    monkeypatch.setattr(av, "_read_env", lambda: {})
    enabled = av.enabled_providers("image")
    assert enabled == set()
    assert "duckduckgo" not in enabled


def test_enabled_providers_image_default_is_keyed_providers_only(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {
        "OPENAI_API_KEY": "k", "XAI_API_KEY": "k"})
    enabled = av.enabled_providers("image")
    assert enabled == {"openai", "grok"}
    assert "gemini" not in enabled    # no GOOGLE key
    assert "duckduckgo" not in enabled  # image has no keyless floor


# --- web_search floor preserved -------------------------------------------

def test_web_search_keeps_duckduckgo_floor(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {})  # no keys, no pref
    enabled = av.enabled_providers("web_search")
    assert "duckduckgo" in enabled


def test_enabled_web_search_providers_alias_unchanged(monkeypatch):
    monkeypatch.setattr(av, "_read_env", lambda: {"XAI_API_KEY": "k"})
    enabled = av.enabled_web_search_providers()
    assert enabled == av.enabled_providers("web_search")
    assert "duckduckgo" in enabled and "grok" in enabled and "grok_x" in enabled


# --- mixed catalog: each entry gated by ITS feature -----------------------

def test_mixed_catalog_filters_each_by_its_feature(monkeypatch):
    # OPENAI key present (image: openai ok), NO XAI key (web: grok dropped),
    # IMAGE/WEB prefs unset. duckduckgo (web floor) kept; image-gemini dropped.
    monkeypatch.setattr(av, "_read_env", lambda: {"OPENAI_API_KEY": "k"})
    entries = [
        {"name": "roll_dice"},  # ungated -> kept
        {"name": "grok_web_search",
         "x-availability": {"feature": "web_search", "provider": "grok",
                            "requires_env": ["XAI_API_KEY"]}},  # dropped (no key)
        {"name": "duckduckgo_web_search",
         "x-availability": {"feature": "web_search", "provider": "duckduckgo",
                            "requires_env": []}},  # kept (web floor)
        _image_entry("openai"),                     # kept (key + default)
        _image_entry("gemini", ("GOOGLE_API_KEY",)),  # dropped (no GOOGLE key)
    ]
    names = {e["name"] for e in av.filter_available(entries)}
    assert names == {"roll_dice", "duckduckgo_web_search", "openai_image"}


def test_mixed_catalog_image_enabled_does_not_leak_to_web(monkeypatch):
    # WEB_SEARCH_ENABLED unset (ddg floor) but IMAGE_ENABLED restricts image.
    monkeypatch.setattr(av, "_read_env", lambda: {
        "OPENAI_API_KEY": "k", "IMAGE_ENABLED": "gemini"})
    entries = [
        {"name": "duckduckgo_web_search",
         "x-availability": {"feature": "web_search", "provider": "duckduckgo",
                            "requires_env": []}},
        _image_entry("openai"),  # dropped: openai not in IMAGE_ENABLED
    ]
    names = {e["name"] for e in av.filter_available(entries)}
    assert names == {"duckduckgo_web_search"}


# --- back-compat: no feature key -> web_search ----------------------------

def test_entry_without_feature_key_resolves_as_web_search(monkeypatch):
    # No `feature` key on the gate -> defaults to web_search (shipped tools).
    monkeypatch.setattr(av, "_read_env", lambda: {"XAI_API_KEY": "k"})
    entry = {"name": "grok_web_search",
             "x-availability": {"provider": "grok", "requires_env": ["XAI_API_KEY"]}}
    assert av.is_available(entry) is True
    # And without the key, it's gated off via the web_search enabled set.
    monkeypatch.setattr(av, "_read_env", lambda: {})
    assert av.is_available(entry) is False
