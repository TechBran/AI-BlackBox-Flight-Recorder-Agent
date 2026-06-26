"""Multi-gateway / line-aware SMSRouter tests.

Uses the FakeClient from test_manager plus a fake message store. No real
sockets, no real chat pipeline — `_route_through_chat` and
`_find_operator_by_phone` are stubbed per test.
"""
import pytest

from Orchestrator.sms.manager import AMIConnectionManager
from Orchestrator.sms.router import SMSRouter, Resolution


@pytest.fixture(autouse=True)
def _silence_notify(monkeypatch):
    """M3: neutralize the peer-inbound notification bus across this module so the
    peer-classified routing tests never fire the real notify() (which would mint
    a snapshot). Peer notification is asserted in test_sms_peer_path."""
    async def _noop(*a, **k):
        return None
    import Orchestrator.sms.router as _router_mod
    monkeypatch.setattr(_router_mod, "notify", _noop)

from Orchestrator.sms.tests.test_manager import FakeClient, _gw


class FakeStore:
    def __init__(self):
        self.messages = []

    def store_message(self, **kwargs):
        self.messages.append(kwargs)
        return len(self.messages)


@pytest.mark.asyncio
async def test_router_registers_via_manager_callback():
    m = AMIConnectionManager(client_factory=FakeClient)
    await m.add_gateway(_gw("a", "10.0.0.1"))
    router = SMSRouter(m, FakeStore())
    # The router's handle_incoming must be registered on the existing client.
    assert router.handle_incoming in m.get("a").sms_callbacks


@pytest.mark.asyncio
async def test_inbound_reply_goes_out_same_gateway(monkeypatch):
    m = AMIConnectionManager(client_factory=FakeClient)
    await m.add_gateway(_gw("a", "10.0.0.1"))
    await m.add_gateway(_gw("b", "10.0.0.2"))
    store = FakeStore()
    router = SMSRouter(m, store)

    # Known sender -> matched operator. The inbound path now resolves via the
    # 5-tier resolve_inbound seam (not _find_operator_by_phone, which is the
    # outbound send_manual contact-name lookup).
    monkeypatch.setattr(
        router, "resolve_inbound",
        lambda sender, owner: Resolution("Brandon", {"name": "Alice", "phone": sender}, "peer"),
    )

    async def fake_chat(sender, body, operator, contact_name, **kwargs):
        return "hello back"

    monkeypatch.setattr(router, "_route_through_chat", fake_chat)

    # Inbound arrives via gateway A's client callback, span 5.
    cb = m.get("a").sms_callbacks[0]
    await cb("+14105550000", "hi", "5", "2026-06-07 12:00:00", "a")

    # Reply must go out gateway A (the receiver), NOT B, on the same span.
    assert m.get("a").sent == [("+14105550000", "hello back", 5)]
    assert m.get("b").sent == []


@pytest.mark.asyncio
async def test_inbound_unknown_sender_dropped_before_chat(monkeypatch):
    m = AMIConnectionManager(client_factory=FakeClient)
    await m.add_gateway(_gw("a", "10.0.0.1"))
    store = FakeStore()
    router = SMSRouter(m, store)

    # Unknown sender -> no operator.
    monkeypatch.setattr(
        router, "_find_operator_by_phone", lambda phone: (None, None)
    )

    called = {"chat": False}

    async def fake_chat(*args, **kwargs):
        called["chat"] = True
        return "should not happen"

    monkeypatch.setattr(router, "_route_through_chat", fake_chat)

    cb = m.get("a").sms_callbacks[0]
    await cb("+19998887777", "spam", "2", "2026-06-07 12:00:00", "a")

    # Whitelist gate: chat never invoked, nothing sent, nothing stored.
    assert called["chat"] is False
    assert m.get("a").sent == []
    assert store.messages == []


