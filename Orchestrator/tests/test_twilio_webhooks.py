"""Twilio voice webhooks: signed, PIN state server-side, media bound to the call.

Audit X03-security-backend-08: a forged POST /twilio/voice -> /twilio/operator
-> WebSocket /twilio/media/<id> reached a tool-armed AI without the PIN, and
the PIN retry counter was a query parameter the caller controlled. W50 /
X03-security-backend-02: /twilio/sms handed any texter the phone tool group.

These tests run the real routes on the shared FastAPI app through TestClient
WITHOUT entering its lifespan (no startup hooks run) and compute
X-Twilio-Signature with a known test token. The AI bridge and the ledger save
are stubbed, so no network and no live service is touched.
"""
import re
from xml.etree import ElementTree

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from Orchestrator import config
from Orchestrator.phone import pin_gate
from Orchestrator.phone import twilio_signature as ts
from Orchestrator.phone.session import PHONE_SESSIONS, PhoneStatus
from Orchestrator.routes import twilio_routes as tw

TOKEN = "test-auth-token-not-real"
PIN = "739104"
WRONG = "552281"
CALL_SID = "CA00000000000000000000000000000001"
CALLER = "+15555550142"
BASE = "http://testserver"

# POST routes under /twilio that Twilio itself calls (must be signed) vs the
# box's own server-to-server APIs (make_phone_call, live-voice callbacks, the
# Claude CLI phone bridge). A new /twilio POST route must be added to one set.
TWILIO_WEBHOOKS = {
    "/twilio/voice", "/twilio/pin", "/twilio/gather", "/twilio/operator",
    "/twilio/status", "/twilio/outbound-answer", "/twilio/cli/answer",
}
INTERNAL_APIS = {
    "/twilio/call", "/twilio/cli/call", "/twilio/cli/{cli_session_id}/thinking",
    "/twilio/cli/{cli_session_id}/speak", "/twilio/cli/{cli_session_id}/audio",
    "/twilio/inject-audio",
}


class FakeBridge:
    instances = []

    def __init__(self, session):
        self.session = session
        self.started_with = None
        self.is_running = False
        self.on_ai_audio = None
        self.on_ai_transcript = None
        FakeBridge.instances.append(self)

    async def start(self, operator):
        self.started_with = operator
        self.is_running = True
        return True

    async def send_audio(self, data):
        pass

    async def stop(self):
        self.is_running = False


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(tw, "PHONE_ENABLED", True)
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(config, "TWILIO_WEBHOOK_BASE_URL", "")
    monkeypatch.setattr(config, "PHONE_PIN_ENABLED", True)
    monkeypatch.setattr(config, "PHONE_PIN_CODE", PIN)
    monkeypatch.setattr(config, "PHONE_PIN_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(pin_gate, "LOCKOUT_PATH", tmp_path / "phone_pin_lockout.json")
    monkeypatch.setattr(tw, "USERS_LIST", ["Alpha", "Bravo"])
    monkeypatch.setattr(tw, "PhoneAIBridge", FakeBridge)

    async def no_save(session):
        return None

    monkeypatch.setattr(tw, "save_session_to_blackbox", no_save)
    FakeBridge.instances.clear()
    PHONE_SESSIONS.clear()
    yield TestClient(tw.app)
    PHONE_SESSIONS.clear()


def signed_post(client, path, data, *, sign_url=None, token=TOKEN, headers=None):
    url = sign_url or (BASE + path)
    sig = ts.compute_signature(token, url, data)
    return client.post(path, data=data, headers={"X-Twilio-Signature": sig, **(headers or {})})


def voice(client, caller=CALLER):
    return signed_post(client, "/twilio/voice",
                       {"CallSid": CALL_SID, "From": caller, "To": "+15555550100"})


def _action(resp):
    root = ElementTree.fromstring(resp.text)
    gather = root.find("Gather")
    return gather.get("action") if gather is not None else None


def _session_id(resp):
    return re.search(r"session_id=([\w-]+)", resp.text).group(1)


def _hangs_up(resp):
    return ElementTree.fromstring(resp.text).find("Hangup") is not None


# ── every Twilio webhook carries the signature guard ────────────────────────

def test_every_twilio_post_route_is_classified_and_webhooks_are_guarded():
    posts = {r.path: r for r in tw.app.routes
             if isinstance(r, APIRoute) and r.path.startswith("/twilio") and "POST" in r.methods}
    assert set(posts) == TWILIO_WEBHOOKS | INTERNAL_APIS, \
        "classify new /twilio POST routes as a Twilio webhook (signed) or an internal API"
    for path in TWILIO_WEBHOOKS:
        deps = [d.call for d in posts[path].dependant.dependencies]
        assert ts.require_twilio_signature in deps, f"{path} is not signature-checked"


def test_twilio_sms_handler_is_gone(client):
    resp = signed_post(client, "/twilio/sms", {"From": CALLER, "Body": "hi"})
    assert resp.status_code in (404, 405)


# ── signature enforcement ───────────────────────────────────────────────────

def test_missing_signature_is_403(client):
    resp = client.post("/twilio/voice", data={"CallSid": CALL_SID, "From": CALLER})
    assert resp.status_code == 403
    assert not PHONE_SESSIONS


def test_tampered_param_is_403(client):
    data = {"CallSid": CALL_SID, "From": CALLER}
    sig = ts.compute_signature(TOKEN, BASE + "/twilio/voice", data)
    resp = client.post("/twilio/voice", data={**data, "From": "+15555550143"},
                       headers={"X-Twilio-Signature": sig})
    assert resp.status_code == 403
    assert not PHONE_SESSIONS


def test_wrong_token_is_403(client):
    resp = signed_post(client, "/twilio/voice", {"CallSid": CALL_SID, "From": CALLER},
                       token="someone-elses-token")
    assert resp.status_code == 403


def test_unconfigured_token_fails_closed(client, monkeypatch):
    monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "")
    resp = signed_post(client, "/twilio/voice", {"CallSid": CALL_SID, "From": CALLER}, token="")
    assert resp.status_code == 403


