"""Inbound ReceivedSMS handling in the AMI client (audit O12-messaging-01, -11, -10).

  * -01: a concatenated SMS was buffered under (sender, Recvtime), but the TG200
    stamps every part with its own Recvtime (the parts land in different
    seconds), so each part sat alone in its own buffer until the 30 s timer and
    was dispatched as a separate inbound SMS, each with its own AI reply. The
    live firmware also numbers multi-part Index from 1. The journal of
    2026-08-17 shows exactly that: part Index 1 then 2 of a Total 2 text, one
    second apart, flushed as "1/2 parts" twice.
  * -11: int() on a blank or garbled Total/Index (or any other exception in
    event handling) escaped the read loop, closed the transport and forced a
    reconnect.
  * -10: the read loop awaited the whole reply (chat turn + send) inline, so no
    AMI frame was read meanwhile: a keepalive Ping answered in that window timed
    out and tore the connection down mid-reply.

Every test feeds the client hermetically: events go straight into the handler
or through an in-memory StreamReader. No socket, no gateway, no SMS is sent.
"""
from __future__ import annotations

import asyncio

import pytest

from Orchestrator.sms import ami_client as ami_mod
from Orchestrator.sms.ami_client import AMISMSClient


def _event(sender="+14105550001", content="", index="0", total="1",
           recvtime="2026-08-17 07:24:20", ref="", span="2"):
    return {
        "Event": "ReceivedSMS", "ID": ref, "GsmSpan": span, "Sender": sender,
        "Recvtime": recvtime, "Index": index, "Total": total, "Content": content,
    }


def _client():
    c = AMISMSClient(host="h", username="u", secret="s")
    c.gateway_id = "g1"
    got = []

    async def cb(sender, body, span, recvtime, gateway_id):
        got.append({"sender": sender, "body": body, "span": span,
                    "recvtime": recvtime, "gateway_id": gateway_id})

    c.on_sms(cb)
    return c, got


