"""Whitelisted-peer inbound SMS path (M3).

M2 computes the inbound classification (``self`` / ``peer``); M3 CONSUMES it for
the ``peer`` case ONLY -- a non-operator whitelisted sender (e.g. Anna) texting
the operator's AI:

  3.1 -- the ``peer`` signal reaches the model: ``_route_through_chat`` puts
        ``sms_peer=True`` in the posted ``/chat`` payload. A ``self`` inbound
        (the operator texting their own AI) does NOT.
  3.2 -- the operator is NOTIFIED on a peer inbound (``notify`` fires once,
        addressed to the resolved operator, with the sender name + a preview).
        A ``self`` inbound does NOT notify.

(3.3 -- the tasks.py SMS-prefix framing -- is covered in
``Orchestrator/tests/test_sms_peer_framing.py`` because it exercises the
tasks.py prompt builder, not the router.)

Reuses the FakeClient/FakeManager/FakeStore harness shape from
``test_router_precedence``. The notification bus is mocked at the router's
import site (``Orchestrator.sms.router.notify``); the bus itself never raises.
"""
import copy

import pytest

from Orchestrator.sms.router import SMSRouter


# ---------------------------------------------------------------------------
# Fakes (g1: span2 owned by Brandon, span3 unowned)
# ---------------------------------------------------------------------------
class FakeClient:
    def __init__(self):
        self.sent = []

    async def send_sms(self, destination, message, span=2):
        self.sent.append((destination, message, span))
        return {"success": True, "error": None}


class FakeManager:
    def __init__(self):
        self.client = FakeClient()
        self._gateways = {
            "g1": {
                "id": "g1",
                "ports": [
                    {"span": 2, "phone_number": "+14100000000", "operator": "Brandon"},
                    {"span": 3, "phone_number": "+15550000000", "operator": ""},
                ],
            }
        }

    def set_sms_callback(self, cb):
        pass

    def gateways(self):
        return dict(self._gateways)

    def get(self, gateway_id):
        return self.client if gateway_id == "g1" else None

    def default(self):
        return self.client


class FakeStore:
    def __init__(self):
        self.messages = []

    def store_message(self, **kwargs):
        self.messages.append(kwargs)
        return len(self.messages)


def _make_router(monkeypatch, books):
    """Build a router with a hermetic loader pointed at ``books``."""
    import Orchestrator.contacts as contacts_mod
    import Orchestrator.config as config_mod

    monkeypatch.setattr(contacts_mod, "load_contacts", lambda: copy.deepcopy(books))
    monkeypatch.setattr(config_mod, "USERS_LIST", list(books.keys()) or ["Brandon"])

    mgr = FakeManager()
    store = FakeStore()
    r = SMSRouter(mgr, store)
    r._fake_store = store
    r._fake_mgr = mgr
    return r


def _contact(name, phone, *, inbound_allowed=True, is_operator_self=False,
             created_by="user", **extra):
    c = {
        "name": name,
        "phone": phone,
        "inbound_allowed": inbound_allowed,
        "is_operator_self": is_operator_self,
        "created_by": created_by,
    }
    c.update(extra)
    return c


# ---------------------------------------------------------------------------
# A capturing fake for aiohttp.ClientSession so we can inspect the /chat payload
# without a live server. POST returns a task_id; the first GET reports completed.
# ---------------------------------------------------------------------------
async def _noop_sleep(*a, **k):
    return None


async def _noop_notify(*a, **k):
    """No-op notify so payload-capture tests stay hermetic (no real bus, no real
    snapshot mint, no aiohttp leaking from the notify path into the capture)."""
    return None


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload


class _CapturingSession:
    """Records the JSON body posted to /chat; fakes the task poll to completed."""

    posted_payloads = []  # class-level so the test can read it after the call

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, timeout=None):
        _CapturingSession.posted_payloads.append(json)
        return _FakeResp({"task_id": "t-fake-1"})

    def get(self, url, timeout=None):
        return _FakeResp({
            "status": "completed",
            "result_data": {"ui_reply": "ok"},
        })