def test_status_callback_is_guarded(client):
    assert client.post("/twilio/status", data={"CallSid": CALL_SID}).status_code == 403
    assert signed_post(client, "/twilio/status", {"CallSid": CALL_SID,
                                                  "CallStatus": "ringing"}).status_code == 200


def test_signature_over_public_forwarded_url_is_accepted(client):
    # tailscale serve/Funnel: Host preserved, X-Forwarded-Proto=https; Twilio
    # signed the public https URL, not http://testserver.
    data = {"CallSid": CALL_SID, "From": CALLER}
    resp = signed_post(client, "/twilio/voice", data,
                       sign_url="https://box.example.ts.net/twilio/voice",
                       headers={"Host": "box.example.ts.net", "X-Forwarded-Proto": "https"})
    assert resp.status_code == 200


def test_signature_over_configured_base_is_accepted(client, monkeypatch):
    monkeypatch.setattr(config, "TWILIO_WEBHOOK_BASE_URL", "wss://box.example.ts.net")
    resp = signed_post(client, "/twilio/voice", {"CallSid": CALL_SID, "From": CALLER},
                       sign_url="https://box.example.ts.net/twilio/voice")
    assert resp.status_code == 200


# ── PIN gate, server-side ───────────────────────────────────────────────────

def test_voice_prompts_for_pin_with_derived_length(client):
    resp = voice(client)
    assert resp.status_code == 200
    gather = ElementTree.fromstring(resp.text).find("Gather")
    assert gather.get("numDigits") == str(len(PIN))
    assert f"{len(PIN)} digit" in resp.text
    assert "retry=" not in gather.get("action")
    session = PHONE_SESSIONS[_session_id(resp)]
    assert session.call_id == CALL_SID and session.pin_verified is False


def test_voice_refuses_when_pin_unconfigured(client, monkeypatch, capsys):
    monkeypatch.setattr(config, "PHONE_PIN_CODE", "")
    resp = voice(client)
    assert resp.status_code == 200 and _hangs_up(resp)
    assert ElementTree.fromstring(resp.text).find("Gather") is None
    assert "PHONE_PIN_CODE" in capsys.readouterr().out


def test_pin_attempts_are_counted_server_side(client, capsys):
    sid = _session_id(voice(client))
    # The caller-controlled retry=0 must not reset the server-side count.
    for _ in range(2):
        resp = signed_post(client, f"/twilio/pin?session_id={sid}&retry=0",
                           {"CallSid": CALL_SID, "Digits": WRONG})
        assert not _hangs_up(resp)
    resp = signed_post(client, f"/twilio/pin?session_id={sid}&retry=0",
                       {"CallSid": CALL_SID, "Digits": WRONG})
    assert _hangs_up(resp)
    assert PHONE_SESSIONS[sid].pin_attempts == 3
    assert PHONE_SESSIONS[sid].pin_verified is False
    out = capsys.readouterr().out
    assert PIN not in out and WRONG not in out


