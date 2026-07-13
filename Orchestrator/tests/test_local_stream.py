"""Local realtime streaming STT bridge: resample + WS-to-WS relay."""
import asyncio
import base64
import json

from Orchestrator.routes import stt_ws_routes as ws
from Orchestrator.onboarding import custom_servers


def test_resample_pcm16_passthrough():
    pcm = b"\x01\x00" * 100
    assert ws._resample_pcm16(pcm, 24000, 24000) == pcm


def test_resample_pcm16_16k_to_24k():
    pcm = b"\x01\x00" * 160
    out = ws._resample_pcm16(pcm, 16000, 24000)
    assert len(out) // 2 == round(160 * 24000 / 16000)  # 240 samples


class _FakeLocalWS:
    """Fake Speaches /v1/realtime: on commit, emit one completed transcript."""
    def __init__(self, transcript):
        self._t = transcript
        self.sent = []
        self._q = asyncio.Queue()

    async def send(self, data):
        d = json.loads(data)
        self.sent.append(d)
        if d.get("type") == "input_audio_buffer.commit":
            await self._q.put(json.dumps({
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": self._t}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._q.get()

    async def close(self):
        pass


class _FakeClientWS:
    def __init__(self):
        self.sent = []
        self._msgs = [
            {"type": "stt_audio", "pcm": base64.b64encode(b"\x00\x00" * 240).decode()},
            {"type": "stt_stop"},
        ]

    async def receive_json(self):
        if self._msgs:
            return self._msgs.pop(0)
        await asyncio.sleep(3600)

    async def send_json(self, m):
        self.sent.append(m)


def test_local_bridge_relays_final(monkeypatch):
    monkeypatch.setattr(custom_servers, "resolve_audio",
                        lambda kind: ({"base_url": "http://h/v1", "api_key": "k"}, "whisper-turbo"))
    fake_local = _FakeLocalWS("Hello world")

    async def fake_connect(url, **kw):
        assert url == "ws://h/v1/realtime?model=whisper-turbo&intent=transcription"
        assert kw["additional_headers"]["Authorization"] == "Bearer k"
        return fake_local

    monkeypatch.setattr(ws.websockets, "connect", fake_connect)
    client = _FakeClientWS()
    asyncio.run(ws._local_bridge(client, target="prompt", lang="en", sample_rate=24000))
    finals = [m for m in client.sent if m.get("type") == "stt_final"]
    assert finals and finals[0]["text"] == "Hello world" and finals[0]["target"] == "prompt"
    # audio was appended (one chunk) + committed on stop
    types = [d["type"] for d in fake_local.sent]
    assert "input_audio_buffer.append" in types and "input_audio_buffer.commit" in types


def test_local_bridge_no_server(monkeypatch):
    monkeypatch.setattr(custom_servers, "resolve_audio", lambda kind: None)
    client = _FakeClientWS()
    asyncio.run(ws._local_bridge(client, target="prompt", lang="en", sample_rate=24000))
    assert any(m.get("type") == "stt_error" for m in client.sent)
