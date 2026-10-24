"""Inbound SMS rows are stamped with the server's receipt time (audit O12-messaging-02).

The router used to store the TG200's Recvtime ('YYYY-MM-DD HH:MM:SS', in the
gateway's own clock) relabelled as UTC, while every outbound row is stamped with
the server's UTC clock. The gateway on the dev box runs hours behind UTC, so an
inbound sorted hours BEFORE the reply it caused: the thread view interleaved,
the model's thread memory attached replies to the wrong turn, and the thread
list previewed an older outbound as the latest message. The row's timestamp is
now the server's receipt time; the gateway's own string is kept, untouched, in
the additive gateway_recvtime column.
"""
from __future__ import annotations

import asyncio
import copy
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from Orchestrator.sms import message_store as msm
from Orchestrator.sms.router import SMSRouter


class _Client:
    gateway_id = "g1"

    def __init__(self):
        self.sent = []

    async def send_sms(self, destination, message, span=2):
        self.sent.append((destination, message, span))
        return {"success": True, "error": None}


class _Manager:
    def __init__(self):
        self.client = _Client()

    def set_sms_callback(self, cb):
        pass

    def gateways(self):
        return {"g1": {"id": "g1", "ports": [{"span": 2, "phone_number": "+15550000000", "operator": ""}]}}

    def get(self, gateway_id):
        return self.client

    def default(self):
        return self.client

    def resolve_for_number(self, number):
        return None


@pytest.fixture
def router(monkeypatch, tmp_path):
    import Orchestrator.config as config_mod
    import Orchestrator.contacts as contacts_mod
    import Orchestrator.sms.router as router_mod

    monkeypatch.setattr(msm, "DB_PATH", tmp_path / "sms.db")
    store = msm.MessageStore()
    books = {"Ana": {"c1": {"name": "Pat", "phone": "+14105550001", "inbound_allowed": True,
                            "is_operator_self": False, "created_by": "user"}}}
    monkeypatch.setattr(contacts_mod, "load_contacts",
                        lambda: contacts_mod._apply_inbound_defaults(copy.deepcopy(books)))
    monkeypatch.setattr(config_mod, "USERS_LIST", ["Ana"])

    async def no_notify(*a, **k):
        return None

    monkeypatch.setattr(router_mod, "notify", no_notify)
    r = SMSRouter(_Manager(), store)

    async def no_reply(self, *a, **k):
        return ""

    monkeypatch.setattr(SMSRouter, "_route_through_chat", no_reply)
    return r


