"""Gemini TTS synthesises the model the caller named, or refuses it.

Measured defect (audit O07-tts-voice-06): call_gemini_tts_cloud kept a literal
valid_models list and swapped any other id for gemini-2.5-pro-tts, and
call_gemini_tts_preview did the same through model_mapping.get(..., pro-preview).
An unknown or mistyped id (or an OpenAI model sent with a Gemini voice) was
synthesised and billed as Pro with no error. Both paths now resolve the model
through one helper over config.GEMINI_TTS_MODELS: empty means the Pro default,
a known GA or AI Studio preview id is used as named, anything else is a 400.

Hermetic: requests and the Cloud TTS token are replaced; no network.
"""
import base64

import pytest
from fastapi import HTTPException

from Orchestrator.config import GEMINI_TTS_MODELS, GEMINI_TTS_PREVIEW_IDS

FLASH = GEMINI_TTS_MODELS["gemini-flash"]
PRO = GEMINI_TTS_MODELS["gemini-pro"]


@pytest.fixture
def tr(monkeypatch):
    import Orchestrator.routes.tts_routes as tr
    monkeypatch.setattr(tr, "GOOGLE_API_KEY", "test-google-key", raising=False)

    def _no_network(url, *a, **k):
        raise AssertionError(f"unexpected provider call to {url}")

    monkeypatch.setattr(tr.requests, "post", _no_network)
    return tr


def _inp(model, voice="Kore"):
    from Orchestrator.startup import GeminiProTTSIn
    return GeminiProTTSIn(text="hello", voice_name=voice, model=model)


class _Json:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _ok_preview(seen):
    pcm = base64.b64encode(b"\x00\x00" * 8).decode()

    def fake_post(url, **kw):
        seen.append(url)
        return _Json({"candidates": [{"content": {"parts": [
            {"inlineData": {"data": pcm, "mimeType": "audio/L16;rate=24000"}}]}}]})
    return fake_post


# ── resolve_gemini_tts_model ─────────────────────────────────────────────────
def test_every_group_model_has_an_ai_studio_preview_id():
    assert set(GEMINI_TTS_PREVIEW_IDS) == set(GEMINI_TTS_MODELS.values())


@pytest.mark.parametrize("requested,expected", [
    (None, PRO), ("", PRO), ("  ", PRO),
    (FLASH, FLASH), (PRO, PRO),
    (GEMINI_TTS_PREVIEW_IDS[FLASH], FLASH),   # an AI Studio id names the same model
])
def test_known_or_empty_models_resolve(tr, requested, expected):
    assert tr.resolve_gemini_tts_model(requested) == expected


@pytest.mark.parametrize("bogus", ["tts-1-hd", "gemini-2.5-pro-tts-typo", "gemini-9-tts"])
def test_an_unknown_model_is_a_400_naming_the_known_ids(tr, bogus):
    with pytest.raises(HTTPException) as exc:
        tr.resolve_gemini_tts_model(bogus)
    assert exc.value.status_code == 400
    assert bogus in exc.value.detail
    assert FLASH in exc.value.detail and PRO in exc.value.detail


# ── AI Studio (API key) path ─────────────────────────────────────────────────
def test_preview_path_refuses_an_unknown_model_instead_of_billing_pro(tr):
    with pytest.raises(HTTPException) as exc:
        tr.call_gemini_tts_preview(_inp("gemini-9-tts"))
    assert exc.value.status_code == 400


@pytest.mark.parametrize("requested,preview", [
    (FLASH, GEMINI_TTS_PREVIEW_IDS[FLASH]),
    (GEMINI_TTS_PREVIEW_IDS[FLASH], GEMINI_TTS_PREVIEW_IDS[FLASH]),
    ("", GEMINI_TTS_PREVIEW_IDS[PRO]),
])
def test_preview_path_calls_the_named_model(tr, monkeypatch, requested, preview):
    seen = []
    monkeypatch.setattr(tr.requests, "post", _ok_preview(seen))
    assert tr.call_gemini_tts_preview(_inp(requested))[:4] == b"RIFF"
    assert seen == [f"{tr.GEMINI_BASE_URL}/{preview}:generateContent"]


# ── Cloud TTS (service account) path ─────────────────────────────────────────
def test_cloud_path_refuses_an_unknown_model_before_fetching_a_token(tr, monkeypatch):
    def _no_token():
        raise AssertionError("no token may be fetched for a request that is refused")

    monkeypatch.setattr(tr, "_get_cloud_tts_access_token", _no_token)
    with pytest.raises(HTTPException) as exc:
        tr.call_gemini_tts_cloud(_inp("gemini-9-tts"))
    assert exc.value.status_code == 400


def test_cloud_path_sends_the_named_model(tr, monkeypatch):
    sent = {}
    monkeypatch.setattr(tr, "_get_cloud_tts_access_token", lambda: "tok")

    def fake_post(url, json=None, **kw):
        sent.update(json)
        return _Json({"audioContent": base64.b64encode(b"\x00\x00" * 8).decode()})

    monkeypatch.setattr(tr.requests, "post", fake_post)
    assert tr.call_gemini_tts_cloud(_inp(FLASH))[:4] == b"RIFF"
    assert sent["voice"]["modelName"] == FLASH
