"""P2 — /ws/realtime endpoint plumbing for noise_reduction + transcription_delay.

Drives the real WS endpoint with FastAPI TestClient; upstream dial, session
config, background loops, and teardown save are stubbed at module attrs
(same override-at-imported-name pattern as test_live_models.py)."""
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import Orchestrator.routes.realtime_routes as rr
from Orchestrator.checkpoint import app


@pytest.fixture
def relay_stubs(monkeypatch):
    monkeypatch.setattr(rr, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(rr, "WEBSOCKETS_AVAILABLE", True)
    connect_mock = AsyncMock(return_value=True)
    configure_mock = AsyncMock()
    monkeypatch.setattr(rr, "connect_to_openai", connect_mock)
    monkeypatch.setattr(rr, "configure_openai_session", configure_mock)
    monkeypatch.setattr(rr, "save_session_to_blackbox", AsyncMock())

    async def _noop(session):
        return None

    monkeypatch.setattr(rr, "openai_listener", _noop)
    monkeypatch.setattr(rr, "openai_keepalive_loop", _noop)
    return connect_mock, configure_mock


def _drive_connect(path, connect_msg):
    client = TestClient(app)
    with client.websocket_connect(path) as ws:
        ws.send_text(json.dumps(connect_msg))
        assert ws.receive_json()["type"] == "status"
        assert ws.receive_json()["type"] == "connected"
        ws.send_text(json.dumps({"type": "disconnect"}))


def test_query_params_reach_configure(relay_stubs):
    _, configure_mock = relay_stubs
    _drive_connect(
        "/ws/realtime/p2-ep-1?noise_reduction=far_field&transcription_delay=low",
        {"type": "connect", "operator": "test_operator", "voice": "ash"},
    )
    kwargs = configure_mock.await_args.kwargs
    assert kwargs["noise_reduction"] == "far_field"
    assert kwargs["transcription_delay"] == "low"


def test_connect_json_wins_over_query_params(relay_stubs):
    _, configure_mock = relay_stubs
    _drive_connect(
        "/ws/realtime/p2-ep-2?noise_reduction=far_field",
        {"type": "connect", "operator": "test_operator",
         "noise_reduction": "near_field", "transcription_delay": "minimal"},
    )
    kwargs = configure_mock.await_args.kwargs
    assert kwargs["noise_reduction"] == "near_field"
    assert kwargs["transcription_delay"] == "minimal"


def test_params_default_none_when_absent(relay_stubs):
    _, configure_mock = relay_stubs
    _drive_connect("/ws/realtime/p2-ep-3", {"type": "connect", "operator": "test_operator"})
    kwargs = configure_mock.await_args.kwargs
    assert kwargs["noise_reduction"] is None
    assert kwargs["transcription_delay"] is None
