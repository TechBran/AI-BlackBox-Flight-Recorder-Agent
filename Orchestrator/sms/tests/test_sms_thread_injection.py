"""Auto-inject recent SMS thread into inbound replies (M4).

``handle_incoming`` STORES the current inbound, then calls ``_route_through_chat``,
which (M4) fetches the recent per-thread history from the SQLite store and
prepends it to the ``/chat`` payload as conversation turns:

  4.2 -- thread assembly: the most recent ~20 messages (oldest-first), scoped by
        ``(operator, sender, line_number)``; inbound -> ``user``, outbound ->
        ``assistant``; the just-stored current inbound is dropped exactly once
        (no duplicate -- it is re-appended as the final live user turn);
        consecutive same-direction rows (e.g. one AI reply split across 160-char
        segment rows) MERGE into one turn; a leading ``assistant`` turn is
        dropped so the first non-system message is a ``user`` (Anthropic rule).
  4.3 -- end-to-end shape: a SECOND inbound carries the prior user/assistant
        exchange AHEAD of the new text; a FIRST-EVER inbound carries only the
        single current user turn.

Uses a REAL ``MessageStore`` (tmp SQLite) so the fetch/de-dup/merge logic is
exercised against the real query, plus the ``_CapturingSession`` shape from
``test_sms_peer_path`` to inspect the posted ``/chat`` payload without a live
server, and the hermetic contact loader.
"""
import copy

import pytest

from Orchestrator.sms import message_store as msm
from Orchestrator.sms.router import SMSRouter


# ---------------------------------------------------------------------------
# Fakes: g1 with span2 owned by Brandon, span3 unowned (mirrors peer-path tests)
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
# Capturing aiohttp.ClientSession: records the /chat payload, fakes a completed task
# ---------------------------------------------------------------------------
async def _noop_sleep(*a, **k):
    return None


async def _noop_notify(*a, **k):
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
    posted_payloads = []  # class-level: read after the call

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, timeout=None):
        _CapturingSession.posted_payloads.append(json)
        return _FakeResp({"task_id": "t-fake-1"})

    def get(self, url, timeout=None):
        return _FakeResp({"status": "completed", "result_data": {"ui_reply": "ok"}})


