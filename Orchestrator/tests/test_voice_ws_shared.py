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
