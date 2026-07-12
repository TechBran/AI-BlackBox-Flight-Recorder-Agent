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
