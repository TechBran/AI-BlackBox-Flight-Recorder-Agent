"""Hermetic tests for the ElevenLabs voice-LIBRARY browse feature (Task 19).

Two layers, both fully mocked (no live ElevenLabs call ever):

1. Route layer (TestClient) -- monkeypatch catalog.get_shared_voices /
   add_shared_voice where the handlers look them up (imported inside the
   handler), proving the proxy passes args through, the missing-field guard
   returns 400, add success returns {ok, voice_id}, and a provider RuntimeError
   maps to HTTP 400 with its message.

2. Provider layer -- monkeypatch the HTTP choke points (catalog._get_json for
   the GET; requests.post for the add) to assert the normalization shape and
   that a successful add busts the voices cache.
"""
import pytest
from fastapi.testclient import TestClient

from Orchestrator.app import app
from Orchestrator.elevenlabs import catalog as cat
from Orchestrator.elevenlabs import client as el


@pytest.fixture
def cli():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_cache_and_key(monkeypatch):
    """Empty cache + present (fake) key for every test."""
    cat._cache.clear()
    monkeypatch.setattr(el, "resolve_api_key", lambda: "xi-fake")
    monkeypatch.setattr(el, "auth_headers", lambda key=None: {"xi-api-key": "xi-fake"})
    yield
    cat._cache.clear()


# --- canned shared-voices API response (real /v1/shared-voices field names) ---

_SHARED = {
    "voices": [
        {
            "public_owner_id": "owner-abc",
            "voice_id": "shared-123",
            "name": "Narrator Joe",
            "preview_url": "https://prev/joe.mp3",
            "accent": "american",
            "age": "middle_aged",
            "gender": "male",
            "description": "warm documentary narrator",
            "category": "professional",
            "language": "en",
            "free_users_allowed": True,
            "is_added_by_user": False,
        },
    ],
    "has_more": True,
    "last_sort_id": "xyz",
    "total_count": 42,
}


# =============================================================================
# Route layer
# =============================================================================

