"""Deterministic 5-tier inbound operator resolution (M2).

Replaces the Pass1/Pass2 string-sniffing with an explicit, ordered, fully-logged
resolver. Resolution order (first match wins):

  1. Line ownership   — owned line: sender must be inbound_allowed in the OWNER's
                        book -> owner; else DROP (owned lines are strict).
  2. Operator-identity — unowned line: sender is is_operator_self in some book ->
                        that operator (their own line). Multiple self-flags ->
                        most-recently-updated + WARNING.
  3. Single whitelist  — unowned line: sender inbound_allowed in exactly one book.
  4. Multi-match       — unowned line: inbound_allowed in several books ->
                        most-recently-updated + WARNING naming all candidates.
  5. No match          — DROP (nothing stored).

The resolver returns (operator, contact, classification) where classification is
``self`` (is_operator_self contact) or ``peer`` (inbound_allowed non-self).

Reuses the FakeClient/FakeManager/FakeStore harness from test_router_security.
Books here are explicit about the two M1 flags so the loader's read-time defaults
are not load-bearing for the assertions.
"""
import copy

import pytest

from Orchestrator.sms.router import SMSRouter


# ---------------------------------------------------------------------------
# Fakes (mirror test_router_security's harness; g1 has an owned + unowned line)
# ---------------------------------------------------------------------------
class FakeClient:
    def __init__(self):
        self.sent = []

    async def send_sms(self, destination, message, span=2):
        self.sent.append((destination, message, span))
        return {"success": True, "error": None}


class FakeManager:
    """g1: span2 owned by Brandon, span3 unowned."""

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

    monkeypatch.setattr(contacts_mod, "load_contacts",
                        lambda: copy.deepcopy(books))
    monkeypatch.setattr(config_mod, "USERS_LIST", list(books.keys()) or ["Brandon"])

    mgr = FakeManager()
    store = FakeStore()
    r = SMSRouter(mgr, store)

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


def _contact(name, phone, *, inbound_allowed=True, is_operator_self=False,
             created_by="user", updated_at=None, created_at=None, **extra):
    c = {
        "name": name,
        "phone": phone,
        "inbound_allowed": inbound_allowed,
        "is_operator_self": is_operator_self,
        "created_by": created_by,
    }
    if updated_at is not None:
        c["updated_at"] = updated_at
    if created_at is not None:
        c["created_at"] = created_at
    c.update(extra)
    return c


# ===========================================================================
# Tier 1 — Line ownership (owned line is STRICT)
# ===========================================================================
@pytest.mark.asyncio
async def test_tier1_owned_line_inbound_allowed_routes_to_owner(monkeypatch):
    """Owned line (span2 -> Brandon) + sender inbound_allowed in Brandon's book."""
    r = _make_router(monkeypatch, {
        "Brandon": {"c1": _contact("Bob", "+1111111111", inbound_allowed=True)},
    })
    res = r.resolve_inbound("+1111111111", owner="Brandon")
    assert res.operator == "Brandon"
    assert res.classification == "peer"

    await r.handle_incoming(sender="+1111111111", body="hi", span=2,
                            recvtime="2026-06-07 12:00:00", gateway_id="g1")
    assert len(r._chat_calls) == 1
    assert r._chat_calls[0]["operator"] == "Brandon"


@pytest.mark.asyncio
async def test_tier1_owned_line_not_inbound_allowed_drops(monkeypatch):
    """Owned line + sender present in owner's book but inbound_allowed=False -> DROP."""
    r = _make_router(monkeypatch, {
        "Brandon": {"c1": _contact("Bob", "+1111111111", inbound_allowed=False)},
    })
    res = r.resolve_inbound("+1111111111", owner="Brandon")
    assert res.operator is None

    await r.handle_incoming(sender="+1111111111", body="hi", span=2,
                            recvtime="2026-06-07 12:00:00", gateway_id="g1")
    assert r._chat_calls == []
    assert r._fake_store.messages == []


