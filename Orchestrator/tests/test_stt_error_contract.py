"""stt_error carries a machine-readable code (audit rank 52: W24,
O08-stt-realtime-04).

ElevenLabs quota_exceeded reached the client as a bare message (seven frames
Oct 2-6): nothing could tell an exhausted provider from a blip, Android
reconnected to the same provider, and no surface offered another one. Every
stt_error now names a code, the provider and whether another provider could
serve the dictation. The frames are pinned by the fixture the Portal and
Android suites read too: tests/fixtures/stt_ws_frames.json.

Hermetic: the endpoint coroutine is driven with a scripted client socket and a
fake upstream; provider resolution is stubbed.
"""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect

from Orchestrator.routes import stt_ws_routes
from Orchestrator.stt import errors

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "stt_ws_frames.json"


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _frame(name):
    return next(f["frame"] for f in _fixture()["frames"] if f["name"] == name)


class _Client:
    def __init__(self, start):
        self._inbox = [start]
        self.sent = []

    async def accept(self):
        pass

    async def receive_json(self):
        if self._inbox:
            return self._inbox.pop(0)
        await asyncio.sleep(0.05)
        raise WebSocketDisconnect()

    async def send_json(self, obj):
        self.sent.append(obj)


class _Upstream:
    def __init__(self, frames):
        self._frames = [json.dumps(f) for f in frames]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def send(self, data):
        pass

    async def close(self, *a, **k):
        pass


class _WsModule:
    def __init__(self, upstream):
        self._upstream = upstream

    async def connect(self, url, **kwargs):
        return self._upstream


# The providers that could stream on the simulated box: several by default, so
# a provider failure has somewhere to switch to.
MULTI_PROVIDER_BOX = ["openai", "google", "elevenlabs"]


@pytest.fixture
def resolved(monkeypatch):
    monkeypatch.setattr(stt_ws_routes, "local_streaming_stt_available", lambda: False)
    monkeypatch.setattr(stt_ws_routes, "onbox_stt_available", lambda: False)
    streaming = {"providers": list(MULTI_PROVIDER_BOX)}
    monkeypatch.setattr(stt_ws_routes, "streaming_stt_providers",
                        lambda **k: list(streaming["providers"]))

    def pick(provider, box=None):
        monkeypatch.setattr(stt_ws_routes, "resolve_stt_provider",
                            lambda *a, **k: provider)
        if box is not None:
            streaming["providers"] = list(box)
    return pick


def _run(client):
    asyncio.run(stt_ws_routes.ws_stt(client))
    return client.sent


def test_elevenlabs_quota_exceeded_is_a_coded_switchable_error(resolved, monkeypatch):
    resolved("elevenlabs")
    monkeypatch.setattr(stt_ws_routes, "resolve_api_key", lambda: "test-key")
    monkeypatch.setattr(stt_ws_routes, "websockets", _WsModule(_Upstream([
        {"message_type": "quota_exceeded", "error": "This request exceeds your quota."},
    ])))

    sent = _run(_Client({"type": "stt_start", "provider": "", "sample_rate": 24000}))

    errors_sent = [m for m in sent if m["type"] == "stt_error"]
    assert errors_sent == [_frame("elevenlabs_quota_exceeded")]
    assert sent[-1] == {"type": "stt_done"}


def test_no_provider_is_coded_and_not_switchable(resolved):
    resolved(None)
    sent = _run(_Client({"type": "stt_start", "provider": ""}))
    assert sent[0] == _frame("no_provider")
    assert sent[-1] == {"type": "stt_done"}


def test_bad_first_frame_is_a_bad_request(resolved):
    resolved("openai")
    sent = _run(_Client({"type": "stt_audio", "pcm": ""}))
    assert sent[0]["code"] == errors.BAD_REQUEST
    assert sent[0]["switchable"] is False


def test_bridge_exception_names_the_provider(resolved, monkeypatch):
    resolved("google")

    async def explode(websocket, provider, start):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(stt_ws_routes, "run_stt_bridge", explode)
    sent = _run(_Client({"type": "stt_start", "provider": ""}))
    err = sent[0]
    assert err["type"] == "stt_error" and "provider exploded" in err["message"]
    assert err["code"] == errors.PROVIDER_ERROR
    assert err["provider"] == "google"
    assert err["switchable"] is True


