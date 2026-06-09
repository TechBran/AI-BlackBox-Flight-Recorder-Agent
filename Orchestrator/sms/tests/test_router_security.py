"""Line-aware inbound SMS routing — whitelist security invariants (Task 3.2).

The whitelist is ABSOLUTE: an unknown sender must be dropped BEFORE any
chat/task/send/store. Line scoping only TIGHTENS security — when the line
(the gateway port that received the SMS) has an owner, the sender is matched
ONLY against that owner's contact book; a sender whitelisted for some OTHER
operator is dropped. An unowned line falls back to searching all books.

Hermetic: fake manager (gateways()/get()/default()), fake store, stubbed
contact loader (monkeypatched), and a stubbed _route_through_chat that records
whether it was called.
"""
import pytest

from Orchestrator.sms.router import SMSRouter


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeClient:
    def __init__(self):
        self.sent = []  # (destination, message, span)

    async def send_sms(self, destination, message, span=2):
        self.sent.append((destination, message, span))
        return {"success": True, "error": None}


class FakeManager:
    """Returns a single gateway g1 with two ports (owned + unowned line)."""

    def __init__(self):
        self.client = FakeClient()
        self._gateways = {
            "g1": {
                "id": "g1",
                "ports": [
                    {"span": 2, "phone_number": "+14100000000A", "operator": "Brandon"},
                    {"span": 3, "phone_number": "+15550000000B", "operator": ""},
                ],
            }
        }

    def set_sms_callback(self, cb):
        # Router registers itself here on construction; nothing to fan out.
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


# Contact books used by the stubbed loader.
_CONTACTS = {
    "Brandon": {
        "c1": {"name": "Bob", "phone": "+1111111111"},
    },
    "Alice": {
        "c2": {"name": "Carol", "phone": "+2222222222"},
    },
}


@pytest.fixture
def router(monkeypatch):
    """Build a router with fakes and a hermetic contact loader."""
    import Orchestrator.contacts as contacts_mod
    import Orchestrator.config as config_mod

    monkeypatch.setattr(contacts_mod, "load_contacts", lambda: {
        op: dict(book) for op, book in _CONTACTS.items()
    })
    monkeypatch.setattr(contacts_mod, "ensure_operator_book", lambda data, op: False)
    monkeypatch.setattr(config_mod, "USERS_LIST", list(_CONTACTS.keys()))

    mgr = FakeManager()
    store = FakeStore()
    r = SMSRouter(mgr, store)

    # Record chat calls without hitting the real pipeline.
    calls = []

    async def fake_chat(sender, body, operator, contact_name):
        calls.append({"sender": sender, "body": body, "operator": operator,
                      "contact_name": contact_name})
        return "reply"

    monkeypatch.setattr(r, "_route_through_chat", fake_chat)
    r._chat_calls = calls
    r._fake_store = store
    r._fake_mgr = mgr
    return r


# ---------------------------------------------------------------------------
# Security invariants
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unknown_sender_dropped_before_chat(router):
    # Unknown sender on an OWNED line (span2, owner Brandon).
    await router.handle_incoming(
        sender="+9999999999", body="spam", span=2,
        recvtime="2026-06-07 12:00:00", gateway_id="g1",
    )
    assert router._chat_calls == []           # chat never invoked
    assert router._fake_mgr.client.sent == []  # nothing sent
    assert router._fake_store.messages == []   # nothing stored


@pytest.mark.asyncio
async def test_owner_line_sender_in_owner_book_routes(router):
    # Bob (+1111111111) is in Brandon's book; line span2 owner=Brandon.
    await router.handle_incoming(
        sender="+1111111111", body="hi", span=2,
        recvtime="2026-06-07 12:00:00", gateway_id="g1",
    )
    assert len(router._chat_calls) == 1
    assert router._chat_calls[0]["operator"] == "Brandon"

    inbound = [m for m in router._fake_store.messages if m["direction"] == "inbound"]
    assert len(inbound) == 1
    # Stored with the resolved line number (span2 phone) and gateway id.
    assert inbound[0]["line_number"] == "+14100000000A"
    assert inbound[0]["gateway_id"] == "g1"


@pytest.mark.asyncio
async def test_owner_line_sender_in_other_book_dropped(router):
    # KEY TIGHTENING: Carol (+2222222222) is in Alice's book, NOT Brandon's.
    # On Brandon's dedicated line (span2) she must be DROPPED.
    await router.handle_incoming(
        sender="+2222222222", body="hello", span=2,
        recvtime="2026-06-07 12:00:00", gateway_id="g1",
    )
    assert router._chat_calls == []
    assert router._fake_mgr.client.sent == []
    assert router._fake_store.messages == []


@pytest.mark.asyncio
async def test_unowned_line_falls_back_to_all_books(router):
    # span3 has no owner -> search ALL books; Carol is in Alice's.
    await router.handle_incoming(
        sender="+2222222222", body="hi", span=3,
        recvtime="2026-06-07 12:00:00", gateway_id="g1",
    )
    assert len(router._chat_calls) == 1
    assert router._chat_calls[0]["operator"] == "Alice"

    inbound = [m for m in router._fake_store.messages if m["direction"] == "inbound"]
    assert len(inbound) == 1
    assert inbound[0]["line_number"] == "+15550000000B"
    assert inbound[0]["gateway_id"] == "g1"


@pytest.mark.asyncio
async def test_unowned_line_unknown_sender_dropped(router):
    # span3 no owner, sender in nobody's book -> dropped.
    await router.handle_incoming(
        sender="+9999999999", body="spam", span=3,
        recvtime="2026-06-07 12:00:00", gateway_id="g1",
    )
    assert router._chat_calls == []
    assert router._fake_mgr.client.sent == []
    assert router._fake_store.messages == []
