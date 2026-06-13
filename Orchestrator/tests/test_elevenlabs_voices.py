"""Hermetic tests for the ElevenLabs Voice Lab provider (Task 21).

Fully mocked -- no live ElevenLabs call ever, no real network. The HTTP layer is
monkeypatched at ``requests.post`` / ``requests.delete`` (the exact callables the
module uses), ``catalog.bust_voices_cache`` is spied to prove every mutator busts
the cache, and the uploads dir is redirected to ``tmp_path`` so preview decode
writes into the test sandbox.

Covers all five surfaces:
  clone_instant  -- multipart includes name + files + remove_background_noise;
                    returns voice_id/requires_verification; cache busted.
  design_previews -- auto_generate_text when text is None, text when provided;
                    base64 previews decoded to real files; shape returned.
  design_save    -- correct body; cache busted; returns voice_id.
  delete_voice   -- strips elevenlabs: prefix; right URL; cache busted.
  voice_in_use   -- finds the operator with a planted pref; [] when none.
  error path     -- a 4xx -> RuntimeError with the mapped message.
"""
import base64

import pytest

from Orchestrator.elevenlabs import catalog as cat
from Orchestrator.elevenlabs import client as el
from Orchestrator.elevenlabs import voices


# A 1-frame-ish silent MP3 is unnecessary; any bytes round-trip through base64.
_TINY_AUDIO = b"ID3fake-mp3-bytes\x00\x01\x02"
_TINY_B64 = base64.b64encode(_TINY_AUDIO).decode()


@pytest.fixture(autouse=True)
def _fresh_cache_and_key(monkeypatch):
    """Empty voices cache + a present (fake) key for every test."""
    cat._cache.clear()
    monkeypatch.setattr(el, "resolve_api_key", lambda: "xi-fake")
    monkeypatch.setattr(el, "auth_headers", lambda key=None: {"xi-api-key": "xi-fake"})
    yield
    cat._cache.clear()


@pytest.fixture
def spy_bust(monkeypatch):
    """Spy on catalog.bust_voices_cache; records whether it fired."""
    state = {"called": False}
    real = cat.bust_voices_cache

    def _spy():
        state["called"] = True
        real()

    monkeypatch.setattr(voices.catalog, "bust_voices_cache", _spy)
    return state


class _Resp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


# =============================================================================
# clone_instant
# =============================================================================

def test_clone_instant_multipart_and_result(monkeypatch, tmp_path, spy_bust):
    """IVC multipart carries name + files + remove_background_noise; returns the
    new voice_id/requires_verification; busts the cache."""
    cat._cache["voices"] = (1.0, {"my_voices": [], "premade": []})  # seed to prove clear

    sample = tmp_path / "sample.mp3"
    sample.write_bytes(_TINY_AUDIO)

    captured = {}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        captured.update(url=url, headers=headers, data=data, files=files, timeout=timeout)
        return _Resp(200, {"voice_id": "vc-new-123", "requires_verification": True})

    monkeypatch.setattr(voices.requests, "post", fake_post)

    out = voices.clone_instant(
        "BBX Test Clone",
        [str(sample)],
        description="a test clone",
        labels={"accent": "british"},
        remove_background_noise=True,
    )

    assert out == {"voice_id": "vc-new-123", "requires_verification": True}
    assert spy_bust["called"] is True
    assert "voices" not in cat._cache  # cache cleared

    assert captured["url"].endswith("/v1/voices/add")
    assert captured["data"]["name"] == "BBX Test Clone"
    assert captured["data"]["remove_background_noise"] == "true"
    assert captured["data"]["description"] == "a test clone"
    # dict labels are JSON-encoded for the multipart field
    assert captured["data"]["labels"] == '{"accent": "british"}'
    # exactly one ``files`` part, field name "files"
    assert len(captured["files"]) == 1
    assert captured["files"][0][0] == "files"


def test_clone_instant_remove_noise_false(monkeypatch, tmp_path):
    """remove_background_noise=False is sent as the string "false"."""
    sample = tmp_path / "s.mp3"
    sample.write_bytes(_TINY_AUDIO)

    captured = {}

    def fake_post(url, headers=None, data=None, files=None, timeout=None):
        captured.update(data=data)
        return _Resp(200, {"voice_id": "vc-1"})

    monkeypatch.setattr(voices.requests, "post", fake_post)
    voices.clone_instant("n", [str(sample)], remove_background_noise=False)
    assert captured["data"]["remove_background_noise"] == "false"
    # no description / labels keys when not provided
    assert "description" not in captured["data"]
    assert "labels" not in captured["data"]


