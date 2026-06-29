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


def test_synthesize_stream_raises_runtimeerror_on_terminal_failure(monkeypatch):
    seq = [_FakeResp(403, chunks=()), _FakeResp(403, chunks=())]  # downgrade still fails
    monkeypatch.setattr(el_tts.requests, "post", lambda url, **kw: seq.pop(0))
    monkeypatch.setattr(el_tts.client, "map_error", lambda code, body: "mapped-error")
    with pytest.raises(RuntimeError):
        list(el_tts.synthesize_stream("hi", "v", output_format="mp3_44100_192"))
    assert seq == []  # exactly original + one downgrade, no infinite retry


def test_synthesize_stream_no_downgrade_when_already_fallback(monkeypatch):
    posts = []
    def fake_post(url, **kw):
        posts.append(1)
        return _FakeResp(403, chunks=())
    monkeypatch.setattr(el_tts.requests, "post", fake_post)
    monkeypatch.setattr(el_tts.client, "map_error", lambda code, body: "e")
    with pytest.raises(RuntimeError):
        list(el_tts.synthesize_stream("hi", "v", output_format="mp3_44100_128"))  # already fallback
    assert len(posts) == 1  # no second POST when already at fallback format


def test_synthesize_stream_closes_response(monkeypatch):
    closed = {"n": 0}
    class _CountResp(_FakeResp):
        def close(self):
            closed["n"] += 1
    monkeypatch.setattr(el_tts.requests, "post", lambda url, **kw: _CountResp(200, chunks=(b"A",)))
    list(el_tts.synthesize_stream("hi", "v"))
    assert closed["n"] >= 1  # response closed after iteration


def test_synthesize_stream_wraps_midstream_error_as_runtimeerror(monkeypatch):
    class _StallResp(_FakeResp):
        def iter_content(self, chunk_size=4096):
            yield b"AB"
            raise el_tts.requests.exceptions.ReadTimeout("stalled")
    monkeypatch.setattr(el_tts.requests, "post", lambda url, **kw: _StallResp(200))
    with pytest.raises(RuntimeError):
        list(el_tts.synthesize_stream("hi", "v"))
