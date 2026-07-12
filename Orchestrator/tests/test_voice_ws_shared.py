"""Unit tests for Orchestrator/routes/voice_ws_shared.py (P1b cross-route hardening)."""
import asyncio

from Orchestrator.routes import voice_ws_shared as vs
from Orchestrator.tests.voice_ws_fakes import FakeUpstreamWS, FakePortalWS


FC_EVENT = {
    "type": "response.function_call_arguments.done",
    "call_id": "call-42",
    "name": "web_fetch",
    "arguments": "{\"url\": \"https://x\"}",
}

GEMINI_EVENT = {
    "toolCall": {
        "functionCalls": [
            {"id": "fc-1", "name": "get_current_time", "args": {}},
            {"id": "fc-2", "name": "web_fetch", "args": {"url": "https://x"}},
        ]
    }
}


def test_openai_style_tool_error_answers_call_id():
    async def run():
        upstream, portal = FakeUpstreamWS(), FakePortalWS()
        ok = await vs.send_openai_style_tool_error(
            upstream, portal, FC_EVENT, RuntimeError("boom"))
        assert ok is True
        assert [m["type"] for m in upstream.sent] == \
            ["conversation.item.create", "response.create"]
        item = upstream.sent[0]["item"]
        assert item["type"] == "function_call_output"
        assert item["call_id"] == "call-42"
        assert "web_fetch" in item["output"] and "boom" in item["output"]
        assert portal.sent[0]["type"] == "tool_result"
        assert portal.sent[0]["data"]["error"] is True
    asyncio.run(run())


def test_openai_style_tool_error_ignores_non_tool_events():
    async def run():
        upstream = FakeUpstreamWS()
        ok = await vs.send_openai_style_tool_error(
            upstream, None, {"type": "response.done"}, RuntimeError("x"))
        assert ok is False
        assert upstream.sent == []
    asyncio.run(run())


def test_openai_style_tool_error_never_raises_on_dead_upstream():
    class DeadWS:
        async def send(self, payload):
            raise ConnectionError("closed")

    async def run():
        ok = await vs.send_openai_style_tool_error(
            DeadWS(), None, FC_EVENT, RuntimeError("boom"))
        assert ok is False
    asyncio.run(run())


def test_gemini_tool_error_answers_all_unanswered():
    async def run():
        upstream, portal = FakeUpstreamWS(), FakePortalWS()
        ok = await vs.send_gemini_tool_error(
            upstream, portal, GEMINI_EVENT, RuntimeError("boom"), answered_ids=None)
        assert ok is True
        responses = upstream.sent[0]["toolResponse"]["functionResponses"]
        assert [r["id"] for r in responses] == ["fc-1", "fc-2"]
        assert all("boom" in r["response"]["result"] for r in responses)
        assert len(portal.sent) == 2
    asyncio.run(run())


def test_gemini_tool_error_skips_answered_ids():
    async def run():
        upstream = FakeUpstreamWS()
        ok = await vs.send_gemini_tool_error(
            upstream, None, GEMINI_EVENT, RuntimeError("boom"), answered_ids={"fc-1"})
        assert ok is True
        responses = upstream.sent[0]["toolResponse"]["functionResponses"]
        assert [r["id"] for r in responses] == ["fc-2"]
    asyncio.run(run())


def test_gemini_tool_error_noop_when_all_answered():
    async def run():
        upstream = FakeUpstreamWS()
        ok = await vs.send_gemini_tool_error(
            upstream, None, GEMINI_EVENT, RuntimeError("boom"),
            answered_ids={"fc-1", "fc-2"})
        assert ok is False
        assert upstream.sent == []
    asyncio.run(run())


# ---------------------------------------------------------------------------
# save_voice_transcript (POST /chat/save, clear-only-on-200 contract)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status):
        self.status = status

    async def json(self):
        return {"success": True, "minted": True, "snap_id": "SNAP-20260711-TEST"}

    async def text(self):
        return "server error body"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_aiohttp(log, status):
    class _FakePost:
        def __init__(self, url, **kwargs):
            log.append({"url": url, **kwargs})
            self._resp = _FakeResp(status)

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *exc):
            return False

    class FakeClientSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, **kwargs):
            return _FakePost(url, **kwargs)

    class FakeTimeout:
        def __init__(self, total=None):
            self.total = total

    class FakeAiohttpModule:
        ClientSession = FakeClientSession
        ClientTimeout = FakeTimeout

    return FakeAiohttpModule


def test_save_voice_transcript_true_on_200(monkeypatch):
    log = []
    monkeypatch.setattr(vs, "aiohttp", _fake_aiohttp(log, 200))
    ok = asyncio.run(vs.save_voice_transcript(
        operator="system",
        user_message="[Voice Session Transcript] test session t-1",
        session_summary="=== Test Voice Session ===\n[User]: hi",
        model_label="test-voice",
        log_prefix="[TEST]",
    ))
    assert ok is True
    assert log[0]["url"] == "http://localhost:9091/chat/save"
    body = log[0]["json"]
    assert body["operator"] == "system"
    assert body["user_message"].startswith("[Voice Session Transcript]")
    assert body["assistant_response"].startswith("=== Test Voice Session ===")
    assert body["model"] == "test-voice"
    assert body["tokens"] == {"prompt": 0, "completion": 0}


def test_save_voice_transcript_false_on_500(monkeypatch):
    monkeypatch.setattr(vs, "aiohttp", _fake_aiohttp([], 500))
    ok = asyncio.run(vs.save_voice_transcript(
        operator="system", user_message="m", session_summary="s",
        model_label="test-voice", log_prefix="[TEST]"))
    assert ok is False


def test_save_voice_transcript_false_on_exception(monkeypatch):
    class Exploding:
        ClientTimeout = staticmethod(lambda total=None: None)

        class ClientSession:
            def __init__(self, *a, **k):
                raise ConnectionError("no server")

    monkeypatch.setattr(vs, "aiohttp", Exploding)
    ok = asyncio.run(vs.save_voice_transcript(
        operator="system", user_message="m", session_summary="s",
        model_label="test-voice", log_prefix="[TEST]"))
    assert ok is False
