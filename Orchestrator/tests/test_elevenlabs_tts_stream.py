import types
import pytest
from Orchestrator.elevenlabs import tts as el_tts


class _FakeResp:
    def __init__(self, status_code=200, chunks=(b"AB", b"CD"), body=None):
        self.status_code = status_code
        self._chunks = chunks
        self._body = body or {}
    def iter_content(self, chunk_size=4096):
        for c in self._chunks:
            yield c
    def json(self):
        return self._body
    def close(self):
        pass


def test_synthesize_stream_yields_chunks(monkeypatch):
    calls = {}
    def fake_post(url, **kw):
        calls["url"] = url
        calls["stream"] = kw.get("stream")
        return _FakeResp(200, chunks=(b"AB", b"CD"))
    monkeypatch.setattr(el_tts.requests, "post", fake_post)
    out = b"".join(el_tts.synthesize_stream("hello", "elevenlabs:voice123", model_id="eleven_v3"))
    assert out == b"ABCD"
    assert calls["url"].endswith("/v1/text-to-speech/voice123/stream")  # raw id + /stream
    assert calls["stream"] is True


def test_synthesize_stream_downgrades_format_once_on_gate(monkeypatch):
    seq = [_FakeResp(403, chunks=()), _FakeResp(200, chunks=(b"XY",))]
    def fake_post(url, **kw): return seq.pop(0)
    monkeypatch.setattr(el_tts.requests, "post", fake_post)
    out = b"".join(el_tts.synthesize_stream("hi", "voice123", output_format="mp3_44100_192"))
    assert out == b"XY"
    assert seq == []  # exactly two posts (original + one downgrade)
