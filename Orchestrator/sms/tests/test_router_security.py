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
    """Build a router with fakes and a hermetic contact loader.

    The stub mirrors production ``load_contacts`` by applying the same read-time
    inbound-SMS migration defaults (``_apply_inbound_defaults``) the real loader
    guarantees: a flag-less legacy contact reads back ``inbound_allowed=True``.
    Without this, the M2 resolver — which gates on ``inbound_allowed`` — would
    see the flag absent and wrongly drop a whitelisted sender.
    """
    import copy
    import Orchestrator.contacts as contacts_mod
    import Orchestrator.config as config_mod

    monkeypatch.setattr(contacts_mod, "load_contacts", lambda: contacts_mod._apply_inbound_defaults(
        {op: copy.deepcopy(book) for op, book in _CONTACTS.items()}
    ))
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


# ---------------------------------------------------------------------------
# System/self seed contact must NEVER satisfy the inbound whitelist.
#
# ensure_operator_book() auto-injects SEED_CONTACT (the fixed, spoofable system
# number +17164512527, created_by 'system', tags ['system','self']) into any
# bookless operator. The whitelist lookup must be READ-ONLY (no book
# fabrication) and must skip any system/self seed contact, so the seed number
# can never reach the chat pipeline. A real operator-added self contact
# (created_by a user, no 'system' tag) must still route.
# ---------------------------------------------------------------------------
SEED_NUMBER = "+17164512527"

# A faithful copy of the auto-injected seed contact, as it would land on disk.
SEED_ON_DISK = {
    "id": "seedid",
    "name": "AI BlackBox Flight Recorder",
    "phone": SEED_NUMBER,
    "email": "brandon@aiblackboxfc.com",
    "relationship": "self",
    "notes": "This is your own phone number. The AI BlackBox system number.",
    "tags": ["system", "self"],
    "created_by": "system",
}


def _patch_contacts(monkeypatch, books):
    """Point the hermetic loader at `books` (deep-copied per call).

    Applies the production read-time inbound-SMS defaults so flag-less legacy
    contacts behave exactly as the real ``load_contacts`` returns them.
    """
    import copy
    import Orchestrator.contacts as contacts_mod
    import Orchestrator.config as config_mod

    monkeypatch.setattr(contacts_mod, "load_contacts",
                        lambda: contacts_mod._apply_inbound_defaults(copy.deepcopy(books)))
    monkeypatch.setattr(config_mod, "USERS_LIST", list(books.keys()) or ["Brandon"])


@pytest.mark.asyncio
async def test_seed_number_dropped_on_owned_bookless_line(router, monkeypatch):
    # Owner "Brandon" has NO contacts (bookless). The seed number arriving on
    # Brandon's owned line (span2) must be DROPPED — the lookup must not
    # fabricate a seed book to whitelist it.
    _patch_contacts(monkeypatch, {"Brandon": {}})
    await router.handle_incoming(
        sender=SEED_NUMBER, body="spoofed", span=2,
        recvtime="2026-06-07 12:00:00", gateway_id="g1",
    )
    assert router._chat_calls == []
    assert router._fake_mgr.client.sent == []
    assert router._fake_store.messages == []


@pytest.mark.asyncio
async def test_seed_contact_on_disk_does_not_whitelist(router, monkeypatch):
    # The seed contact persisted to disk in Brandon's book must NOT whitelist
    # the seed number on Brandon's owned line (span2). It is skipped.
    _patch_contacts(monkeypatch, {"Brandon": {"seedid": dict(SEED_ON_DISK)}})
    await router.handle_incoming(
        sender=SEED_NUMBER, body="spoofed", span=2,
        recvtime="2026-06-07 12:00:00", gateway_id="g1",
    )
    assert router._chat_calls == []
    assert router._fake_mgr.client.sent == []
    assert router._fake_store.messages == []


@pytest.mark.asyncio
async def test_real_self_contact_still_routes(router, monkeypatch):
    # A REAL self contact the operator added themselves (created_by a user, no
    # 'system' tag) must still match — operator-texts-their-own-AI preserved.
    _patch_contacts(monkeypatch, {
        "Brandon": {
            "c1": {
                "name": "Me",
                "phone": "+13335557777",
                "relationship": "self",
                "created_by": "brandon",
                "tags": ["self"],
            }
        }
    })
    await router.handle_incoming(
        sender="+13335557777", body="hi me", span=2,
        recvtime="2026-06-07 12:00:00", gateway_id="g1",
    )
    assert len(router._chat_calls) == 1
    assert router._chat_calls[0]["operator"] == "Brandon"


@pytest.mark.asyncio
async def test_seed_number_dropped_all_books(router, monkeypatch):
    # Unowned line (span3) -> all-books pass. Operators are bookless or only
    # hold the system seed. The seed number must be DROPPED.
    _patch_contacts(monkeypatch, {
        "Brandon": {},
        "Alice": {"seedid": dict(SEED_ON_DISK)},
    })
    await router.handle_incoming(
        sender=SEED_NUMBER, body="spoofed", span=3,
        recvtime="2026-06-07 12:00:00", gateway_id="g1",
    )
    assert router._chat_calls == []
    assert router._fake_mgr.client.sent == []
    assert router._fake_store.messages == []
