"""M-E Task E1 — STT choke-point completeness.

The guarantee behind "switch the STT endpoint -> everything uses it": EVERY
batch/stream STT entry point must dispatch through
``Orchestrator.stt.resolve.resolve_stt_provider``. This parametrized test drives
each entry point with the network hop stubbed and asserts the resolver fired.

Also asserts the on-box work is INERT when the local stack is off: with the
on-box token unavailable, resolution falls back to the configured cloud provider
byte-for-byte (no silent behavior change).

Audit note — consumers deliberately NOT covered here (documented bypasses):
  * xAI Grok Live (``xai_phone/call_bridge.py``) and Gemini Live's native
    ``inputAudioTranscription``/``outputAudioTranscription``
    (``gemini_live_routes.py:590``) are PROVIDER-PINNED: the realtime API
    transcribes its own audio stream inline; there is no separable arbitrary-
    audio STT hop to re-point. (Gemini Live's *user*-audio buffer transcription
    DOES go through /stt/json -> transcribe_bytes -> the resolver, and is covered
    transitively by the stt_json case below.)
"""
import asyncio

import pytest

import Orchestrator.stt.resolve as resolve_mod
import Orchestrator.stt.file_transcribe as ft
import Orchestrator.routes.stt_ws_routes as ws
import Orchestrator.routes.tts_routes as tts
import Orchestrator.routes.twilio_routes as twilio


# --------------------------------------------------------------------------- #
# Spy: wraps the resolver so downstream still resolves a real provider name,
# while recording that the choke point was invoked.
# --------------------------------------------------------------------------- #
class _Spy:
    def __init__(self, ret):
        self.ret = ret
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self.ret


def _install_spy(monkeypatch, ret="openai"):
    """Patch resolve_stt_provider at EVERY reference site.

    - ``resolve_mod``: the source; picked up by tts_routes' lazy
      ``from Orchestrator.stt.resolve import resolve_stt_provider``.
    - ``ft``: module-level import used inside transcribe_bytes.
    - ``ws``: module-level import used by the /ws/stt handler.
    """
    spy = _Spy(ret)
    monkeypatch.setattr(resolve_mod, "resolve_stt_provider", spy)
    monkeypatch.setattr(ft, "resolve_stt_provider", spy)
    monkeypatch.setattr(ws, "resolve_stt_provider", spy)
    return spy