@pytest.mark.asyncio
async def test_tier1_owned_line_sender_only_in_other_book_drops(monkeypatch):
    """Owned line is strict: sender whitelisted for ANOTHER operator -> DROP
    (does NOT fall through to that other operator)."""
    r = _make_router(monkeypatch, {
        "Brandon": {},
        "Alice": {"c2": _contact("Carol", "+2222222222", inbound_allowed=True)},
    })
    res = r.resolve_inbound("+2222222222", owner="Brandon")
    assert res.operator is None

    await r.handle_incoming(sender="+2222222222", body="hi", span=2,
                            recvtime="2026-06-07 12:00:00", gateway_id="g1")
    assert r._chat_calls == []
    assert r._fake_store.messages == []


@pytest.mark.asyncio
async def test_tier1_owned_line_self_contact_classifies_self(monkeypatch):
    """Owned line + the owner's own self-flagged contact -> self classification."""
    r = _make_router(monkeypatch, {
        "Brandon": {"c1": _contact("Me", "+13335557777",
                                    inbound_allowed=True, is_operator_self=True)},
    })
    res = r.resolve_inbound("+13335557777", owner="Brandon")
    assert res.operator == "Brandon"
    assert res.classification == "self"


@pytest.mark.asyncio
async def test_tier1_owned_line_self_identity_bypasses_inbound_allowed(monkeypatch):
    """Tier1/tier2 symmetry: an operator's OWN self-contact reaches their OWN
    dedicated line even with inbound_allowed=False. Identity is independent of
    the inbound whitelist on BOTH paths ("your own line always reaches you"). M5
    exposes both flags as independent toggles, so "IS operator on + Allow inbound
    off" must NOT lock the operator out of their own line."""
    r = _make_router(monkeypatch, {
        "Brandon": {"c1": _contact("Me", "+13335557777",
                                    inbound_allowed=False, is_operator_self=True)},
    })
    res = r.resolve_inbound("+13335557777", owner="Brandon")
    assert res.operator == "Brandon"
    assert res.contact["name"] == "Me"
    assert res.classification == "self"


# ===========================================================================
# Tier 2 — Operator-identity (unowned line)
# ===========================================================================
@pytest.mark.asyncio
async def test_tier2_identity_wins_even_when_plain_contact_elsewhere(monkeypatch):
    """Unowned line: sender is is_operator_self in Brandon's book AND a plain
    inbound_allowed contact in Alice's book -> identity (Brandon) wins."""
    r = _make_router(monkeypatch, {
        "Brandon": {"c1": _contact("Brandon", "+14108166914",
                                   inbound_allowed=True, is_operator_self=True)},
        "Alice": {"c2": _contact("Brandon", "+14108166914", inbound_allowed=True)},
    })
    res = r.resolve_inbound("+14108166914", owner=None)
    assert res.operator == "Brandon"
    assert res.classification == "self"

    await r.handle_incoming(sender="+14108166914", body="hi", span=3,
                            recvtime="2026-06-07 12:00:00", gateway_id="g1")
    assert len(r._chat_calls) == 1
    assert r._chat_calls[0]["operator"] == "Brandon"


@pytest.mark.asyncio
async def test_tier2_multiple_self_flags_most_recent_wins_and_warns(monkeypatch, caplog):
    """Defensive: two operators self-flag the same number -> most-recently-updated
    wins + a WARNING naming all candidates."""
    r = _make_router(monkeypatch, {
        "Brandon": {"c1": _contact("Me", "+14108166914", is_operator_self=True,
                                   updated_at="2026-01-01T00:00:00+00:00")},
        "Alice": {"c2": _contact("Me", "+14108166914", is_operator_self=True,
                                 updated_at="2026-05-01T00:00:00+00:00")},
    })
    import logging
    with caplog.at_level(logging.WARNING, logger="sms.router"):
        res = r.resolve_inbound("+14108166914", owner=None)
    assert res.operator == "Alice"  # most-recently-updated
    assert res.classification == "self"
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert warnings, "expected a collision WARNING"
    blob = " ".join(rec.getMessage() for rec in warnings)
    assert "Brandon" in blob and "Alice" in blob


