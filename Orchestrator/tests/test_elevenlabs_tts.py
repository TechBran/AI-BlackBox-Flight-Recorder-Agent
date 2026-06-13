"""Hermetic tests for ElevenLabs TTS synthesis (quality-first defaults).

No network, no live key: every test monkeypatches ``requests.post`` (captured
into a recorder so we can assert the exact URL/params/body the API would have
received) and ``catalog.get_models``. Auth headers are stubbed so no key lookup
hits ``.env``.
"""
import pytest

from Orchestrator import config
from Orchestrator.elevenlabs import client as el_client
from Orchestrator.elevenlabs import catalog as el_catalog
from Orchestrator.elevenlabs import tts


class _FakeResp:
    """Minimal stand-in for requests.Response: status + bytes + JSON body."""
    def __init__(self, status_code, content=b"", json_body=None):
        self.status_code = status_code
        self.content = content
        self._json = json_body

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


@pytest.fixture(autouse=True)
def _stub_auth(monkeypatch):
    """Never touch a real key; auth_headers returns a fixed fake header."""
    monkeypatch.setattr(el_client, "auth_headers", lambda key=None: {"xi-api-key": "xi-fake"})
    yield


def _record_post(monkeypatch, responses):
    """Patch tts.requests.post to pop canned responses; return the call recorder.

    ``responses`` is a list of _FakeResp consumed in order, one per POST.
    """
    calls = []

    def fake_post(url, headers=None, params=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "params": params, "json": json, "timeout": timeout})
        return responses[len(calls) - 1]

    monkeypatch.setattr(tts.requests, "post", fake_post)
    return calls


def test_defaults_are_quality_first(monkeypatch):
    """No explicit model/format -> eleven_v3 @ mp3_44100_192 on the wire."""
    calls = _record_post(monkeypatch, [_FakeResp(200, content=b"AUDIO")])

    out = tts.synthesize("Hello world", "voiceX")

    assert out == b"AUDIO"
    assert len(calls) == 1
    assert calls[0]["params"]["output_format"] == "mp3_44100_192"
    assert calls[0]["json"]["model_id"] == "eleven_v3"
    assert calls[0]["json"]["text"] == "Hello world"
    # config consts back these defaults (env-overridable).
    assert config.ELEVENLABS_TTS_MODEL_DEFAULT == "eleven_v3"
    assert config.ELEVENLABS_TTS_FORMAT_DEFAULT == "mp3_44100_192"


def test_voice_settings_only_sent_when_provided(monkeypatch):
    """No voice_settings -> absent from body; provided -> passed through."""
    calls = _record_post(monkeypatch, [_FakeResp(200, content=b"A"), _FakeResp(200, content=b"B")])

    tts.synthesize("hi", "v1")
    assert "voice_settings" not in calls[0]["json"]

    tts.synthesize("hi", "v1", voice_settings={"stability": 0.5})
    assert calls[1]["json"]["voice_settings"] == {"stability": 0.5}


def test_prefix_stripped_exactly_once(monkeypatch):
    """'elevenlabs:abc123' -> URL path contains 'abc123', not the prefix."""
    calls = _record_post(monkeypatch, [_FakeResp(200, content=b"A")])

    tts.synthesize("hi", "elevenlabs:abc123")

    url = calls[0]["url"]
    assert url.endswith("/v1/text-to-speech/abc123")
    assert "elevenlabs:abc123" not in url
    assert "elevenlabs:" not in url


def test_format_downgrade_retries_at_128_with_visible_notice(monkeypatch, capsys):
    """400 at 192 -> retry once at mp3_44100_128 (200) -> bytes + printed notice."""
    calls = _record_post(monkeypatch, [
        _FakeResp(400, json_body={"detail": "format not allowed on this plan"}),
        _FakeResp(200, content=b"DOWNGRADED"),
    ])

    out = tts.synthesize("hi", "v1")

    assert out == b"DOWNGRADED"
    assert len(calls) == 2
    assert calls[0]["params"]["output_format"] == "mp3_44100_192"
    assert calls[1]["params"]["output_format"] == "mp3_44100_128"
    notice = capsys.readouterr().out
    assert "downgraded to mp3_44100_128" in notice


def test_non_format_error_at_128_raises_mapped_message(monkeypatch):
    """401 while already at mp3_44100_128 -> no retry, RuntimeError(auth message)."""
    calls = _record_post(monkeypatch, [
        _FakeResp(401, json_body={"detail": {"status": "auth_error"}}),
    ])

    with pytest.raises(RuntimeError) as ei:
        tts.synthesize("hi", "v1", output_format="mp3_44100_128")

    assert len(calls) == 1  # NOT retried (already at fallback format)
    assert "auth" in str(ei.value).lower()


def test_still_failing_after_downgrade_raises(monkeypatch):
    """400 at 192 then 422 at 128 -> raises mapped error from the second body."""
    _record_post(monkeypatch, [
        _FakeResp(400, json_body={"detail": "nope"}),
        _FakeResp(422, json_body={"detail": {"status": "quota_exceeded"}}),
    ])

    with pytest.raises(RuntimeError) as ei:
        tts.synthesize("hi", "v1")

    assert "quota" in str(ei.value).lower()


def test_max_chars_for_reads_live_catalog(monkeypatch):
    """maximum_text_length_per_request comes from get_models for the matched id."""
    monkeypatch.setattr(el_catalog, "get_models", lambda *a, **k: [
        {"model_id": "eleven_v3", "maximum_text_length_per_request": 3000},
        {"model_id": "eleven_multilingual_v2", "maximum_text_length_per_request": 10000},
    ])
    assert tts.max_chars_for("eleven_v3") == 3000
    assert tts.max_chars_for("eleven_multilingual_v2") == 10000


def test_max_chars_for_unknown_model_falls_back_to_5000(monkeypatch):
    """Model absent from the live list -> conservative 5000 fallback."""
    monkeypatch.setattr(el_catalog, "get_models", lambda *a, **k: [
        {"model_id": "eleven_v3", "maximum_text_length_per_request": 3000},
    ])
    assert tts.max_chars_for("nonexistent_model") == 5000


def test_max_chars_for_no_catalog_falls_back_to_5000(monkeypatch):
    """get_models None (no key) -> 5000 fallback, no crash."""
    monkeypatch.setattr(el_catalog, "get_models", lambda *a, **k: None)
    assert tts.max_chars_for("eleven_v3") == 5000
