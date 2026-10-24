"""The voice agents place calls through the shared make_phone_call executor (audit
O18-toolvault-modules-08, review of rank 57).

The OpenAI Realtime, Gemini Live and Grok voice routes each had their own
make_phone_call branch that always POSTed the raw number to /twilio/call. On a
TG200/Asterisk box (TELEPHONY_PROVIDER=asterisk, or auto with Asterisk enabled)
those calls then failed or went out through Twilio instead of the gateway, and
the number was never normalized. They now run the ToolVault executor, which
picks the route (Orchestrator.telephony), normalizes to E.164 and treats a 200
{"error": ...} as a failure. A static guard keeps any new copy from
hard-coding a call route. Hermetic: the executor is a fake.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

import Orchestrator.routes.gemini_live_routes as gm
import Orchestrator.routes.grok_live_routes as gk
import Orchestrator.routes.realtime_routes as rt
import Orchestrator.tools as tools
from Orchestrator.models import GeminiLiveSession, GrokLiveSession, RealtimeSession
from Orchestrator.tests.voice_ws_fakes import FakeUpstreamWS
from Orchestrator.toolvault.context import ToolResult

pytestmark = pytest.mark.timeout(30)

ARGS = {"phone_number": "(555) 123-4567", "greeting": "Hi", "role": "concierge", "backend": "gemini_live"}
REFUSED = "Failed to initiate call: Phone service is not enabled"


@pytest.fixture
def executor(monkeypatch):
    calls = []

    class FakeExecutor:
        def __init__(self, operator="system", **kw):
            self.operator = operator

        async def execute(self, name, params):
            calls.append((self.operator, name, dict(params)))
            return ToolResult(False, REFUSED)

    monkeypatch.setattr(tools, "BlackBoxToolExecutor", FakeExecutor)
    return calls


def _expect(calls):
    assert calls == [("Alice", "make_phone_call", {
        "phone_number": ARGS["phone_number"], "greeting": "Hi",
        "role": "concierge", "backend": "gemini_live"})]


def test_openai_realtime_calls_through_the_executor(executor):
    async def run():
        session = RealtimeSession(session_id="t-rt-call", operator="Alice")
        session.openai_ws = FakeUpstreamWS()
        await rt.handle_openai_message(session, {
            "type": "response.function_call_arguments.done", "call_id": "c1",
            "name": "make_phone_call", "arguments": json.dumps(ARGS)})
        await asyncio.gather(*session.tool_tasks)  # the tool runs as a session task (M4 U26)
        return session.openai_ws.sent[0]["item"]["output"]

    output = asyncio.run(run())
    _expect(executor)
    assert REFUSED in output


def test_grok_live_calls_through_the_executor(executor):
    async def run():
        session = GrokLiveSession(session_id="t-gk-call", operator="Alice")
        session.grok_ws = FakeUpstreamWS()
        await gk.handle_grok_message(session, {
            "type": "response.function_call_arguments.done", "call_id": "c2",
            "name": "make_phone_call", "arguments": json.dumps(ARGS)})
        await asyncio.gather(*session.tool_tasks)  # the tool runs as a session task (M4 U26)
        return session.grok_ws.sent[0]["item"]["output"]

    output = asyncio.run(run())
    _expect(executor)
    assert REFUSED in output


def test_gemini_live_calls_through_the_executor(executor):
    async def run():
        session = GeminiLiveSession(session_id="t-gm-call", operator="Alice")
        session.gemini_ws = FakeUpstreamWS()
        await gm.handle_gemini_message(session, {"toolCall": {"functionCalls": [
            {"id": "fc-call", "name": "make_phone_call", "args": dict(ARGS)}]}})
        await asyncio.gather(*session.tool_tasks)  # the tool runs as a session task (M4 U26)
        return session.gemini_ws.sent[0]["toolResponse"]["functionResponses"][0]["response"]["result"]

    output = asyncio.run(run())
    _expect(executor)
    assert REFUSED in output


# The only modules that may name a call route: the routes themselves and the
# one helper that chooses between them.
_ROUTE_LITERAL = re.compile(r"""["'](/(?:twilio|asterisk|cellular)/call)["']""")
_ALLOWED = {"Orchestrator/telephony.py", "Orchestrator/routes/twilio_routes.py",
            "Orchestrator/routes/asterisk_routes.py"}


def test_no_module_hard_codes_a_call_route():
    root = Path(__file__).resolve().parents[2]
    offenders = []
    for base in ("Orchestrator", "ToolVault", "MCP"):
        for path in (root / base).rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if "/tests/" in rel or "/venv/" in rel or rel in _ALLOWED or path.name.startswith("test_"):
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if _ROUTE_LITERAL.search(line):
                    offenders.append(f"{rel}:{n}: {line.strip()}")
    assert offenders == [], "call routes are chosen by Orchestrator.telephony:\n" + "\n".join(offenders)
