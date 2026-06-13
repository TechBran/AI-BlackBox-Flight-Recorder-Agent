"""Hermetic tests for the ElevenLabs SoT catalog layer.

The ONE network choke point is catalog._get_json -- every test monkeypatches it
so no live call is ever made. A call-counter proves the TTL cache is real
(not a mock-of-mock): cached fetches must NOT re-enter _get_json.
"""
import pytest
from Orchestrator.elevenlabs import catalog as cat
from Orchestrator.elevenlabs import client as el


@pytest.fixture(autouse=True)
def _fresh_cache_and_key(monkeypatch):
    """Each test starts with an empty cache and a present (fake) key."""
    cat._cache.clear()
    monkeypatch.setattr(el, "resolve_api_key", lambda: "xi-fake")
    monkeypatch.setattr(el, "auth_headers", lambda key=None: {"xi-api-key": "xi-fake"})
    yield
    cat._cache.clear()


# --- canned API responses (built to the real ElevenLabs field names) ---------

_VOICES_PAGE_1 = {
    "voices": [
        {  # the user's OWN cloned voice -> my_voices
            "voice_id": "own123",
            "name": "My Clone",
            "category": "cloned",
            "is_owner": True,
            "preview_url": "https://prev/own.mp3",
            "labels": {"accent": "american", "gender": "female", "age": "young"},
            "description": "personal clone",
        },
        {  # a shared LIBRARY professional voice the user does NOT own -> premade
            "voice_id": "lib456",
            "name": "Library Pro",
            "category": "professional",
            "is_owner": False,
            "preview_url": "https://prev/lib.mp3",
            "labels": {"accent": "british", "gender": "male", "age": "old"},
            "description": "shared library voice",
        },
    ],
    "has_more": True,
    "total_count": 3,
    "next_page_token": "PAGE2TOKEN",
}

_VOICES_PAGE_2 = {
    "voices": [
        {  # a stock premade voice
            "voice_id": "pre789",
            "name": "Rachel",
            "category": "premade",
            "is_owner": False,
            "preview_url": "https://prev/rachel.mp3",
            "labels": {"gender": "female"},  # only one label present
            "description": "",
        },
    ],
    "has_more": False,
    "total_count": 3,
    "next_page_token": None,
}

_USER = {
    "subscription": {
        "tier": "starter",
        "character_count": 1500,
        "character_limit": 90000,
        "voice_limit": 10,
        "can_use_instant_voice_cloning": True,
        "can_use_professional_voice_cloning": False,
    },
    "xi_api_key": "should-not-leak-but-passed-through-in-raw",
}

_MODELS = [
    {"model_id": "eleven_multilingual_v2", "name": "Multilingual v2",
     "can_do_text_to_speech": True, "maximum_text_length_per_request": 10000},
    {"model_id": "eleven_flash_v2_5", "name": "Flash v2.5",
     "can_do_text_to_speech": True, "maximum_text_length_per_request": 40000},
]


def _voices_router():
    """Return a fake _get_json that serves page 1 then page 2 by token, counting calls."""
    calls = {"n": 0}

    def fake(path, params=None):
        calls["n"] += 1
        params = params or {}
        if params.get("next_page_token") == "PAGE2TOKEN":
            return _VOICES_PAGE_2
        return _VOICES_PAGE_1

    return fake, calls


# --- pagination + grouping ----------------------------------------------------

def test_get_voices_paginates_and_groups_by_is_owner(monkeypatch):
    fake, calls = _voices_router()
    monkeypatch.setattr(cat, "_get_json", fake)

    result = cat.get_voices()

    # paginated across both pages -> two HTTP calls
    assert calls["n"] == 2

    my_ids = {v["id"] for v in result["my_voices"]}
    premade_ids = {v["id"] for v in result["premade"]}

    # owned cloned voice -> my_voices
    assert "elevenlabs:own123" in my_ids
    # professional LIBRARY voice (is_owner=False) lands in PREMADE, proving is_owner rule
    assert "elevenlabs:lib456" in premade_ids
    assert "elevenlabs:lib456" not in my_ids
    # stock premade -> premade
    assert "elevenlabs:pre789" in premade_ids

    # all three accounted for, none dropped/duplicated
    assert len(result["my_voices"]) == 1
    assert len(result["premade"]) == 2