# ===========================================================================
# Tier 3 — Single inbound_allowed match (unowned line)
# ===========================================================================
@pytest.mark.asyncio
async def test_tier3_single_whitelist_match(monkeypatch):
    """Unowned line: sender inbound_allowed in exactly one book -> that operator,
    classified peer (the 'Anna texts in' case)."""
    r = _make_router(monkeypatch, {
        "Brandon": {},
        "Alice": {"c2": _contact("Anna", "+2222222222", inbound_allowed=True)},
    })
    res = r.resolve_inbound("+2222222222", owner=None)
    assert res.operator == "Alice"
    assert res.classification == "peer"

    await r.handle_incoming(sender="+2222222222", body="hi", span=3,
                            recvtime="2026-06-07 12:00:00", gateway_id="g1")
    assert len(r._chat_calls) == 1
    assert r._chat_calls[0]["operator"] == "Alice"


@pytest.mark.asyncio
async def test_tier3_not_inbound_allowed_is_not_a_match(monkeypatch):
    """A contact with inbound_allowed=False is NOT a whitelist match."""
    r = _make_router(monkeypatch, {
        "Alice": {"c2": _contact("Anna", "+2222222222", inbound_allowed=False)},
    })
    res = r.resolve_inbound("+2222222222", owner=None)
    assert res.operator is None


# ===========================================================================
# Tier 4 — Multi-match collision (unowned line)
# ===========================================================================
@pytest.mark.asyncio
async def test_tier4_multi_match_most_recent_wins_and_warns(monkeypatch, caplog):
    """Unowned line: inbound_allowed in TWO books, no self-flag -> most-recently-
    updated wins + WARNING naming all candidate operators."""
    r = _make_router(monkeypatch, {
        "Brandon": {"c1": _contact("X", "+2222222222", inbound_allowed=True,
                                   updated_at="2026-01-01T00:00:00+00:00")},
        "Alice": {"c2": _contact("X", "+2222222222", inbound_allowed=True,
                                 updated_at="2026-06-01T00:00:00+00:00")},
    })
    import logging
    with caplog.at_level(logging.WARNING, logger="sms.router"):
        res = r.resolve_inbound("+2222222222", owner=None)
    assert res.operator == "Alice"  # most-recently-updated
    assert res.classification == "peer"
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert warnings, "expected a collision WARNING"
    blob = " ".join(rec.getMessage() for rec in warnings)
    assert "Brandon" in blob and "Alice" in blob


@pytest.mark.asyncio
async def test_tier4_no_timestamp_falls_back_to_users_list_order_still_warns(
        monkeypatch, caplog):
    """Multi-match with NO updated_at/created_at -> USERS_LIST order tiebreak
    (first listed wins), but the WARNING still fires."""
    r = _make_router(monkeypatch, {
        "Brandon": {"c1": _contact("X", "+2222222222", inbound_allowed=True)},
        "Alice": {"c2": _contact("X", "+2222222222", inbound_allowed=True)},
    })
    import logging
    with caplog.at_level(logging.WARNING, logger="sms.router"):
        res = r.resolve_inbound("+2222222222", owner=None)
    assert res.operator == "Brandon"  # USERS_LIST order: Brandon before Alice
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert warnings, "expected a collision WARNING even without timestamps"


# ===========================================================================
# Tier 5 — No match
# ===========================================================================
@pytest.mark.asyncio
async def test_tier5_no_match_drops_nothing_stored(monkeypatch):
    r = _make_router(monkeypatch, {
        "Brandon": {"c1": _contact("Bob", "+1111111111", inbound_allowed=True)},
    })
    res = r.resolve_inbound("+9999999999", owner=None)
    assert res.operator is None
    assert res.contact is None
    assert res.classification is None

    await r.handle_incoming(sender="+9999999999", body="spam", span=3,
                            recvtime="2026-06-07 12:00:00", gateway_id="g1")
    assert r._chat_calls == []
    assert r._fake_mgr.client.sent == []
    assert r._fake_store.messages == []


# ===========================================================================
# Task 2.2 — System-seed fix
# ===========================================================================
SEED_NUMBER = "+17164512527"