def _pcm16(n_bytes=400):
    return b"\x01\x00" * (n_bytes // 2)


class _FakeUpload:
    """Minimal FastAPI UploadFile stand-in for the /stt route."""
    def __init__(self, data, filename="audio.wav", content_type="audio/wav"):
        self._data = data
        self.filename = filename
        self.content_type = content_type

    async def read(self):
        return self._data


class _FakeWS:
    """Minimal WebSocket: enough for ws_stt to reach the resolver and return."""
    def __init__(self, start):
        self._start = start
        self.sent = []

    async def accept(self):
        pass

    async def receive_json(self):
        return self._start

    async def send_json(self, msg):
        self.sent.append(msg)


class _FakeCLISession:
    def __init__(self):
        self.transcription_queue = []


# --------------------------------------------------------------------------- #
# Each invoker drives one entry point end-to-end (network stubbed) and returns
# the spy so the test can assert calls > 0.
# --------------------------------------------------------------------------- #
def _run(coro):
    return asyncio.run(coro)


def invoke_transcribe_bytes(monkeypatch):
    spy = _install_spy(monkeypatch)
    monkeypatch.setattr(ft, "_openai_transcribe", lambda *a, **k: "ok")
    ft.transcribe_bytes(b"data", "audio/wav", filename="audio.wav")
    return spy


def invoke_ws_stt(monkeypatch):
    # Resolver returns None -> handler emits stt_error and returns before any
    # bridge; that is enough to prove the entry point dispatches through it.
    spy = _install_spy(monkeypatch, ret=None)
    fake = _FakeWS({"type": "stt_start", "provider": ""})
    _run(ws.ws_stt(fake))
    return spy


def invoke_stt_route(monkeypatch):
    spy = _install_spy(monkeypatch)
    monkeypatch.setattr(ft, "_openai_transcribe", lambda *a, **k: "ok")
    _run(tts.stt(file=_FakeUpload(_pcm16()), provider=None, diarize=False))
    return spy


def invoke_stt_json(monkeypatch):
    import base64
    spy = _install_spy(monkeypatch)
    monkeypatch.setattr(ft, "_openai_transcribe", lambda *a, **k: "ok")
    body = {"audio": base64.b64encode(_pcm16()).decode(), "sample_rate": 16000}
    _run(tts.stt_json(body=body))
    return spy


def invoke_stt_translate(monkeypatch):
    import base64
    import Orchestrator.stt.translate as tr
    spy = _install_spy(monkeypatch)
    monkeypatch.setattr(ft, "_openai_transcribe", lambda *a, **k: "ok")
    monkeypatch.setattr(tr, "translate_text", lambda text, lang: "hola")
    body = {"target_lang": "es", "audio": base64.b64encode(_pcm16()).decode(),
            "content_type": "audio/wav"}
    _run(tts.stt_translate(body=body))
    return spy


def invoke_stt_catalog(monkeypatch):
    spy = _install_spy(monkeypatch)
    _run(tts.stt_catalog())
    return spy


def invoke_twilio_transcribe_and_queue(monkeypatch):
    spy = _install_spy(monkeypatch)
    monkeypatch.setattr(ft, "_openai_transcribe", lambda *a, **k: "hello")
    sess = _FakeCLISession()
    _run(twilio.transcribe_and_queue(_pcm16(), sess))
    # And the resolved transcript actually lands in the queue.
    assert sess.transcription_queue and sess.transcription_queue[0]["text"] == "hello"
    return spy


ENTRY_POINTS = {
    "file_transcribe.transcribe_bytes": invoke_transcribe_bytes,
    "ws_stt (/ws/stt streaming)": invoke_ws_stt,
    "/stt route": invoke_stt_route,
    "/stt/json route": invoke_stt_json,
    "/stt/translate route": invoke_stt_translate,
    "/stt/catalog route": invoke_stt_catalog,
    "twilio telephony transcribe_and_queue": invoke_twilio_transcribe_and_queue,
}


@pytest.mark.parametrize("name", list(ENTRY_POINTS), ids=list(ENTRY_POINTS))
def test_entry_point_dispatches_through_resolver(monkeypatch, name):
    """Every batch/stream STT entry point funnels through resolve_stt_provider."""
    spy = ENTRY_POINTS[name](monkeypatch)
    assert spy.calls > 0, f"{name} did NOT dispatch through resolve_stt_provider"


def test_toolvault_speech_to_text_enum_offers_onbox_and_local():
    """The ToolVault speech_to_text tool exposes the on-box + local providers so
    an agent can pin them; omitting provider defers to the /stt resolver."""
    import json
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    schema = json.loads((root / "ToolVault" / "tools" / "speech_to_text" /
                         "schema.json").read_text())
    enum = schema["parameters"]["properties"]["provider"]["enum"]
    assert "onbox" in enum and "local" in enum
    assert set(enum) >= {"openai", "google", "elevenlabs", "local", "onbox"}


def test_inert_when_onbox_off_falls_back_to_cloud():
    """With the on-box stack OFF (onbox_ok=False) resolution is byte-for-byte the
    prior cloud behavior: the configured/first cloud provider, never a silent
    switch. Uses the pure-kwargs path so no .env/filesystem is touched."""
    # No explicit pick, only OpenAI available -> openai (unchanged tie-break).
    assert resolve_mod.resolve_stt_provider(
        None, openai_ok=True, google_ok=False, elevenlabs_ok=False,
        local_ok=False, onbox_ok=False) == "openai"
    # An 'onbox' pick while onbox is unavailable must NOT stick — it falls back
    # to the available cloud provider (fail-open, no silent on-box routing).
    assert resolve_mod.resolve_stt_provider(
        "onbox", openai_ok=True, google_ok=False, elevenlabs_ok=False,
        local_ok=False, onbox_ok=False) == "openai"
    # Nothing available -> None (loud "no STT provider configured" downstream).
    assert resolve_mod.resolve_stt_provider(
        None, openai_ok=False, google_ok=False, elevenlabs_ok=False,
        local_ok=False, onbox_ok=False) is None