def test_clone_instant_error_raises_runtime(monkeypatch, tmp_path):
    """A 4xx maps to RuntimeError; cache is NOT busted on failure."""
    cat._cache["voices"] = (1.0, {"my_voices": [], "premade": []})
    sample = tmp_path / "s.mp3"
    sample.write_bytes(_TINY_AUDIO)

    monkeypatch.setattr(
        voices.requests, "post",
        lambda *a, **k: _Resp(401, {"detail": {"status": "auth_error"}}),
    )
    with pytest.raises(RuntimeError) as exc:
        voices.clone_instant("n", [str(sample)])
    assert "auth failed" in str(exc.value)
    assert "voices" in cat._cache  # failed mutator leaves cache intact


# =============================================================================
# design_previews
# =============================================================================

def _patch_uploads_dir(monkeypatch, tmp_path):
    """Redirect UPLOADS_DIR (read lazily inside _decode_preview_audio) to tmp."""
    import Orchestrator.config as cfg
    monkeypatch.setattr(cfg, "UPLOADS_DIR", tmp_path)


def test_design_previews_auto_text_and_decode(monkeypatch, tmp_path):
    """text=None -> auto_generate_text True; previews decoded to real mp3 files;
    returned shape carries audio_path/audio_url + sample text."""
    _patch_uploads_dir(monkeypatch, tmp_path)
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, json=json)
        return _Resp(200, {
            "text": "The quick brown fox sample.",
            "previews": [
                {"generated_voice_id": "gv-1", "audio_base_64": _TINY_B64,
                 "media_type": "audio/mpeg", "duration_secs": 4.2, "language": "en"},
                {"generated_voice_id": "gv-2", "audio_base_64": _TINY_B64,
                 "media_type": "audio/mpeg", "duration_secs": 3.8, "language": "en"},
                {"generated_voice_id": "gv-3", "audio_base_64": _TINY_B64,
                 "media_type": "audio/mpeg", "duration_secs": 5.0, "language": "en"},
            ],
        })

    monkeypatch.setattr(voices.requests, "post", fake_post)

    out = voices.design_previews("a gravelly old sea captain")

    assert captured["url"].endswith("/v1/text-to-voice/design")
    assert captured["json"]["voice_description"] == "a gravelly old sea captain"
    assert captured["json"]["auto_generate_text"] is True
    assert captured["json"]["model_id"] == "eleven_ttv_v3"
    assert "text" not in captured["json"]  # no explicit text when auto

    assert out["text"] == "The quick brown fox sample."
    assert len(out["previews"]) == 3
    p0 = out["previews"][0]
    assert p0["generated_voice_id"] == "gv-1"
    assert p0["duration_secs"] == 4.2
    assert p0["language"] == "en"
    assert p0["audio_url"].startswith("/ui/uploads/")
    assert p0["audio_url"].endswith(".mp3")
    # the file was actually written and contains the decoded bytes
    written = tmp_path / p0["audio_url"].split("/ui/uploads/")[-1]
    assert written.exists()
    assert written.read_bytes() == _TINY_AUDIO
    # unique filenames per preview
    urls = {p["audio_url"] for p in out["previews"]}
    assert len(urls) == 3


def test_design_previews_explicit_text(monkeypatch, tmp_path):
    """When text is provided, it is sent and auto_generate_text is NOT."""
    _patch_uploads_dir(monkeypatch, tmp_path)
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json=json)
        return _Resp(200, {"text": "x", "previews": []})

    monkeypatch.setattr(voices.requests, "post", fake_post)

    voices.design_previews("desc", text="a" * 120, model_id="eleven_ttv_v3")
    assert captured["json"]["text"] == "a" * 120
    assert "auto_generate_text" not in captured["json"]


def test_design_previews_error_raises_runtime(monkeypatch, tmp_path):
    """A 4xx maps to RuntimeError."""
    _patch_uploads_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        voices.requests, "post",
        lambda *a, **k: _Resp(422, {"detail": "missing text or auto_generate_text"}),
    )
    with pytest.raises(RuntimeError) as exc:
        voices.design_previews("desc")
    assert "422" in str(exc.value) or "error" in str(exc.value).lower()


# =============================================================================
# design_save
# =============================================================================