def test_normalized_voice_shape(monkeypatch):
    fake, _ = _voices_router()
    monkeypatch.setattr(cat, "_get_json", fake)

    result = cat.get_voices()
    own = next(v for v in result["my_voices"] if v["id"] == "elevenlabs:own123")

    # id prefixed, additive fields present, matches catalog voice shape + extras
    assert own["id"] == "elevenlabs:own123"
    assert own["name"] == "My Clone"
    assert own["preview_url"] == "https://prev/own.mp3"
    assert own["category"] == "cloned"
    # description built from labels (accent, gender, age) joined ", "
    assert own["description"] == "american, female, young"

    # voice with a single label -> description has just that value
    rachel = next(v for v in result["premade"] if v["id"] == "elevenlabs:pre789")
    assert rachel["description"] == "female"


def test_description_falls_back_to_own_description_when_no_labels(monkeypatch):
    page = {
        "voices": [{
            "voice_id": "nolabels",
            "name": "Bare",
            "category": "premade",
            "is_owner": False,
            "preview_url": "https://prev/bare.mp3",
            "labels": {},
            "description": "fallback text",
        }],
        "has_more": False,
        "next_page_token": None,
    }
    monkeypatch.setattr(cat, "_get_json", lambda path, params=None: page)
    result = cat.get_voices()
    v = result["premade"][0]
    assert v["description"] == "fallback text"


# --- caching proof ------------------------------------------------------------

def test_voices_cache_avoids_second_fetch(monkeypatch):
    fake, calls = _voices_router()
    monkeypatch.setattr(cat, "_get_json", fake)

    cat.get_voices()
    first = calls["n"]
    cat.get_voices()  # served from cache -> no new HTTP calls
    assert calls["n"] == first  # unchanged == cache hit


def test_bust_voices_cache_forces_refetch(monkeypatch):
    fake, calls = _voices_router()
    monkeypatch.setattr(cat, "_get_json", fake)

    cat.get_voices()
    n_after_first = calls["n"]
    cat.bust_voices_cache()
    cat.get_voices()  # cache cleared -> must hit HTTP again
    assert calls["n"] > n_after_first


def test_force_bypasses_cache(monkeypatch):
    fake, calls = _voices_router()
    monkeypatch.setattr(cat, "_get_json", fake)

    cat.get_voices()
    n_after_first = calls["n"]
    cat.get_voices(force=True)
    assert calls["n"] > n_after_first


def test_bust_voices_only_clears_voices(monkeypatch):
    calls = {"models": 0}

    def fake(path, params=None):
        if path == "/v1/models":
            calls["models"] += 1
            return _MODELS
        return _VOICES_PAGE_2  # single-page voices

    monkeypatch.setattr(cat, "_get_json", fake)
    cat.get_models()
    cat.get_voices()
    cat.bust_voices_cache()
    cat.get_models()  # models cache must survive a voices bust
    assert calls["models"] == 1  # models fetched once, not re-fetched


# --- user normalization -------------------------------------------------------

def test_get_user_maps_plan_and_capabilities(monkeypatch):
    monkeypatch.setattr(cat, "_get_json", lambda path, params=None: _USER)
    user = cat.get_user()

    assert user["tier"] == "starter"
    assert user["credits_remaining"] == 90000 - 1500
    assert user["credits_limit"] == 90000
    # explicit capability booleans straight from the API (not inferred from tier)
    assert user["can_use_instant_voice_cloning"] is True
    assert user["can_use_professional_voice_cloning"] is False
    assert user["raw"] == _USER


# --- models passthrough -------------------------------------------------------

def test_get_models_returns_raw_list(monkeypatch):
    monkeypatch.setattr(cat, "_get_json", lambda path, params=None: _MODELS)
    models = cat.get_models()
    assert models == _MODELS
    assert isinstance(models, list)


# --- no-key path: graceful None, no exception ---------------------------------

def test_no_key_returns_none_for_all_fetchers(monkeypatch):
    # simulate "not configured": resolve_api_key -> None, auth_headers raises
    monkeypatch.setattr(el, "resolve_api_key", lambda: None)

    def _raise(key=None):
        raise RuntimeError("No ElevenLabs API key configured")

    monkeypatch.setattr(el, "auth_headers", _raise)

    # _get_json must never even be reached; if it is, blow up loudly
    def _boom(path, params=None):
        raise AssertionError("network must not be touched when no key configured")

    monkeypatch.setattr(cat, "_get_json", _boom)

    assert cat.get_models() is None
    assert cat.get_voices() is None
    assert cat.get_user() is None
