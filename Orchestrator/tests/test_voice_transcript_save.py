"""P1b: voice routes persist transcripts via /chat/save and clear ONLY on success."""
import asyncio

import Orchestrator.routes.realtime_routes as rt
from Orchestrator.models import RealtimeSession


def _session(cls, sid):
    s = cls(session_id=sid, operator="system")
    s.conversation = [
        {"role": "user", "content": "hi there", "timestamp": "2026-07-11T00:00:00Z"},
        {"role": "assistant", "content": "hello", "timestamp": "2026-07-11T00:00:01Z"},
    ]
    return s


def _run_save(monkeypatch, module, save_fn, session, ok):
    captured = {}

    async def fake_save(**kwargs):
        captured.update(kwargs)
        return ok

    monkeypatch.setattr(module, "save_voice_transcript", fake_save)
    asyncio.run(save_fn(session))
    return captured


def test_realtime_save_clears_on_success(monkeypatch):
    session = _session(RealtimeSession, "t-rt-ok")
    captured = _run_save(monkeypatch, rt, rt.save_session_to_blackbox, session, ok=True)
    assert session.conversation == []
    assert captured["operator"] == "system"
    assert "OpenAI Realtime Voice Session" in captured["session_summary"]
    assert "[User]: hi there" in captured["session_summary"]
    assert "[AI]: hello" in captured["session_summary"]
    assert captured["user_message"].startswith("[Voice Session Transcript]")


def test_realtime_save_keeps_transcript_on_failure(monkeypatch):
    session = _session(RealtimeSession, "t-rt-fail")
    _run_save(monkeypatch, rt, rt.save_session_to_blackbox, session, ok=False)
    assert len(session.conversation) == 2, \
        "conversation must be KEPT after a failed save (retry on next teardown)"
