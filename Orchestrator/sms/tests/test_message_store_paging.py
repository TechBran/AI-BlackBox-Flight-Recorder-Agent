"""Newest-window cursor paging and one row per thread (audit O12-messaging-03, -20).

  * -03: /sms/messages served the OLDEST page of a thread; both clients asked for
    one page of 100 and never paged, so a long thread never showed its recent
    messages. The store's newest-window mode now takes a cursor (before_id):
    the page of messages just before that one, in the same (timestamp, id)
    order the thread is shown in.
  * -20 / A14-ui-misc-10: get_recent_threads joined on MAX(timestamp), so two
    rows of one phone sharing the newest timestamp produced two thread rows for
    that phone (and Android keys its list by phone, so it crashed on the
    duplicate key). One row per phone now: the newest by (timestamp, id).
"""
from __future__ import annotations

from Orchestrator.sms import message_store as msm

PHONE = "+14105550001"


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(msm, "DB_PATH", tmp_path / "sms.db")
    return msm.MessageStore()


def _fill(s, n, phone=PHONE, operator="Ana"):
    for i in range(n):
        s.store_message(operator=operator, direction="inbound" if i % 2 else "outbound",
                        phone_number=phone, contact_name="Pat", body=f"m{i:03d}",
                        timestamp=f"2026-10-01T00:{i // 60:02d}:{i % 60:02d}+00:00")


def _bodies(rows):
    return [r["body"] for r in rows]


def test_cursor_pages_walk_back_from_the_newest_without_gaps_or_repeats(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    _fill(s, 120)
    page = s.get_conversation("Ana", PHONE, limit=50, recent=True)
    assert _bodies(page) == [f"m{i:03d}" for i in range(70, 120)]
    seen = list(page)
    while page:
        page = s.get_conversation("Ana", PHONE, limit=50, recent=True, before_id=page[0]["id"])
        seen = page + seen
    assert _bodies(seen) == [f"m{i:03d}" for i in range(120)]


def test_the_cursor_follows_display_order_not_insertion_order(tmp_path, monkeypatch):
    """Rows written before receipt-time stamping carry a skewed clock, so the
    thread's (timestamp, id) order is not its id order: paging must follow the
    order the thread is shown in."""
    s = _store(tmp_path, monkeypatch)
    for body, ts in [("c", "2026-10-01T03:00:00+00:00"), ("a", "2026-10-01T01:00:00+00:00"),
                     ("d", "2026-10-01T04:00:00+00:00"), ("b", "2026-10-01T02:00:00+00:00"),
                     ("e", "2026-10-01T05:00:00+00:00")]:
        s.store_message(operator="Ana", direction="inbound", phone_number=PHONE,
                        contact_name="Pat", body=body, timestamp=ts)
    full = s.get_conversation("Ana", PHONE, limit=100)
    assert _bodies(full) == ["a", "b", "c", "d", "e"]
    newest = s.get_conversation("Ana", PHONE, limit=2, recent=True)
    assert _bodies(newest) == ["d", "e"]
    older = s.get_conversation("Ana", PHONE, limit=2, recent=True, before_id=newest[0]["id"])
    assert _bodies(older) == ["b", "c"]
    oldest = s.get_conversation("Ana", PHONE, limit=2, recent=True, before_id=older[0]["id"])
    assert _bodies(oldest) == ["a"]


def test_a_cursor_from_another_thread_or_a_missing_row_returns_nothing(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    _fill(s, 5)
    assert s.get_conversation("Ana", PHONE, limit=10, recent=True, before_id=999_999) == []


def test_the_newest_window_skips_offset_rows_from_the_newest(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    _fill(s, 10)
    assert _bodies(s.get_conversation("Ana", PHONE, limit=3, offset=2, recent=True)) == [
        "m005", "m006", "m007"]


def test_one_thread_row_per_phone_when_the_newest_rows_share_a_timestamp(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    same = "2026-10-01T12:00:00+00:00"
    s.store_message(operator="Ana", direction="inbound", phone_number=PHONE, contact_name="Pat",
                    body="part of a burst", timestamp=same)
    s.store_message(operator="Ana", direction="outbound", phone_number=PHONE, contact_name="Pat",
                    body="the reply", timestamp=same)
    s.store_message(operator="Ana", direction="inbound", phone_number="+14105550002",
                    contact_name="Lee", body="older thread", timestamp="2026-10-01T11:00:00+00:00")
    threads = s.get_recent_threads("Ana")
    assert [t["phone_number"] for t in threads] == ["4105550001", "4105550002"]
    assert threads[0]["last_message"] == "the reply", "the tie goes to the later row"
    assert threads[0]["unread_count"] == 1
    assert threads[1]["unread_count"] == 1


def test_thread_rows_stay_scoped_to_the_operator_and_line(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    s.store_message(operator="Ana", direction="inbound", phone_number=PHONE, contact_name="Pat",
                    body="to line 1", line_number="+15550000001", timestamp="2026-10-01T10:00:00+00:00")
    s.store_message(operator="Ana", direction="inbound", phone_number=PHONE, contact_name="Pat",
                    body="to line 2", line_number="+15550000002", timestamp="2026-10-01T11:00:00+00:00")
    s.store_message(operator="Ben", direction="inbound", phone_number=PHONE, contact_name="Pat",
                    body="Ben's", timestamp="2026-10-01T12:00:00+00:00")
    assert _bodies_of_threads(s.get_recent_threads("Ana")) == ["to line 2"]
    assert _bodies_of_threads(s.get_recent_threads("Ana", line_number="+15550000001")) == ["to line 1"]
    assert _bodies_of_threads(s.get_recent_threads("Ben")) == ["Ben's"]
    line1 = s.get_recent_threads("Ana", line_number="+15550000001")
    assert line1[0]["unread_count"] == 1, "the unread count follows the line filter"


def _bodies_of_threads(threads):
    return [t["last_message"] for t in threads]