@pytest.fixture
def capture_chat_payload(monkeypatch):
    import aiohttp

    _CapturingSession.posted_payloads = []
    monkeypatch.setattr(aiohttp, "ClientSession", _CapturingSession)
    import Orchestrator.sms.router as router_mod
    monkeypatch.setattr(router_mod.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(router_mod, "notify", _noop_notify)
    return _CapturingSession.posted_payloads


@pytest.fixture
def make_router(monkeypatch, tmp_path):
    """Build a router wired to a REAL MessageStore (tmp SQLite) and a hermetic
    contact loader pointed at ``books``."""
    import Orchestrator.contacts as contacts_mod
    import Orchestrator.config as config_mod

    monkeypatch.setattr(msm, "DB_PATH", tmp_path / "sms.db")
    store = msm.MessageStore()

    def _build(books):
        monkeypatch.setattr(
            contacts_mod, "load_contacts",
            lambda: contacts_mod._apply_inbound_defaults(copy.deepcopy(books)),
        )
        monkeypatch.setattr(config_mod, "USERS_LIST", list(books.keys()) or ["Brandon"])
        r = SMSRouter(FakeManager(), store)
        r._store_obj = store
        return r

    return _build


def _msgs(payload):
    return payload["messages"]


# ===========================================================================
# 4.3 -- FIRST-EVER inbound: only the single current user turn
# ===========================================================================
@pytest.mark.asyncio
async def test_first_ever_inbound_single_user_turn(make_router, capture_chat_payload):
    r = make_router({"Brandon": {"c1": _contact("Anna", "+2223334444")}})
    await r.handle_incoming(sender="+2223334444", body="first hello", span=3,
                            recvtime="2026-06-25 12:00:00", gateway_id="g1")

    assert len(capture_chat_payload) == 1
    messages = _msgs(capture_chat_payload[0])
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "first hello" in messages[0]["content"]


# ===========================================================================
# 4.3 -- SECOND inbound: prior exchange AHEAD of the new text, no duplicate
# ===========================================================================
@pytest.mark.asyncio
async def test_second_inbound_includes_prior_exchange(make_router, capture_chat_payload):
    r = make_router({"Brandon": {"c1": _contact("Anna", "+2223334444")}})

    # Turn 1: inbound + an AI reply, persisted to the real store.
    await r.handle_incoming(sender="+2223334444", body="hello", span=3,
                            recvtime="2026-06-25 12:00:00", gateway_id="g1")
    # First call payload was the first-ever turn (1 message).
    assert len(_msgs(capture_chat_payload[0])) == 1

    # Turn 2: a SECOND inbound from the same number.
    await r.handle_incoming(sender="+2223334444", body="you there?", span=3,
                            recvtime="2026-06-25 12:00:05", gateway_id="g1")

    messages = _msgs(capture_chat_payload[1])
    # Prior exchange (user "hello" + assistant "ok") AHEAD of the new text.
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user"]
    assert "hello" in messages[0]["content"]
    assert messages[1]["content"] == "ok"           # the stored AI reply
    assert "you there?" in messages[2]["content"]   # the live final user turn

    # The current text appears EXACTLY once (not duplicated by the history fetch).
    current_count = sum(1 for m in messages if "you there?" in m["content"])
    assert current_count == 1

    # First non-system message is a user turn (Anthropic requirement).
    assert messages[0]["role"] == "user"


# ===========================================================================
# 4.2 -- two consecutive inbounds (AI hasn't replied yet) must NOT yield
#        adjacent user turns. SMS defaults to Anthropic, which 400s on
#        consecutive same-role turns -> the peer would get NO reply. The live
#        current turn must MERGE into the trailing user history turn.
# ===========================================================================
@pytest.mark.asyncio
async def test_consecutive_inbounds_no_adjacent_user_turns(make_router, capture_chat_payload):
    r = make_router({"Brandon": {"c1": _contact("Anna", "+2223334444")}})
    store = r._store_obj

    # Anna texts twice with NO intervening AI reply (the AI hadn't answered the
    # first yet). The stored rows are [inbound A, inbound B(current)].
    store.store_message(operator="Brandon", direction="inbound", phone_number="+2223334444",
                        contact_name="Anna", body="first message",
                        timestamp="2026-06-25T11:59:00+00:00", line_number="+15550000000")

    await r.handle_incoming(sender="+2223334444", body="second message", span=3,
                            recvtime="2026-06-25 12:00:00", gateway_id="g1")

    messages = _msgs(capture_chat_payload[-1])
    roles = [m["role"] for m in messages]
    # STRICT alternation: no two adjacent turns share a role (Anthropic rule).
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)), roles
    # First non-system message is a user turn.
    assert roles[0] == "user"
    # The current text appears exactly once (merged, not duplicated).
    current_count = sum(1 for m in messages if "second message" in m["content"])
    assert current_count == 1
    # Both the prior unanswered text AND the current text are present (the prior
    # inbound is not lost — it's merged into the single user turn).
    joined = " ".join(m["content"] for m in messages)
    assert "first message" in joined
    assert "second message" in joined


# ===========================================================================
# 4.2 -- consecutive same-direction outbound segments MERGE into one turn
# ===========================================================================
@pytest.mark.asyncio
async def test_segmented_reply_merges_into_one_assistant_turn(make_router, capture_chat_payload):
    r = make_router({"Brandon": {"c1": _contact("Anna", "+2223334444")}})
    store = r._store_obj

    # Seed a prior exchange where the AI reply was split across 3 segment rows.
    store.store_message(operator="Brandon", direction="inbound", phone_number="+2223334444",
                        contact_name="Anna", body="long question",
                        timestamp="2026-06-25T11:00:00+00:00", line_number="+15550000000")
    for i, seg in enumerate(["part one ", "part two ", "part three"]):
        store.store_message(operator="Brandon", direction="outbound", phone_number="+2223334444",
                            contact_name="Anna", body=seg,
                            timestamp=f"2026-06-25T11:00:0{i + 1}+00:00",
                            line_number="+15550000000")

    await r.handle_incoming(sender="+2223334444", body="follow up", span=3,
                            recvtime="2026-06-25 12:00:00", gateway_id="g1")

    messages = _msgs(capture_chat_payload[-1])
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user"]
    # The 3 outbound segment rows merged into ONE assistant turn.
    assert messages[1]["content"] == "part one part two part three"
    assert "follow up" in messages[2]["content"]


