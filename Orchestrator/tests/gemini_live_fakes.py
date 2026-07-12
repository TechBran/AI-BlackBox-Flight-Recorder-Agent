"""Shared fakes for the Gemini Live pytest suites (Phase 1a).

Real GeminiLiveSession dataclass + minimal async fakes for both websocket ends.
FakePortalWS satisfies _safe_ws_send's CONNECTED check and records every frame.
"""
from unittest.mock import AsyncMock

from starlette.websockets import WebSocketState

from Orchestrator.models import GeminiLiveSession


class FakePortalWS:
    def __init__(self):
        self.application_state = WebSocketState.CONNECTED
        self.sent: list = []
        self.closed: list = []

    async def send_json(self, obj):
        self.sent.append(obj)

    async def close(self, code=1000, reason=""):
        self.closed.append((code, reason))
        self.application_state = WebSocketState.DISCONNECTED

    def frames(self, type_):
        return [f for f in self.sent if f.get("type") == type_]


class FakeGeminiWS:
    """Async-iterable fake of the upstream websockets connection.

    ``messages`` are yielded in order; ``closing_exc`` (if set) is raised after
    they are exhausted — simulating a WS close frame mid-listen.
    """
    def __init__(self, messages=None, closing_exc=None):
        self._messages = list(messages or [])
        self._closing_exc = closing_exc
        self.send = AsyncMock()
        self.close = AsyncMock()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        if self._closing_exc is not None:
            raise self._closing_exc
        raise StopAsyncIteration


def make_session(**overrides) -> GeminiLiveSession:
    session = GeminiLiveSession(session_id="test-session", operator="test_operator")
    session.portal_ws = FakePortalWS()
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


def stub_fossil_context(monkeypatch):
    def _stub(user_text, operator, log_prefix=""):
        return ("", {"recent": [], "keyword": [], "semantic": [], "checkpoint": []})
    monkeypatch.setattr(
        "Orchestrator.routes.gemini_live_routes.build_fossil_context", _stub
    )
