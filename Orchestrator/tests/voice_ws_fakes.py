"""Shared fake WebSocket doubles for the P1b voice-route hardening tests."""
import json

from starlette.websockets import WebSocketState


class FakeUpstreamWS:
    """Stands in for a `websockets` client connection (OpenAI/Grok/Gemini side)."""

    def __init__(self):
        self.sent = []          # decoded JSON frames, in send order
        self.closed = False

    async def send(self, payload: str):
        self.sent.append(json.loads(payload))

    async def close(self):
        self.closed = True


class FakeStreamWS(FakeUpstreamWS):
    """FakeUpstreamWS that also async-iterates a scripted list of inbound frames."""

    def __init__(self, messages=None):
        super().__init__()
        self._messages = list(messages or [])

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class FakePortalWS:
    """Stands in for the FastAPI client WebSocket (routes check application_state)."""

    application_state = WebSocketState.CONNECTED

    def __init__(self):
        self.sent = []

    async def send_json(self, data):
        self.sent.append(data)