# ===========================================================================
# 4.2 -- a leading assistant turn is dropped (first non-system must be user)
# ===========================================================================
@pytest.mark.asyncio
async def test_leading_assistant_turn_dropped(make_router, capture_chat_payload):
    r = make_router({"Brandon": {"c1": _contact("Anna", "+2223334444")}})
    store = r._store_obj

    # A thread whose OLDEST row is outbound (e.g. operator-initiated via send_manual)
    # -> the assembled history would start with an assistant turn. Drop it.
    store.store_message(operator="Brandon", direction="outbound", phone_number="+2223334444",
                        contact_name="Anna", body="we reached out first",
                        timestamp="2026-06-25T10:00:00+00:00", line_number="+15550000000")
    store.store_message(operator="Brandon", direction="inbound", phone_number="+2223334444",
                        contact_name="Anna", body="thanks",
                        timestamp="2026-06-25T10:05:00+00:00", line_number="+15550000000")
    store.store_message(operator="Brandon", direction="outbound", phone_number="+2223334444",
                        contact_name="Anna", body="welcome",
                        timestamp="2026-06-25T10:06:00+00:00", line_number="+15550000000")

    await r.handle_incoming(sender="+2223334444", body="one more thing", span=3,
                            recvtime="2026-06-25 12:00:00", gateway_id="g1")

    messages = _msgs(capture_chat_payload[-1])
    # Leading assistant ("we reached out first") dropped; first message is user.
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "thanks" or "thanks" in messages[0]["content"]
    assert "we reached out first" not in messages[0]["content"]
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user"]


# ===========================================================================
# 4.2 -- window cap: only the most recent N messages are injected (newest-kept)
# ===========================================================================
@pytest.mark.asyncio
async def test_history_window_capped(make_router, capture_chat_payload, monkeypatch):
    r = make_router({"Brandon": {"c1": _contact("Anna", "+2223334444")}})
    store = r._store_obj

    # Seed 30 alternating turns -> with a 20-message window the OLDEST get dropped.
    for i in range(15):
        store.store_message(operator="Brandon", direction="inbound", phone_number="+2223334444",
                            contact_name="Anna", body=f"in{i}",
                            timestamp=f"2026-06-25T10:{i:02d}:00+00:00", line_number="+15550000000")
        store.store_message(operator="Brandon", direction="outbound", phone_number="+2223334444",
                            contact_name="Anna", body=f"out{i}",
                            timestamp=f"2026-06-25T10:{i:02d}:30+00:00", line_number="+15550000000")

    await r.handle_incoming(sender="+2223334444", body="newest", span=3,
                            recvtime="2026-06-25 12:00:00", gateway_id="g1")

    messages = _msgs(capture_chat_payload[-1])
    # History turns + 1 current; the window is bounded (default 20 messages).
    # 20 history messages merge into 20 alternating turns (no consecutive dupes here).
    assert len(messages) <= 21
    # The very oldest message ("in0") fell outside the 20-message window.
    joined = " ".join(m["content"] for m in messages)
    assert "in0" not in joined
    # The newest historical exchange survived.
    assert "out14" in joined
    assert "newest" in messages[-1]["content"]


# ===========================================================================
# 4.2 -- thread is scoped by line_number (a peer texting two lines doesn't bleed)
# ===========================================================================
@pytest.mark.asyncio
async def test_thread_scoped_by_line_number(make_router, capture_chat_payload):
    # Two unowned lines on one gateway; same sender texts both. History on the
    # OTHER line must NOT bleed into this thread.
    r = make_router({"Brandon": {"c1": _contact("Anna", "+2223334444")}})
    store = r._store_obj

    # Prior message on a DIFFERENT line (span2 phone +14100000000) for same sender.
    store.store_message(operator="Brandon", direction="inbound", phone_number="+2223334444",
                        contact_name="Anna", body="on the other line",
                        timestamp="2026-06-25T09:00:00+00:00", line_number="+14100000000")
    # Prior message on THIS line (span3 phone +15550000000).
    store.store_message(operator="Brandon", direction="inbound", phone_number="+2223334444",
                        contact_name="Anna", body="on this line",
                        timestamp="2026-06-25T11:00:00+00:00", line_number="+15550000000")
    store.store_message(operator="Brandon", direction="outbound", phone_number="+2223334444",
                        contact_name="Anna", body="reply here",
                        timestamp="2026-06-25T11:00:05+00:00", line_number="+15550000000")

    await r.handle_incoming(sender="+2223334444", body="continuing here", span=3,
                            recvtime="2026-06-25 12:00:00", gateway_id="g1")

    messages = _msgs(capture_chat_payload[-1])
    joined = " ".join(m["content"] for m in messages)
    assert "on this line" in joined
    assert "reply here" in joined
    assert "on the other line" not in joined  # the other-line thread did NOT bleed in