@pytest.fixture
def capture_chat_payload(monkeypatch):
    """Patch aiohttp.ClientSession used inside _route_through_chat to capture the
    posted /chat payload. Returns the list it accumulates into.

    Also neutralizes the notification bus (``router.notify``) by default so a
    payload-capture test never fires the real bus (which would mint a real
    snapshot AND itself use aiohttp, polluting the capture). Tests that assert ON
    notify override this via the ``fake_notify`` fixture (a recorder), applied
    after this one.
    """
    import aiohttp

    _CapturingSession.posted_payloads = []
    monkeypatch.setattr(aiohttp, "ClientSession", _CapturingSession)
    # No real sleeps in the poll loop.
    import Orchestrator.sms.router as router_mod
    monkeypatch.setattr(router_mod.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(router_mod, "notify", _noop_notify)
    return _CapturingSession.posted_payloads


# ===========================================================================
# 3.1 -- peer flag reaches the model (the /chat payload carries sms_peer=True)
# ===========================================================================
@pytest.mark.asyncio
async def test_peer_inbound_sets_sms_peer_true_in_payload(monkeypatch, capture_chat_payload):
    """Anna (inbound_allowed, NOT operator-self) texts in on an unowned line ->
    the /chat payload carries sms_peer=True."""
    r = _make_router(monkeypatch, {
        "Brandon": {"c1": _contact("Anna", "+2223334444", inbound_allowed=True)},
    })
    await r.handle_incoming(sender="+2223334444", body="hey", span=3,
                            recvtime="2026-06-07 12:00:00", gateway_id="g1")

    assert len(capture_chat_payload) == 1
    payload = capture_chat_payload[0]
    assert payload["sms_peer"] is True
    # The existing keys must still be present (additive, not a restructure).
    assert payload["sms_mode"] is True
    assert payload["sms_sender"] == "+2223334444"
    assert payload["sms_contact_name"] == "Anna"
    assert payload["operator"] == "Brandon"


@pytest.mark.asyncio
async def test_self_inbound_does_not_set_sms_peer(monkeypatch, capture_chat_payload):
    """The operator texting their OWN AI (is_operator_self) is NOT a peer ->
    sms_peer omitted/False."""
    r = _make_router(monkeypatch, {
        "Brandon": {"c1": _contact("Me", "+13335557777",
                                   inbound_allowed=True, is_operator_self=True)},
    })
    await r.handle_incoming(sender="+13335557777", body="hi me", span=3,
                            recvtime="2026-06-07 12:00:00", gateway_id="g1")

    assert len(capture_chat_payload) == 1
    payload = capture_chat_payload[0]
    assert payload.get("sms_peer", False) is False


@pytest.mark.asyncio
async def test_route_through_chat_is_peer_param_controls_payload(monkeypatch, capture_chat_payload):
    """Directly: _route_through_chat threads an is_peer flag into the payload."""
    r = _make_router(monkeypatch, {
        "Brandon": {"c1": _contact("Anna", "+2223334444")},
    })

    await r._route_through_chat("+2223334444", "hey", "Brandon", "Anna", is_peer=True)
    assert capture_chat_payload[-1]["sms_peer"] is True

    await r._route_through_chat("+2223334444", "hey", "Brandon", "Anna", is_peer=False)
    assert capture_chat_payload[-1].get("sms_peer", False) is False


# ===========================================================================
# 3.2 -- operator notified on a peer inbound (notify fires once); self does not
# ===========================================================================
class _NotifyRecorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, operator, title, body, category="general", **kwargs):
        self.calls.append({
            "operator": operator, "title": title, "body": body,
            "category": category, "kwargs": kwargs,
        })
        return None  # bus returns a NotifyResult; the router ignores it


@pytest.fixture
def fake_notify(monkeypatch):
    rec = _NotifyRecorder()
    import Orchestrator.sms.router as router_mod
    monkeypatch.setattr(router_mod, "notify", rec)
    return rec


@pytest.mark.asyncio
async def test_peer_inbound_notifies_operator(monkeypatch, capture_chat_payload, fake_notify):
    r = _make_router(monkeypatch, {
        "Brandon": {"c1": _contact("Anna", "+2223334444", inbound_allowed=True)},
    })
    await r.handle_incoming(sender="+2223334444", body="can you call me?", span=3,
                            recvtime="2026-06-07 12:00:00", gateway_id="g1")

    assert len(fake_notify.calls) == 1
    call = fake_notify.calls[0]
    assert call["operator"] == "Brandon"          # addressed to the resolved operator
    assert "Anna" in call["title"]                 # sender name in the title
    assert "can you call me?" in call["body"]      # message preview in the body


@pytest.mark.asyncio
async def test_self_inbound_does_not_notify(monkeypatch, capture_chat_payload, fake_notify):
    r = _make_router(monkeypatch, {
        "Brandon": {"c1": _contact("Me", "+13335557777",
                                   inbound_allowed=True, is_operator_self=True)},
    })
    await r.handle_incoming(sender="+13335557777", body="note to self", span=3,
                            recvtime="2026-06-07 12:00:00", gateway_id="g1")

    assert fake_notify.calls == []


@pytest.mark.asyncio
async def test_dropped_inbound_does_not_notify(monkeypatch, capture_chat_payload, fake_notify):
    """An unknown (dropped) sender never reaches the notify hook."""
    r = _make_router(monkeypatch, {"Brandon": {}})
    await r.handle_incoming(sender="+9999999999", body="spam", span=3,
                            recvtime="2026-06-07 12:00:00", gateway_id="g1")
    assert fake_notify.calls == []


@pytest.mark.asyncio
async def test_peer_notify_body_is_truncated(monkeypatch, capture_chat_payload, fake_notify):
    """A long inbound is previewed (truncated) in the notification body, not sent whole."""
    long_body = "x" * 1000
    r = _make_router(monkeypatch, {
        "Brandon": {"c1": _contact("Anna", "+2223334444", inbound_allowed=True)},
    })
    await r.handle_incoming(sender="+2223334444", body=long_body, span=3,
                            recvtime="2026-06-07 12:00:00", gateway_id="g1")

    assert len(fake_notify.calls) == 1
    assert len(fake_notify.calls[0]["body"]) < len(long_body)
