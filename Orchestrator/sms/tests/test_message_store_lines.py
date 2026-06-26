"""Tests for per-line (line_number + gateway_id) scoping in MessageStore."""

from Orchestrator.sms import message_store as msm


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(msm, "DB_PATH", tmp_path / "sms.db")
    return msm.MessageStore()


def test_new_columns_exist(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    cols = {r[1] for r in s._conn.execute("PRAGMA table_info(messages)").fetchall()}
    assert "line_number" in cols and "gateway_id" in cols


def test_store_and_filter_by_line(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    s.store_message(operator="Brandon", direction="inbound", phone_number="+14105550001",
                    contact_name="A", body="hi on line 1", line_number="+14103497272", gateway_id="g1")
    s.store_message(operator="Brandon", direction="inbound", phone_number="+14105550001",
                    contact_name="A", body="hi on line 2", line_number="+15559990000", gateway_id="g2")
    all_msgs = s.get_conversation("Brandon", "+14105550001")
    assert len(all_msgs) == 2                       # no line filter = both
    line1 = s.get_conversation("Brandon", "+14105550001", line_number="+14103497272")
    assert len(line1) == 1 and line1[0]["body"] == "hi on line 1"


def test_recent_threads_scoped_by_line(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    s.store_message(operator="Brandon", direction="inbound", phone_number="+14105550001",
                    contact_name="A", body="x", line_number="+14103497272", gateway_id="g1")
    s.store_message(operator="Brandon", direction="inbound", phone_number="+14105550002",
                    contact_name="B", body="y", line_number="+15559990000", gateway_id="g2")
    assert len(s.get_recent_threads("Brandon")) == 2
    scoped = s.get_recent_threads("Brandon", line_number="+14103497272")
    assert len(scoped) == 1 and "line_number" in scoped[0]


def test_migration_on_legacy_db(tmp_path, monkeypatch):
    # simulate an OLD db without the new columns, then open with MessageStore (should ALTER)
    import sqlite3
    db = tmp_path / "sms.db"
    con = sqlite3.connect(db)
    con.executescript('''CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
        operator TEXT NOT NULL, direction TEXT NOT NULL, phone_number TEXT NOT NULL,
        contact_name TEXT DEFAULT '', body TEXT NOT NULL, ai_response TEXT DEFAULT '',
        timestamp TEXT NOT NULL, status TEXT DEFAULT 'delivered', read INTEGER DEFAULT 0);''')
    # Production stores phone_number already normalized to last-10-digits
    # (store_message calls _normalize_phone), so a real legacy row looks like this.
    con.execute("INSERT INTO messages (operator,direction,phone_number,body,timestamp) VALUES ('Brandon','inbound','4105550001','old',  '2026-01-01T00:00:00Z')")
    con.commit(); con.close()
    monkeypatch.setattr(msm, "DB_PATH", db)
    s = msm.MessageStore()                          # must ALTER without error
    cols = {r[1] for r in s._conn.execute("PRAGMA table_info(messages)").fetchall()}
    assert "line_number" in cols and "gateway_id" in cols
    rows = s.get_conversation("Brandon", "+14105550001")
    assert len(rows) == 1 and rows[0]["line_number"] == ""   # legacy row backfilled to ''


def test_get_conversation_same_second_ordered_by_id(tmp_path, monkeypatch):
    # Two messages with the SAME timestamp: an inbound then an outbound.
    # ORDER BY timestamp alone is non-deterministic for the tie; the secondary
    # ``id`` key must keep them in INSERTION order (inbound first, lower id).
    s = _store(tmp_path, monkeypatch)
    same_ts = "2026-06-25T12:00:00+00:00"
    s.store_message(operator="Brandon", direction="inbound", phone_number="+14105550001",
                    contact_name="A", body="question", timestamp=same_ts)
    s.store_message(operator="Brandon", direction="outbound", phone_number="+14105550001",
                    contact_name="A", body="answer", timestamp=same_ts)
    rows = s.get_conversation("Brandon", "+14105550001")
    assert [r["direction"] for r in rows] == ["inbound", "outbound"]
    assert [r["body"] for r in rows] == ["question", "answer"]
    assert rows[0]["id"] < rows[1]["id"]


def test_get_recent_returns_newest_window_chronologically(tmp_path, monkeypatch):
    # Insert 5 messages with increasing timestamps; get_conversation(recent=True,
    # limit=3) must return the LAST 3 (newest window) in chronological order.
    s = _store(tmp_path, monkeypatch)
    for i in range(5):
        s.store_message(operator="Brandon", direction="inbound", phone_number="+14105550001",
                        contact_name="A", body=f"m{i}",
                        timestamp=f"2026-06-25T12:00:0{i}+00:00")
    recent = s.get_conversation("Brandon", "+14105550001", limit=3, recent=True)
    assert [r["body"] for r in recent] == ["m2", "m3", "m4"]  # last 3, oldest-first


def test_get_recent_fewer_than_window(tmp_path, monkeypatch):
    # When fewer rows than the window exist, recent=True returns them all, oldest-first.
    s = _store(tmp_path, monkeypatch)
    s.store_message(operator="Brandon", direction="inbound", phone_number="+14105550001",
                    contact_name="A", body="only", timestamp="2026-06-25T12:00:00+00:00")
    recent = s.get_conversation("Brandon", "+14105550001", limit=20, recent=True)
    assert [r["body"] for r in recent] == ["only"]


def test_migration_idempotent(tmp_path, monkeypatch):
    # constructing MessageStore twice on the same db must be safe
    db = tmp_path / "sms.db"
    monkeypatch.setattr(msm, "DB_PATH", db)
    s1 = msm.MessageStore()
    s1.store_message(operator="Brandon", direction="inbound", phone_number="+14105550001",
                     contact_name="A", body="hi", line_number="+14103497272", gateway_id="g1")
    s2 = msm.MessageStore()                          # second open must not error
    cols = {r[1] for r in s2._conn.execute("PRAGMA table_info(messages)").fetchall()}
    assert "line_number" in cols and "gateway_id" in cols
    rows = s2.get_conversation("Brandon", "+14105550001", line_number="+14103497272")
    assert len(rows) == 1