def test_correct_pin_marks_session_verified(client):
    sid = _session_id(voice(client))
    resp = signed_post(client, f"/twilio/pin?session_id={sid}",
                       {"CallSid": CALL_SID, "Digits": PIN})
    assert resp.status_code == 200
    assert PHONE_SESSIONS[sid].pin_verified is True
    assert "/twilio/gather" in _action(resp)


def test_pin_lockout_spans_calls(client):
    sid = _session_id(voice(client))
    for _ in range(3):
        signed_post(client, f"/twilio/pin?session_id={sid}", {"CallSid": CALL_SID, "Digits": WRONG})
    # A brand-new call from the same number is refused before any prompt.
    resp = voice(client)
    assert _hangs_up(resp) and ElementTree.fromstring(resp.text).find("Gather") is None


def test_pin_webhook_bound_to_call_sid(client):
    sid = _session_id(voice(client))
    resp = signed_post(client, f"/twilio/pin?session_id={sid}",
                       {"CallSid": "CA_other_call", "Digits": PIN})
    assert _hangs_up(resp)
    assert PHONE_SESSIONS[sid].pin_verified is False


@pytest.mark.parametrize("path", ["/twilio/gather", "/twilio/operator"])
def test_menu_webhooks_refuse_unverified_sessions(client, path):
    sid = _session_id(voice(client))
    resp = signed_post(client, f"{path}?session_id={sid}", {"CallSid": CALL_SID, "Digits": "1"})
    assert _hangs_up(resp)
    assert "/twilio/media/" not in resp.text
    assert PHONE_SESSIONS[sid].media_authorized is False


def test_verified_flow_reaches_media_connect(client):
    sid = _session_id(voice(client))
    signed_post(client, f"/twilio/pin?session_id={sid}", {"CallSid": CALL_SID, "Digits": PIN})
    signed_post(client, f"/twilio/gather?session_id={sid}", {"CallSid": CALL_SID, "Digits": "3"})
    resp = signed_post(client, f"/twilio/operator?session_id={sid}",
                       {"CallSid": CALL_SID, "Digits": "2"})
    assert f"/twilio/media/{sid}" in resp.text
    assert PHONE_SESSIONS[sid].operator == "Bravo"
    assert PHONE_SESSIONS[sid].media_authorized is True


# ── media WebSocket bound to the CallSid ────────────────────────────────────

def _verified_session(client):
    sid = _session_id(voice(client))
    signed_post(client, f"/twilio/pin?session_id={sid}", {"CallSid": CALL_SID, "Digits": PIN})
    signed_post(client, f"/twilio/gather?session_id={sid}", {"CallSid": CALL_SID, "Digits": "3"})
    signed_post(client, f"/twilio/operator?session_id={sid}", {"CallSid": CALL_SID, "Digits": "1"})
    return sid


def test_media_ws_rejects_unauthorized_session(client):
    sid = _session_id(voice(client))  # PIN never entered
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/twilio/media/{sid}") as ws:
            ws.send_json({"event": "start", "streamSid": "MZ1", "start": {"callSid": CALL_SID}})
            ws.receive_json()
    assert not FakeBridge.instances


def test_media_ws_rejects_foreign_call_sid(client):
    sid = _verified_session(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/twilio/media/{sid}") as ws:
            ws.send_json({"event": "connected"})
            ws.send_json({"event": "start", "streamSid": "MZ1",
                          "start": {"callSid": "CA_attacker"}})
            ws.receive_json()
    assert not FakeBridge.instances
    # The legitimate call's session is untouched by the rejected socket.
    assert PHONE_SESSIONS[sid].status != PhoneStatus.COMPLETED


def test_media_ws_accepts_its_own_call_once(client):
    sid = _verified_session(client)
    with client.websocket_connect(f"/twilio/media/{sid}") as ws:
        ws.send_json({"event": "start", "streamSid": "MZ1", "start": {"callSid": CALL_SID}})
        ws.send_json({"event": "stop"})
    assert len(FakeBridge.instances) == 1
    assert FakeBridge.instances[0].started_with == "Alpha"
    # A second stream for the same call is refused.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/twilio/media/{sid}") as ws:
            ws.send_json({"event": "start", "streamSid": "MZ2", "start": {"callSid": CALL_SID}})
            ws.receive_json()
    assert len(FakeBridge.instances) == 1
