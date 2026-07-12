"""P1b: tool-dispatch exceptions and malformed args must answer the model, never dangle."""
import asyncio
import json

import Orchestrator.routes.realtime_routes as rt
from Orchestrator.models import RealtimeSession
from Orchestrator.tests.voice_ws_fakes import FakeUpstreamWS, FakeStreamWS


def test_realtime_malformed_args_returns_parse_error(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("tool must NOT execute on malformed args")
    monkeypatch.setattr(rt, "execute_search_snapshots", boom)

    async def run():
        session = RealtimeSession(session_id="t-badargs", operator="system")
        session.openai_ws = FakeUpstreamWS()
        event = {
            "type": "response.function_call_arguments.done",
            "call_id": "call-7", "name": "search_snapshots",
            "arguments": "{not valid json",
        }
        await rt.handle_openai_message(session, event)
        assert [m["type"] for m in session.openai_ws.sent] == \
            ["conversation.item.create", "response.create"]
        item = session.openai_ws.sent[0]["item"]
        assert item["type"] == "function_call_output"
        assert item["call_id"] == "call-7"
        assert "Malformed tool-call arguments" in item["output"]
    asyncio.run(run())


def test_realtime_listener_answers_dangling_call_on_dispatch_crash(monkeypatch):
    async def crash(session, event):
        raise RuntimeError("executor exploded")
    monkeypatch.setattr(rt, "handle_openai_message", crash)

    async def run():
        session = RealtimeSession(session_id="t-crash", operator="system")
        fc_event = {
            "type": "response.function_call_arguments.done",
            "call_id": "call-9", "name": "roll_dice", "arguments": "{}",
        }
        ws = FakeStreamWS([json.dumps(fc_event)])
        session.openai_ws = ws
        await rt.openai_listener(session)
        assert [m["type"] for m in ws.sent] == \
            ["conversation.item.create", "response.create"], \
            "a dispatch crash must still answer the call_id with an error payload"
        item = ws.sent[0]["item"]
        assert item["call_id"] == "call-9"
        assert "executor exploded" in item["output"]
    asyncio.run(run())


def test_realtime_normal_dispatch_still_answers():
    async def run():
        session = RealtimeSession(session_id="t-ok", operator="system")
        session.openai_ws = FakeUpstreamWS()
        event = {
            "type": "response.function_call_arguments.done",
            "call_id": "call-1", "name": "get_current_time", "arguments": "{}",
        }
        await rt.handle_openai_message(session, event)
        assert [m["type"] for m in session.openai_ws.sent] == \
            ["conversation.item.create", "response.create"]
        assert "Current date and time" in session.openai_ws.sent[0]["item"]["output"]
    asyncio.run(run())


import Orchestrator.routes.grok_live_routes as gk
from Orchestrator.models import GrokLiveSession


def test_grok_malformed_args_returns_parse_error(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("tool must NOT execute on malformed args")
    monkeypatch.setattr(gk, "execute_grok_search_snapshots", boom)

    async def run():
        session = GrokLiveSession(session_id="t-gk-badargs", operator="system")
        session.grok_ws = FakeUpstreamWS()
        event = {
            "type": "response.function_call_arguments.done",
            "call_id": "call-8", "name": "search_snapshots",
            "arguments": "{not valid json",
        }
        await gk.handle_grok_message(session, event)
        assert [m["type"] for m in session.grok_ws.sent] == \
            ["conversation.item.create", "response.create"]
        item = session.grok_ws.sent[0]["item"]
        assert item["call_id"] == "call-8"
        assert "Malformed tool-call arguments" in item["output"]
    asyncio.run(run())


def test_grok_listener_answers_dangling_call_on_dispatch_crash(monkeypatch):
    async def crash(session, event):
        raise RuntimeError("executor exploded")
    monkeypatch.setattr(gk, "handle_grok_message", crash)

    async def run():
        session = GrokLiveSession(session_id="t-gk-crash", operator="system")
        fc_event = {
            "type": "response.function_call_arguments.done",
            "call_id": "call-10", "name": "roll_dice", "arguments": "{}",
        }
        ws = FakeStreamWS([json.dumps(fc_event)])
        session.grok_ws = ws
        await gk.grok_listener(session)
        assert [m["type"] for m in ws.sent] == \
            ["conversation.item.create", "response.create"]
        item = ws.sent[0]["item"]
        assert item["call_id"] == "call-10"
        assert "executor exploded" in item["output"]
    asyncio.run(run())


import Orchestrator.routes.gemini_live_routes as gm
from Orchestrator.models import GeminiLiveSession


def test_gemini_listener_answers_dangling_calls_on_dispatch_crash(monkeypatch):
    async def crash(session, event):
        raise RuntimeError("executor exploded")
    monkeypatch.setattr(gm, "handle_gemini_message", crash)

    async def run():
        session = GeminiLiveSession(session_id="t-gm-crash", operator="system")
        event = {"toolCall": {"functionCalls": [
            {"id": "fc-a", "name": "web_fetch", "args": {"url": "https://x"}},
            {"id": "fc-b", "name": "roll_dice", "args": {}},
        ]}}
        ws = FakeStreamWS([json.dumps(event)])
        session.gemini_ws = ws
        await gm.gemini_listener(session)
        assert len(ws.sent) == 1, "crash must produce ONE toolResponse frame"
        responses = ws.sent[0]["toolResponse"]["functionResponses"]
        assert [r["id"] for r in responses] == ["fc-a", "fc-b"]
        assert all("executor exploded" in r["response"]["result"] for r in responses)
    asyncio.run(run())


def test_gemini_dispatch_records_answered_ids():
    async def run():
        session = GeminiLiveSession(session_id="t-gm-ids", operator="system")
        session.gemini_ws = FakeUpstreamWS()
        event = {"toolCall": {"functionCalls": [
            {"id": "fc-t", "name": "get_current_time", "args": {}},
        ]}}
        await gm.handle_gemini_message(session, event)
        assert session.answered_tool_call_ids == {"fc-t"}, \
            "dispatch must record answered ids so the error responder never double-answers"
        responses = session.gemini_ws.sent[0]["toolResponse"]["functionResponses"]
        assert responses[0]["id"] == "fc-t"
    asyncio.run(run())