def _gateway_clock(hours_behind_utc: int) -> str:
    """'now' as a gateway whose clock runs [hours_behind_utc] behind UTC writes it."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours_behind_utc)).strftime("%Y-%m-%d %H:%M:%S")


def test_an_inbound_received_after_an_outbound_sorts_after_it(router):
    async def run():
        sent = await router.send_manual("Ana", "+14105550001", "are you free at 3?")
        assert sent["success"]
        # The reply arrives AFTER the send, but the gateway clock is 7 h behind UTC.
        recv = _gateway_clock(7)
        await router.handle_incoming(sender="+14105550001", body="yes, 3 works", span="2",
                                     recvtime=recv, gateway_id="g1")
        return recv

    recv = asyncio.run(run())
    rows = router.store.get_conversation("Ana", "+14105550001", limit=10, recent=True)
    assert [r["body"] for r in rows] == ["are you free at 3?", "yes, 3 works"]
    inbound = rows[-1]
    assert inbound["timestamp"] >= rows[0]["timestamp"]
    assert inbound["gateway_recvtime"] == recv, "the gateway's own string is kept as sent"

    threads = router.store.get_recent_threads("Ana")
    assert len(threads) == 1
    assert threads[0]["last_message"] == "yes, 3 works", "the newer inbound is the preview"


def test_the_inbound_timestamp_is_the_server_receipt_time(router):
    before = datetime.now(timezone.utc)
    asyncio.run(router.handle_incoming(sender="+14105550001", body="hi", span="2",
                                       recvtime="2020-01-01 00:00:00", gateway_id="g1"))
    after = datetime.now(timezone.utc)
    row = router.store.get_conversation("Ana", "+14105550001")[0]
    stamped = datetime.fromisoformat(row["timestamp"])
    assert stamped.tzinfo is not None
    assert before <= stamped <= after
    assert row["gateway_recvtime"] == "2020-01-01 00:00:00"


def test_a_garbled_or_missing_recvtime_is_kept_as_is_and_never_used_for_order(router):
    asyncio.run(router.handle_incoming(sender="+14105550001", body="one", span="2",
                                       recvtime="not a time", gateway_id="g1"))
    asyncio.run(router.handle_incoming(sender="+14105550001", body="two", span="2",
                                       recvtime=None, gateway_id="g1"))
    rows = router.store.get_conversation("Ana", "+14105550001")
    assert [r["body"] for r in rows] == ["one", "two"]
    assert [r["gateway_recvtime"] for r in rows] == ["not a time", ""]


def test_gateway_recvtime_is_added_to_an_existing_store(tmp_path, monkeypatch):
    db = tmp_path / "sms.db"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
        operator TEXT NOT NULL, direction TEXT NOT NULL, phone_number TEXT NOT NULL,
        contact_name TEXT DEFAULT '', body TEXT NOT NULL, ai_response TEXT DEFAULT '',
        timestamp TEXT NOT NULL, status TEXT DEFAULT 'delivered', read INTEGER DEFAULT 0,
        line_number TEXT DEFAULT '', gateway_id TEXT DEFAULT '');""")
    con.execute("INSERT INTO messages (operator,direction,phone_number,body,timestamp) "
                "VALUES ('Ana','inbound','4105550001','old','2026-08-17T04:24:19+00:00')")
    con.commit()
    con.close()
    monkeypatch.setattr(msm, "DB_PATH", db)
    s = msm.MessageStore()
    cols = {r[1] for r in s._conn.execute("PRAGMA table_info(messages)").fetchall()}
    assert "gateway_recvtime" in cols
    assert s.get_conversation("Ana", "4105550001")[0]["gateway_recvtime"] == ""
    s.store_message(operator="Ana", direction="inbound", phone_number="+14105550001",
                    contact_name="Pat", body="new", gateway_recvtime="2026-10-23 07:00:00")
    assert s.get_conversation("Ana", "4105550001")[-1]["gateway_recvtime"] == "2026-10-23 07:00:00"
    msm.MessageStore()  # opening again is a no-op


def test_the_receipt_time_the_ami_client_read_is_the_row_timestamp(router):
    """The AMI client reads the receipt time with the event and hands it over; a
    text that waited for the previous reply keeps it (O12-messaging-02)."""
    asyncio.run(router.handle_incoming(sender="+14105550001", body="hi", span="2",
                                       recvtime="2020-01-01 00:00:00", gateway_id="g1",
                                       received_at="2026-10-23T10:00:05+00:00"))
    row = router.store.get_conversation("Ana", "+14105550001")[0]
    assert row["timestamp"] == "2026-10-23T10:00:05+00:00"
    assert row["gateway_recvtime"] == "2020-01-01 00:00:00"


def test_a_text_that_arrived_during_a_reply_is_ordered_before_that_reply(router, monkeypatch):
    """Real AMI client, router and store: B arrives 50 ms after A, while the reply
    to A is still being written. It used to be stamped when its turn came, after
    that reply, and so was shown after a reply that went out after it arrived."""
    from Orchestrator.sms.ami_client import AMISMSClient

    async def slow_reply(self, sender, body, *a, **k):
        await asyncio.sleep(0.3)   # the chat turn
        return f"reply to {body}"

    monkeypatch.setattr(SMSRouter, "_route_through_chat", slow_reply)

    def event(body):
        return {"Event": "ReceivedSMS", "ID": "", "GsmSpan": "2", "Sender": "+14105550001",
                "Recvtime": "2026-10-23 03:00:00", "Index": "0", "Total": "1", "Content": body}

    async def run():
        c = AMISMSClient(host="h", username="u", secret="s")
        c.gateway_id = "g1"
        c.on_sms(router.handle_incoming)
        await c._handle_received_sms(event("A"))
        await asyncio.sleep(0.05)
        await c._handle_received_sms(event("B"))
        while c._deliveries:
            await asyncio.wait(list(c._deliveries), timeout=5)
        await c.disconnect()

    asyncio.run(run())
    rows = router.store.get_conversation("Ana", "+14105550001", limit=10, recent=True)
    assert [(r["direction"], r["body"]) for r in rows] == [
        ("inbound", "A"), ("inbound", "B"), ("outbound", "reply to A"), ("outbound", "reply to B")]