async def _settle(c):
    """Let the delivery tasks the handler scheduled run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)
    pending = [t for t in getattr(c, "_deliveries", ()) if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=2)


async def _feed(c, *events):
    for ev in events:
        await c._handle_received_sms(ev)
    await _settle(c)


# --- multi-part reassembly ----------------------------------------------------


def test_aug17_two_part_text_with_its_own_recvtime_per_part_dispatches_once():
    """The 2026-08-17 replay: Index 1 then 2 of Total 2, one second apart."""
    async def run():
        c, got = _client()
        await _feed(
            c,
            _event(content="I+want+you+to+recheck+my+Google+Calendar+off+because+I%27ve+already+ref",
                   index="1", total="2", recvtime="2026-08-17 07:24:20"),
            _event(content="alendar.+", index="2", total="2", recvtime="2026-08-17 07:24:21"),
        )
        assert len(got) == 1, got
        assert got[0]["body"] == "I want you to recheck my Google Calendar off because I've already refalendar. "
        assert got[0]["recvtime"] == "2026-08-17 07:24:20", "the first part's gateway time rides along"
        assert got[0]["gateway_id"] == "g1"
        assert c._multipart_flush_tasks == {}, "the fallback timer is cancelled once the text is whole"
        await c.disconnect()

    asyncio.run(run())


def test_parts_arriving_out_of_order_are_assembled_by_index():
    async def run():
        c, got = _client()
        await _feed(
            c,
            _event(content="three", index="3", total="3", recvtime="2026-09-29 12:49:30"),
            _event(content="one-", index="1", total="3", recvtime="2026-09-29 12:49:28"),
            _event(content="two-", index="2", total="3", recvtime="2026-09-29 12:49:29"),
        )
        assert [g["body"] for g in got] == ["one-two-three"]
        await c.disconnect()

    asyncio.run(run())


def test_zero_based_index_still_assembles():
    async def run():
        c, got = _client()
        await _feed(c, _event(content="a", index="0", total="2"),
                    _event(content="b", index="1", total="2", recvtime="2026-08-17 07:24:22"))
        assert [g["body"] for g in got] == ["ab"]
        await c.disconnect()

    asyncio.run(run())


def test_two_senders_do_not_share_a_buffer():
    async def run():
        c, got = _client()
        await _feed(
            c,
            _event(sender="+14105550001", content="A1", index="1", total="2"),
            _event(sender="+14105550002", content="B1", index="1", total="2"),
            _event(sender="+14105550002", content="B2", index="2", total="2"),
            _event(sender="+14105550001", content="A2", index="2", total="2"),
        )
        assert sorted((g["sender"], g["body"]) for g in got) == [
            ("+14105550001", "A1A2"), ("+14105550002", "B1B2")]
        await c.disconnect()

    asyncio.run(run())


def test_a_reference_keeps_two_interleaved_texts_apart():
    """A firmware that fills ID with the concatenation reference: two texts from
    one sender with the same part count, interleaved, stay two texts."""
    async def run():
        c, got = _client()
        await _feed(
            c,
            _event(content="x1", index="1", total="2", ref="17"),
            _event(content="y1", index="1", total="2", ref="18"),
            _event(content="x2", index="2", total="2", ref="17"),
            _event(content="y2", index="2", total="2", ref="18"),
        )
        assert sorted(g["body"] for g in got) == ["x1x2", "y1y2"]
        await c.disconnect()

    asyncio.run(run())


def test_a_repeated_index_starts_the_next_text_instead_of_overwriting():
    """Part 1 of a second text arriving before part 2 of the first: the first
    text's parts are delivered as they are (never silently overwritten), and
    the second text assembles on its own."""
    async def run():
        c, got = _client()
        await _feed(
            c,
            _event(content="first-text ", index="1", total="2"),
            _event(content="second-", index="1", total="2", recvtime="2026-08-17 07:25:00"),
            _event(content="text", index="2", total="2", recvtime="2026-08-17 07:25:01"),
        )
        assert [g["body"] for g in got] == ["first-text ", "second-text"]
        await c.disconnect()

    asyncio.run(run())


def test_a_missing_part_is_flushed_once_by_the_fallback_timer(monkeypatch):
    monkeypatch.setattr(ami_mod, "MULTIPART_TIMEOUT_S", 0.05)

    async def run():
        c, got = _client()
        await _feed(c, _event(content="only half", index="1", total="2"))
        assert got == []
        await asyncio.sleep(0.2)
        await _settle(c)
        assert [g["body"] for g in got] == ["only half"]
        assert c._multipart_buf == {} and c._multipart_flush_tasks == {}
        await c.disconnect()

    asyncio.run(run())


def test_disconnect_cancels_pending_multipart_timers():
    async def run():
        c, got = _client()
        await _feed(c, _event(content="half", index="1", total="2"))
        timers = list(c._multipart_flush_tasks.values())
        assert timers
        await c.disconnect()
        await asyncio.sleep(0)
        assert all(t.cancelled() or t.done() for t in timers)
        assert c._multipart_buf == {}

    asyncio.run(run())


# --- gateway-supplied numbers never break event handling ----------------------


@pytest.mark.parametrize("total,index", [("", ""), ("abc", "x"), (" 1 ", "0"), ("-3", "1"), (None, None)])
def test_blank_or_garbled_total_and_index_deliver_as_a_single_part(total, index):
    async def run():
        c, got = _client()
        ev = _event(content="hello", index=index, total=total)
        if total is None:
            del ev["Total"], ev["Index"]
        await _feed(c, ev)
        assert [g["body"] for g in got] == ["hello"]
        await c.disconnect()

    asyncio.run(run())


def _wire(c, data: bytes):
    """Point the client at an in-memory reader holding [data], then EOF."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    c._reader = reader
    c._connected = True
    c._authenticated = True
    closed, reconnects = [], []

    async def fake_close():
        closed.append(True)
        c._connected = False

    c._close_transport = fake_close
    c._schedule_reconnect = lambda: reconnects.append(True)
    return closed, reconnects


def _frame(**fields) -> bytes:
    lines = [f"{k}: {v}" for k, v in fields.items()]
    return ("\r\n".join(lines) + "\r\n--END SMS EVENT--\r\n").encode()