def test_design_save_body_and_bust(monkeypatch, spy_bust):
    """design_save posts the right body, busts the cache, returns voice_id."""
    cat._cache["voices"] = (1.0, {"my_voices": [], "premade": []})
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, json=json)
        return _Resp(200, {"voice_id": "vc-designed-77"})

    monkeypatch.setattr(voices.requests, "post", fake_post)

    out = voices.design_save("gv-1", "BBX Designed", "weathered captain")
    assert out == {"voice_id": "vc-designed-77"}
    assert spy_bust["called"] is True
    assert "voices" not in cat._cache

    assert captured["url"].endswith("/v1/text-to-voice")
    assert captured["json"] == {
        "generated_voice_id": "gv-1",
        "voice_name": "BBX Designed",
        "voice_description": "weathered captain",
    }


def test_design_save_error_raises_runtime(monkeypatch):
    """A 4xx maps to RuntimeError; cache untouched."""
    cat._cache["voices"] = (1.0, {"my_voices": [], "premade": []})
    monkeypatch.setattr(
        voices.requests, "post",
        lambda *a, **k: _Resp(400, {"detail": {"status": "quota_exceeded"}}),
    )
    with pytest.raises(RuntimeError) as exc:
        voices.design_save("gv-1", "n", "d")
    assert "quota exceeded" in str(exc.value)
    assert "voices" in cat._cache


# =============================================================================
# delete_voice
# =============================================================================

def test_delete_voice_strips_prefix_and_busts(monkeypatch, spy_bust):
    """delete_voice strips the elevenlabs: prefix, DELETEs the raw id, busts cache."""
    cat._cache["voices"] = (1.0, {"my_voices": [], "premade": []})
    captured = {}

    def fake_delete(url, headers=None, timeout=None):
        captured.update(url=url)
        return _Resp(200, {})

    monkeypatch.setattr(voices.requests, "delete", fake_delete)

    out = voices.delete_voice("elevenlabs:vc-abc-123")
    assert out == {"ok": True}
    assert spy_bust["called"] is True
    assert "voices" not in cat._cache
    # prefix stripped -> raw id in URL
    assert captured["url"].endswith("/v1/voices/vc-abc-123")
    assert "elevenlabs:" not in captured["url"]


def test_delete_voice_without_prefix(monkeypatch):
    """A raw id (no prefix) is deleted unchanged."""
    captured = {}
    monkeypatch.setattr(
        voices.requests, "delete",
        lambda url, headers=None, timeout=None: captured.update(url=url) or _Resp(200, {}),
    )
    voices.delete_voice("vc-raw-999")
    assert captured["url"].endswith("/v1/voices/vc-raw-999")


def test_delete_voice_error_raises_runtime(monkeypatch):
    """A 4xx maps to RuntimeError; cache untouched."""
    cat._cache["voices"] = (1.0, {"my_voices": [], "premade": []})
    monkeypatch.setattr(
        voices.requests, "delete",
        lambda *a, **k: _Resp(401, {"detail": {"status": "auth_error"}}),
    )
    with pytest.raises(RuntimeError) as exc:
        voices.delete_voice("vc-1")
    assert "auth failed" in str(exc.value)
    assert "voices" in cat._cache


# =============================================================================
# voice_in_use
# =============================================================================

def test_voice_in_use_finds_operators(monkeypatch):
    """Operators whose tts_voice == elevenlabs:<id> are returned."""
    import Orchestrator.state as state
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", {
        "Brandon": {"tts_voice": "elevenlabs:vc-target", "model": "opus"},
        "Brandon-DEV": {"tts_voice": "gemini-pro:Charon"},
        "system": {"tts_voice": "elevenlabs:vc-target"},
        "no-voice": {"model": "sonnet"},
    })
    # accepts either prefixed or raw id, same result
    assert sorted(voices.voice_in_use("vc-target")) == ["Brandon", "system"]
    assert sorted(voices.voice_in_use("elevenlabs:vc-target")) == ["Brandon", "system"]


def test_voice_in_use_none(monkeypatch):
    """No operator uses the voice -> empty list."""
    import Orchestrator.state as state
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", {
        "Brandon": {"tts_voice": "elevenlabs:other"},
    })
    assert voices.voice_in_use("vc-target") == []


def test_voice_in_use_fail_open(monkeypatch):
    """An unreadable store fails open to [] -- never raises."""
    import Orchestrator.state as state
    # a non-dict prefs value must not blow up the scan
    monkeypatch.setattr(state, "OPERATOR_PREFERENCES", {"weird": "not-a-dict"})
    assert voices.voice_in_use("vc-target") == []