@pytest.mark.asyncio
async def test_inbound_reply_falls_back_to_default_when_gateway_unknown(monkeypatch):
    m = AMIConnectionManager(client_factory=FakeClient)
    await m.add_gateway(_gw("a", "10.0.0.1"))
    store = FakeStore()
    router = SMSRouter(m, store)

    monkeypatch.setattr(
        router, "resolve_inbound",
        lambda sender, owner: Resolution("Brandon", {"name": "Alice", "phone": sender}, "peer"),
    )

    async def fake_chat(*args, **kwargs):
        return "ok"

    monkeypatch.setattr(router, "_route_through_chat", fake_chat)

    # gateway_id None -> fall back to default() == gateway A.
    await router.handle_incoming("+14105550000", "hi", "2", "2026-06-07 12:00:00", None)
    assert m.get("a").sent == [("+14105550000", "ok", 2)]


@pytest.mark.asyncio
async def test_send_manual_default_gateway(monkeypatch):
    m = AMIConnectionManager(client_factory=FakeClient)
    await m.add_gateway(_gw("a", "10.0.0.1"))
    store = FakeStore()
    router = SMSRouter(m, store)
    monkeypatch.setattr(
        router, "_find_operator_by_phone", lambda phone: (None, None)
    )

    res = await router.send_manual("Brandon", "+14105550000", "manual msg")
    assert res["success"] is True
    assert m.get("a").sent == [("+14105550000", "manual msg", 2)]
    # No from_number resolved -> line_number "", gateway_id from chosen client.
    msg = store.messages[0]
    assert msg["line_number"] == ""
    assert msg["gateway_id"] == "a"


@pytest.mark.asyncio
async def test_send_manual_resolves_from_number(monkeypatch):
    m = AMIConnectionManager(client_factory=FakeClient)
    await m.add_gateway(_gw("a", "10.0.0.1", numbers=["+14105551111", "+14105552222"]))
    await m.add_gateway(_gw("b", "10.0.0.2", numbers=["+14105553333", "+14105554444"]))
    store = FakeStore()
    router = SMSRouter(m, store)
    monkeypatch.setattr(
        router, "_find_operator_by_phone", lambda phone: (None, None)
    )

    # from_number owned by gateway B slot 1 -> span 3.
    res = await router.send_manual(
        "Brandon", "+14109990000", "hey", from_number="+14105554444"
    )
    assert res["success"] is True
    assert m.get("b").sent == [("+14109990000", "hey", 3)]
    assert m.get("a").sent == []
    # Outbound stored tagged with the resolved line + chosen gateway.
    assert len(store.messages) == 1
    msg = store.messages[0]
    assert msg["direction"] == "outbound"
    assert msg["line_number"] == "+14105554444"
    assert msg["gateway_id"] == "b"


@pytest.mark.asyncio
async def test_send_manual_explicit_gateway_id(monkeypatch):
    m = AMIConnectionManager(client_factory=FakeClient)
    await m.add_gateway(_gw("a", "10.0.0.1", numbers=["+14105551111", "+14105552222"]))
    await m.add_gateway(_gw("b", "10.0.0.2", numbers=["+14105553333", "+14105554444"]))
    store = FakeStore()
    router = SMSRouter(m, store)
    monkeypatch.setattr(
        router, "_find_operator_by_phone", lambda phone: (None, None)
    )

    # Explicit gateway_id "b" -> that client (default span 2 when unresolved).
    res = await router.send_manual(
        "Brandon", "+14109990000", "via b", gateway_id="b"
    )
    assert res["success"] is True
    assert m.get("b").sent == [("+14109990000", "via b", 2)]
    assert m.get("a").sent == []
    msg = store.messages[0]
    assert msg["gateway_id"] == "b"


@pytest.mark.asyncio
async def test_send_manual_no_gateway_available(monkeypatch):
    m = AMIConnectionManager(client_factory=FakeClient)  # no gateways added
    store = FakeStore()
    router = SMSRouter(m, store)
    res = await router.send_manual("Brandon", "+14105550000", "msg")
    assert res["success"] is False
    assert "No gateway available" in res["error"]