@pytest.mark.asyncio
async def test_seed_phone_cannot_whitelist_itself_even_self_flagged(monkeypatch):
    """The literal seed phone is gated regardless of created_by / is_operator_self."""
    r = _make_router(monkeypatch, {
        "Brandon": {"seedid": _contact("AI BlackBox Flight Recorder", SEED_NUMBER,
                                       inbound_allowed=True, is_operator_self=True,
                                       created_by="system")},
    })
    res = r.resolve_inbound(SEED_NUMBER, owner=None)
    assert res.operator is None

    # And on the owned line.
    res2 = r.resolve_inbound(SEED_NUMBER, owner="Brandon")
    assert res2.operator is None


@pytest.mark.asyncio
async def test_created_by_system_real_contact_now_resolves(monkeypatch):
    """A REAL operator contact with created_by='system' + is_operator_self=True
    (NOT the seed phone) now resolves — Anna's latent bug is fixed. Previously
    _is_system_seed excluded ALL created_by='system' contacts."""
    r = _make_router(monkeypatch, {
        "Anna": {"c1": _contact("Brandon", "+14108166914",
                                inbound_allowed=True, is_operator_self=True,
                                created_by="system")},
    })
    res = r.resolve_inbound("+14108166914", owner=None)
    assert res.operator == "Anna"
    assert res.classification == "self"


# ===========================================================================
# Task 2.3 — Symmetric Anna/Brandon regression (live +14108166914 3-book shape)
# ===========================================================================
def _live_three_book_shape():
    """Mirror the real disk shape of +14108166914 (Brandon owner / Anna husband /
    system owner), plus Brandon's own self-flag so identity binds to Brandon."""
    num = "+14108166914"
    return {
        "Brandon": {
            "b1": _contact("Brandon", num, inbound_allowed=True,
                           is_operator_self=True, created_by="Brandon",
                           relationship="owner", tags=["owner", "vip", "primary"],
                           updated_at="2026-02-11T01:11:46+00:00"),
        },
        "Anna": {
            "a1": _contact("Brandon", "14108166914", inbound_allowed=True,
                           is_operator_self=False, created_by="Anna",
                           relationship="husband king", tags=[],
                           updated_at="2026-03-26T02:07:05+00:00"),
            # Anna's OWN line (her self identity)
            "a2": _contact("Anna", "+13015550100", inbound_allowed=True,
                           is_operator_self=True, created_by="Anna",
                           relationship="self",
                           updated_at="2026-03-26T02:07:05+00:00"),
        },
        "system": {
            "s1": _contact("Brandon", num, inbound_allowed=True,
                           is_operator_self=False, created_by="system",
                           relationship="owner",
                           tags=["owner", "operator", "vip", "developer"],
                           updated_at="2026-02-05T22:12:56+00:00"),
        },
    }


@pytest.mark.asyncio
async def test_symmetric_inbound_from_brandon_phone_resolves_brandon(monkeypatch):
    """Inbound from Brandon's phone -> Brandon via identity (tier2), regardless of
    being cross-listed in Anna's and system's books."""
    r = _make_router(monkeypatch, _live_three_book_shape())
    res = r.resolve_inbound("+14108166914", owner=None)
    assert res.operator == "Brandon"
    assert res.classification == "self"


@pytest.mark.asyncio
async def test_symmetric_inbound_from_anna_phone_resolves_anna(monkeypatch):
    """Inbound from Anna's own phone -> Anna via identity (tier2)."""
    r = _make_router(monkeypatch, _live_three_book_shape())
    res = r.resolve_inbound("+13015550100", owner=None)
    assert res.operator == "Anna"
    assert res.classification == "self"


@pytest.mark.asyncio
async def test_live_three_book_brandon_inbound_routes_through(monkeypatch):
    """End-to-end on the unowned line: the 3-book Brandon shape routes to Brandon
    (deterministic, not USERS_LIST order accident)."""
    r = _make_router(monkeypatch, _live_three_book_shape())
    await r.handle_incoming(sender="+14108166914", body="hey", span=3,
                            recvtime="2026-06-07 12:00:00", gateway_id="g1")
    assert len(r._chat_calls) == 1
    assert r._chat_calls[0]["operator"] == "Brandon"