def test_an_exception_while_handling_one_event_does_not_end_the_read_loop():
    async def run():
        c, got = _client()
        seen = []
        real = c._handle_received_sms

        async def flaky(msg):
            seen.append(msg.get("Content"))
            if msg.get("Content") == "boom":
                raise RuntimeError("handler blew up")
            await real(msg)

        c._handle_received_sms = flaky
        _wire(c, _frame(Event="ReceivedSMS", Sender="+14105550001", Total="1", Index="0", Content="boom")
              + _frame(Event="ReceivedSMS", Sender="+14105550001", Total="1", Index="0", Content="after"))
        await asyncio.wait_for(c._read_loop(), timeout=5)
        await _settle(c)
        assert seen == ["boom", "after"], "the event after the failing one is still read"
        assert [g["body"] for g in got] == ["after"]
        await c.disconnect()

    asyncio.run(run())


def test_the_read_loop_keeps_reading_while_a_reply_is_being_written():
    """A slow reply (chat turn + send) must not stop AMI frames being read: the
    Ping answer that arrives meanwhile resolves its pending action."""
    async def run():
        c = AMISMSClient(host="h", username="u", secret="s")
        release = asyncio.Event()
        started = asyncio.Event()

        async def slow_reply(*args):
            started.set()
            await release.wait()

        c.on_sms(slow_reply)
        ping = asyncio.get_running_loop().create_future()
        c._pending["ping-1"] = ping
        _wire(c, _frame(Event="ReceivedSMS", Sender="+14105550001", Total="1", Index="0", Content="hi")
              + b"Response: Success\r\nActionID: ping-1\r\nPing: Pong\r\n\r\n")
        loop_task = asyncio.create_task(c._read_loop())
        try:
            resp = await asyncio.wait_for(ping, timeout=2)
            assert resp.get("Ping") == "Pong"
            assert started.is_set(), "the reply is being written meanwhile"
        finally:
            release.set()
            await asyncio.wait_for(loop_task, timeout=5)
            await _settle(c)
            await c.disconnect()

    asyncio.run(run())


def test_inbound_texts_are_still_answered_one_at_a_time_in_arrival_order():
    async def run():
        c = AMISMSClient(host="h", username="u", secret="s")
        log = []

        async def reply(sender, body, *rest):
            log.append(("start", body))
            await asyncio.sleep(0.01)
            log.append(("end", body))

        c.on_sms(reply)
        await _feed(c, _event(content="first"), _event(content="second"), _event(content="third"))
        assert log == [("start", "first"), ("end", "first"), ("start", "second"),
                       ("end", "second"), ("start", "third"), ("end", "third")]
        await c.disconnect()

    asyncio.run(run())


class _Writer:
    def __init__(self):
        self.data = []

    def write(self, b):
        self.data.append(b)

    async def drain(self):
        return None

    def is_closing(self):
        return False

    def close(self):
        return None

    async def wait_closed(self):
        return None


def test_a_new_connection_stops_the_previous_connections_loops(monkeypatch):
    """A read loop left over from the previous connection kept reading the new
    socket next to the new loop (the 'already waiting' contention)."""
    async def run():
        c = AMISMSClient(host="h", username="u", secret="s")
        old_read = asyncio.create_task(asyncio.sleep(60))
        old_keepalive = asyncio.create_task(asyncio.sleep(60))
        c._read_task, c._keepalive_task = old_read, old_keepalive
        reader = asyncio.StreamReader()
        reader.feed_data(b"Asterisk Call Manager/1.1\r\n")

        async def fake_open(host, port):
            return reader, _Writer()

        async def authenticated():
            return True

        monkeypatch.setattr(ami_mod.asyncio, "open_connection", fake_open)
        c._authenticate = authenticated
        await c.connect()
        await asyncio.sleep(0)
        try:
            assert old_read.cancelled() and old_keepalive.cancelled()
            assert c._read_task is not old_read and not c._read_task.done()
        finally:
            await c.disconnect()

    asyncio.run(run())


def test_disconnect_ends_a_reply_in_flight():
    """Replies used to run inside the read task disconnect cancels; as their own
    tasks they are cancelled with it, never left running past the client."""
    async def run():
        c = AMISMSClient(host="h", username="u", secret="s")
        started, cancelled = asyncio.Event(), []

        async def slow_reply(*args):
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        c.on_sms(slow_reply)
        await c._handle_received_sms(_event(content="hi"))
        await asyncio.wait_for(started.wait(), timeout=2)
        await c.disconnect()
        await asyncio.sleep(0)
        assert cancelled == [True]
        assert not any(not t.done() for t in c._deliveries)

    asyncio.run(run())