def test_library_get_returns_voices(cli, monkeypatch):
    """GET /elevenlabs/library proxies catalog.get_shared_voices and returns it,
    forwarding query params verbatim."""
    seen = {}

    def fake_get_shared(search=None, page_size=30, gender=None, category=None):
        seen.update(search=search, page_size=page_size, gender=gender, category=category)
        return {"voices": [{"name": "Narrator Joe", "voice_id": "shared-123"}], "has_more": True}

    monkeypatch.setattr(cat, "get_shared_voices", fake_get_shared)

    resp = cli.get("/elevenlabs/library", params={"search": "narrator", "page_size": 5, "gender": "male"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_more"] is True
    assert body["voices"][0]["name"] == "Narrator Joe"
    # params forwarded
    assert seen == {"search": "narrator", "page_size": 5, "gender": "male", "category": None}


def test_library_get_no_key_returns_empty(cli, monkeypatch):
    """No key -> provider returns None -> route returns a graceful empty result."""
    monkeypatch.setattr(cat, "get_shared_voices", lambda *a, **k: None)
    resp = cli.get("/elevenlabs/library")
    assert resp.status_code == 200
    assert resp.json() == {"voices": [], "has_more": False}


def test_library_add_missing_fields_returns_400(cli, monkeypatch):
    """Any missing field -> 400, and the provider add is never reached."""
    monkeypatch.setattr(
        cat, "add_shared_voice",
        lambda *a, **k: pytest.fail("add_shared_voice called despite missing fields"),
    )
    # missing name
    resp = cli.post("/elevenlabs/library/add", json={"public_owner_id": "o", "voice_id": "v"})
    assert resp.status_code == 400
    # missing voice_id
    resp = cli.post("/elevenlabs/library/add", json={"public_owner_id": "o", "name": "n"})
    assert resp.status_code == 400
    # empty body
    resp = cli.post("/elevenlabs/library/add", json={})
    assert resp.status_code == 400


def test_library_add_success(cli, monkeypatch):
    """All three fields present -> {ok: True, voice_id} with the new account id."""
    seen = {}

    def fake_add(public_owner_id, voice_id, name):
        seen.update(public_owner_id=public_owner_id, voice_id=voice_id, name=name)
        return {"voice_id": "new-account-id-999"}

    monkeypatch.setattr(cat, "add_shared_voice", fake_add)

    resp = cli.post(
        "/elevenlabs/library/add",
        json={"public_owner_id": "owner-abc", "voice_id": "shared-123", "name": "My Narrator"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "voice_id": "new-account-id-999"}
    assert seen == {"public_owner_id": "owner-abc", "voice_id": "shared-123", "name": "My Narrator"}


def test_library_add_runtime_error_maps_to_400(cli, monkeypatch):
    """Provider RuntimeError (mapped ElevenLabs error) -> HTTP 400 with message."""
    def boom(*a, **k):
        raise RuntimeError("ElevenLabs quota exceeded - add credits or upgrade plan")

    monkeypatch.setattr(cat, "add_shared_voice", boom)
    resp = cli.post(
        "/elevenlabs/library/add",
        json={"public_owner_id": "o", "voice_id": "v", "name": "n"},
    )
    assert resp.status_code == 400
    assert "quota exceeded" in resp.json()["detail"]


# =============================================================================
# Provider layer
# =============================================================================

def test_get_shared_voices_normalizes_shape(monkeypatch):
    """get_shared_voices flattens each raw entry to the library-card shape and
    forwards the query params to the HTTP layer."""
    captured = {}

    def fake_get_json(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return _SHARED

    monkeypatch.setattr(cat, "_get_json", fake_get_json)

    out = cat.get_shared_voices(search="narrator", page_size=7, gender="male", category="professional")
    assert captured["path"] == "/v1/shared-voices"
    assert captured["params"] == {
        "page_size": 7, "search": "narrator", "gender": "male", "category": "professional",
    }
    assert out["has_more"] is True
    v = out["voices"][0]
    # exactly the normalized keys, real values preserved
    assert v == {
        "public_owner_id": "owner-abc",
        "voice_id": "shared-123",
        "name": "Narrator Joe",
        "preview_url": "https://prev/joe.mp3",
        "accent": "american",
        "gender": "male",
        "age": "middle_aged",
        "description": "warm documentary narrator",
        "category": "professional",
        "language": "en",
    }


def test_get_shared_voices_no_key_returns_none(monkeypatch):
    """No key -> None without touching the network."""
    monkeypatch.setattr(el, "resolve_api_key", lambda: None)
    monkeypatch.setattr(el, "auth_headers", lambda key=None: (_ for _ in ()).throw(RuntimeError("no key")))
    monkeypatch.setattr(
        cat, "_get_json",
        lambda *a, **k: pytest.fail("_get_json called despite no key"),
    )
    assert cat.get_shared_voices(search="x") is None


def test_add_shared_voice_busts_cache(monkeypatch):
    """A successful add returns the new account voice_id AND busts the voices cache."""
    # seed a fake voices-cache entry so we can prove it gets cleared
    cat._cache["voices"] = (1.0, {"my_voices": [], "premade": []})
    busted = {"called": False}
    real_bust = cat.bust_voices_cache

    def spy_bust():
        busted["called"] = True
        real_bust()

    monkeypatch.setattr(cat, "bust_voices_cache", spy_bust)

    class _Resp:
        status_code = 200

        def json(self):
            return {"voice_id": "new-account-id-999"}

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json)
        return _Resp()

    monkeypatch.setattr(cat.requests, "post", fake_post)

    out = cat.add_shared_voice("owner-abc", "shared-123", "My Narrator")
    assert out == {"voice_id": "new-account-id-999"}
    assert busted["called"] is True
    assert "voices" not in cat._cache  # cache cleared
    # correct URL + body shape
    assert captured["url"].endswith("/v1/voices/add/owner-abc/shared-123")
    assert captured["json"] == {"new_name": "My Narrator"}


def test_add_shared_voice_error_raises_runtime(monkeypatch):
    """Non-2xx -> RuntimeError via client.map_error; cache is NOT busted."""
    cat._cache["voices"] = (1.0, {"my_voices": [], "premade": []})

    class _Resp:
        status_code = 401

        def json(self):
            return {"detail": {"status": "auth_error"}}

    monkeypatch.setattr(cat.requests, "post", lambda *a, **k: _Resp())

    with pytest.raises(RuntimeError) as exc:
        cat.add_shared_voice("o", "v", "n")
    assert "auth failed" in str(exc.value)
    # failed add must leave the cache intact
    assert "voices" in cat._cache


def test_add_shared_voice_no_key_returns_none(monkeypatch):
    """No key -> None (matches the GET's graceful-hide contract)."""
    monkeypatch.setattr(el, "resolve_api_key", lambda: None)
    monkeypatch.setattr(el, "auth_headers", lambda key=None: (_ for _ in ()).throw(RuntimeError("no key")))
    monkeypatch.setattr(
        cat.requests, "post",
        lambda *a, **k: pytest.fail("requests.post called despite no key"),
    )
    assert cat.add_shared_voice("o", "v", "n") is None