def test_every_stt_error_a_bridge_sends_is_stamped(resolved, monkeypatch):
    """The stamp sits on the socket the bridges write to, so an stt_error from
    any site (today's and tomorrow's) carries the contract fields."""
    resolved("openai")

    async def bridge(websocket, provider, start):
        await websocket.send_json({"type": "stt_error", "message": "a new failure"})

    monkeypatch.setattr(stt_ws_routes, "run_stt_bridge", bridge)
    sent = _run(_Client({"type": "stt_start", "provider": ""}))
    assert sent[0] == {"type": "stt_error", "message": "a new failure",
                       "code": errors.PROVIDER_ERROR, "provider": "openai", "switchable": True}


def test_openai_billing_refusal_reads_as_quota_exceeded():
    fixture = _frame("openai_credits_exhausted")
    raw = {"type": "stt_error", "message": fixture["message"], "error_kind": "credits_exhausted"}
    assert errors.stamp(raw, "openai") == fixture


def test_fixture_codes_are_the_server_taxonomy():
    for entry in _fixture()["frames"]:
        frame = entry["frame"]
        if frame["type"] != "stt_error" or "code" not in frame:
            continue
        assert frame["code"] in errors.CODES, entry["name"]
        assert errors.stamp(frame, frame.get("provider")) == frame, entry["name"]


def test_fixture_status_states_are_ones_the_server_sends():
    source = Path(stt_ws_routes.__file__).read_text(encoding="utf-8")
    for entry in _fixture()["frames"]:
        if entry["frame"]["type"] == "stt_status":
            assert f'"{entry["frame"]["state"]}"' in source, entry["name"]


# ---------------------------------------------------------------------------
# switchable means "another provider can stream now" (review of rank 52)
# ---------------------------------------------------------------------------
# stamp() set switchable for every code but no_provider/bad_request, without
# looking at what else the box has. On a single-provider box (the fresh-box
# case) the Portal then skipped its record-then-/stt fallback for an offer that
# had nothing in it, and the user got a toast and a dead mic.
def test_a_single_provider_box_never_offers_a_switch(resolved, monkeypatch):
    resolved("elevenlabs", box=["elevenlabs"])
    monkeypatch.setattr(stt_ws_routes, "resolve_api_key", lambda: "test-key")
    monkeypatch.setattr(stt_ws_routes, "websockets", _WsModule(_Upstream([
        {"message_type": "quota_exceeded", "error": "This request exceeds your quota."},
    ])))

    sent = _run(_Client({"type": "stt_start", "provider": "", "sample_rate": 24000}))

    errors_sent = [m for m in sent if m["type"] == "stt_error"]
    assert errors_sent == [_frame("single_provider_quota_exceeded")]


def test_a_bridge_exception_on_a_single_provider_box_is_not_switchable(resolved, monkeypatch):
    resolved("openai", box=["openai"])

    async def explode(websocket, provider, start):
        raise RuntimeError("server_error")

    monkeypatch.setattr(stt_ws_routes, "run_stt_bridge", explode)
    sent = _run(_Client({"type": "stt_start", "provider": ""}))
    assert sent[0]["code"] == errors.PROVIDER_ERROR
    assert sent[0]["switchable"] is False


def test_switchable_needs_a_provider_other_than_the_failing_one():
    frame = {"type": "stt_error", "message": "m", "code": errors.QUOTA_EXCEEDED}
    assert errors.stamp(frame, "openai", alternatives=[])["switchable"] is False
    assert errors.stamp(frame, "openai", alternatives=["google"])["switchable"] is True
    no_provider = {"type": "stt_error", "message": "m", "code": errors.NO_PROVIDER}
    assert errors.stamp(no_provider, None, alternatives=["google"])["switchable"] is False


def test_streaming_providers_are_the_ones_that_can_stream(monkeypatch):
    from Orchestrator.stt import resolve

    monkeypatch.setattr(resolve, "stt_availability", lambda: (True, False, True))
    assert resolve.streaming_stt_providers(local_ok=False, onbox_ok=False) == ["openai", "elevenlabs"]
    assert resolve.streaming_stt_providers(local_ok=True, onbox_ok=True) == \
        ["openai", "elevenlabs", "onbox", "local"]
