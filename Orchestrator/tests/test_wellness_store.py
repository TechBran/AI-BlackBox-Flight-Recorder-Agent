"""The per-operator wellness store (spec §3.4): modes 0700/0600, the spec's
DDL, idempotent re-ingest, replace-day, deletes by HC id, the effective-epoch
rule, flagged rows, the delivered/notice/consent/task_ref APIs, and clean
open/close (no leaked handles)."""
from __future__ import annotations

import gc
import os
import sqlite3
import stat
import threading
import warnings

import pytest

from Orchestrator.wellness import paths, store

DAY = "2026-10-20"
OFF = -14400                       # UTC-4
T0 = store.day_bounds(DAY, OFF)[0]  # local midnight, in ms
MIN = 60_000


@pytest.fixture
def st(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "data" / "wellness")
    s = store.open_store("0123456789abcdef")
    yield s
    s.close()


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_modes_are_private(st, tmp_path):
    assert _mode(paths.data_dir()) == 0o700
    assert _mode(st.path) == 0o600
    with st.write() as conn:
        conn.execute("INSERT INTO meta(key, value) VALUES ('x', 'y')")
    for suffix in ("-wal", "-shm"):
        side = st.path.with_name(st.path.name + suffix)
        if side.exists():
            assert _mode(side) == 0o600


def test_resecure_fixes_loose_modes(st):
    os.chmod(paths.data_dir(), 0o755)
    os.chmod(st.path, 0o644)
    counts = store.secure_all()
    assert counts["dbs"] == 1
    assert _mode(paths.data_dir()) == 0o700 and _mode(st.path) == 0o600


def test_schema_is_the_spec_ddl(st):
    with st.read() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"meta", "epoch", "origin", "bucket", "session", "stage", "reading", "rollup",
                "baseline", "coverage_day", "digest", "ingest_log", "delivered", "notice",
                "consent_event", "task_ref"} <= tables
        sql = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master "
                                                "WHERE type='table'")}
    assert "STRICT" in sql["bucket"] and "WITHOUT ROWID" in sql["bucket"]
    assert "WITHOUT ROWID" in sql["stage"]
    assert "STRICT" in sql["delivered"]
    with st.read() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_op_key_is_the_file_name_never_the_operator():
    with pytest.raises(ValueError):
        paths.db_path("wellness-fixture")
    with pytest.raises(ValueError):
        paths.db_path("../../etc/passwd")
    assert store.new_op_key() != store.new_op_key()
    assert paths.valid_op_key(store.new_op_key())


def _hr(n=12, start=T0 + 8 * 60 * MIN, avg=60.0):
    return [[start + i * 5 * MIN, avg, avg - 3, avg + 3, 5] for i in range(n)]


def test_replace_day_is_idempotent_and_replaces(st):
    with st.write() as conn:
        o = st.ensure_origin(conn, {"package": "p", "device_type": "WATCH"})
        st.replace_bucket_day(conn, "heart_rate", "5min", o, 1, DAY, OFF, _hr(12))
        st.replace_bucket_day(conn, "heart_rate", "5min", o, 1, DAY, OFF, _hr(12))
    with st.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bucket").fetchone()[0] == 12
    # A re-aggregated day with fewer buckets REPLACES (a deletion on the phone).
    with st.write() as conn:
        st.replace_bucket_day(conn, "heart_rate", "5min", o, 1, DAY, OFF, _hr(4))
    with st.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bucket").fetchone()[0] == 4


def test_replace_day_leaves_the_neighbouring_days(st):
    nxt = store.day_bounds("2026-10-21", OFF)[0]
    with st.write() as conn:
        o = st.ensure_origin(conn, {"package": "p", "device_type": "WATCH"})
        st.replace_bucket_day(conn, "heart_rate", "5min", o, 1, "2026-10-21", OFF,
                              _hr(3, start=nxt + MIN))
        st.replace_bucket_day(conn, "heart_rate", "5min", o, 1, DAY, OFF, _hr(2))
        st.replace_bucket_day(conn, "heart_rate", "5min", o, 1, DAY, OFF, [])
    with st.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bucket").fetchone()[0] == 3


def test_sessions_and_readings_upsert_by_epoch_and_hc_id(st):
    sess = {"kind": "sleep", "hc_id": "s1", "origin_id": 1, "start": T0, "end": T0 + 420 * MIN,
            "offset_s": OFF, "stages": [[T0, T0 + 420 * MIN, 4]], "last_modified": 1}
    with st.write() as conn:
        st.upsert_session(conn, 1, sess)
        st.upsert_session(conn, 1, dict(sess, end=T0 + 400 * MIN, stages=[[T0, T0 + 400 * MIN, 5]]))
        st.upsert_reading(conn, 1, {"hc_id": "r1", "metric": "spo2", "t": T0, "offset_s": OFF,
                                    "origin_id": 1, "v": 96.0})
        st.upsert_reading(conn, 1, {"hc_id": "r1", "metric": "spo2", "t": T0, "offset_s": OFF,
                                    "origin_id": 1, "v": 97.0})
    with st.read() as conn:
        assert tuple(conn.execute("SELECT COUNT(*), MAX(end_ts) FROM session").fetchone()) == \
            (1, T0 + 400 * MIN)
        assert [tuple(r) for r in conn.execute("SELECT code FROM stage")] == [(5,)]
        assert tuple(conn.execute("SELECT COUNT(*), MAX(v1) FROM reading").fetchone()) == (1, 97.0)


def test_delete_by_hc_id_returns_the_dirty_days(st):
    with st.write() as conn:
        st.upsert_session(conn, 1, {"kind": "sleep", "hc_id": "s1", "origin_id": 1,
                                    "start": T0 - 60 * MIN, "end": T0 + 360 * MIN,
                                    "offset_s": OFF, "stages": []})
        st.upsert_reading(conn, 1, {"hc_id": "r1", "metric": "spo2", "t": T0 + MIN,
                                    "offset_s": OFF, "origin_id": 1, "v": 96.0})
        days = st.delete_hc_ids(conn, ["s1", "r1", "never-seen"])
    assert days == {"2026-10-19", DAY}
    with st.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM reading").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM stage").fetchone()[0] == 0


def test_effective_epoch_uses_the_highest_epoch_per_day(st):
    """A re-enrolled phone re-sends the same week under a new epoch: each local
    day reads only its newest epoch, so nothing is double-counted."""
    with st.write() as conn:
        o = st.ensure_origin(conn, {"package": "hc_merged"})
        st.set_day_offset(conn, DAY, OFF)
        st.set_day_offset(conn, "2026-10-21", OFF)
        st.replace_bucket_day(conn, "steps", "1h", o, 1, DAY, OFF, [[T0 + 9 * 60 * MIN, 1000]])
        st.replace_bucket_day(conn, "steps", "1h", o, 2, DAY, OFF, [[T0 + 9 * 60 * MIN, 1000]])
        nxt = store.day_bounds("2026-10-21", OFF)[0]
        st.replace_bucket_day(conn, "steps", "1h", o, 1, "2026-10-21", OFF,
                              [[nxt + 9 * 60 * MIN, 500]])
    rows = st.buckets("steps", "1h", T0, T0 + 2 * 86400_000)
    assert sorted((r["epoch"], r["sum"]) for r in rows) == [(1, 500.0), (2, 1000.0)]


def test_flagged_rows_are_stored_but_not_read(st):
    with st.write() as conn:
        o = st.ensure_origin(conn, {"package": "p", "device_type": "WATCH"})
        st.set_day_offset(conn, DAY, OFF)
        rows = _hr(3)
        st.replace_bucket_day(conn, "heart_rate", "5min", o, 1, DAY, OFF, rows, flags=[0, 1, 0])
    with st.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bucket WHERE flags=1").fetchone()[0] == 1
    assert len(st.buckets("heart_rate", "5min", T0, T0 + 86400_000)) == 2
    assert len(st.buckets("heart_rate", "5min", T0, T0 + 86400_000, include_flagged=True)) == 3


def test_batch_log_and_duplicates(st):
    with st.write() as conn:
        assert not st.has_batch(conn, "b1")
        st.log_batch(conn, "b1", 1000, 1, 10, 0, "ok", {"unknown_metric": 2})
        assert st.has_batch(conn, "b1")


def test_delivered_table_api(st):
    st.record_delivery("w-20261020-aaaa", ts=1000, turn_key="wt_1", surface="android",
                       provider="anthropic", model="m", tool="wellness_summary",
                       args={"period": "last_night"}, categories=["sleep"],
                       payload={"ok": True})
    st.record_delivery("w-20261020-bbbb", ts=2000, turn_key=None, surface="android",
                       provider="local", model=None, tool="wellness_series", args={},
                       categories=["heart"], payload={"ok": True})
    assert [d["ref"] for d in st.deliveries_for_turn("wt_1")] == ["w-20261020-aaaa"]
    assert {d["ref"] for d in st.unmatched_deliveries(since_ms=0)} == \
        {"w-20261020-aaaa", "w-20261020-bbbb"}
    st.mark_masked(["w-20261020-aaaa"], at=3000)
    assert [d["ref"] for d in st.unmatched_deliveries(since_ms=0)] == ["w-20261020-bbbb"]
    assert st.get_delivery("w-20261020-aaaa")["payload"] == {"ok": True}
    log = st.disclosures(limit=10)
    assert [d["ref"] for d in log] == ["w-20261020-bbbb", "w-20261020-aaaa"]   # newest first
    assert "payload" not in log[0]
    with pytest.raises(sqlite3.IntegrityError):
        st.record_delivery("w-20261020-aaaa", ts=1, turn_key=None, surface="android",
                           provider="x", model=None, tool="t", args={}, categories=[],
                           payload={})
    # The contract's positional order, with the time defaulting to now.
    st.record_delivery("w-20261020-cccc", "wt_2", "android", "local", None, "wellness_history",
                       {"start": "2026-10-01"}, ["sleep"], {"ok": True})
    assert st.get_delivery("w-20261020-cccc")["ts"] > 1_700_000_000_000


def test_notices_pending_and_delivered(st):
    nid = st.add_notice("source_moved", "Health moved to a new phone", "Open BlackBox",
                        now=1000)
    st.add_notice("old", "t", "b", now=1000, expires_at=1500)
    pending = st.pending_notices(now=2000)
    assert [n["id"] for n in pending] == [nid]
    assert set(pending[0]) == {"id", "kind", "title", "body", "created_at"}
    st.mark_notices_delivered([nid], now=2100)
    assert st.pending_notices(now=2200) == []


def test_consent_event_and_task_ref(st):
    st.consent_event(at=1, change={"categories": {"blood_pressure": "ask"}},
                     surface="android", policy_version="2026-10-29")
    st.add_task_ref("task-1", now=5)
    st.add_task_ref("task-1", now=6)
    assert st.task_refs() == ["task-1"]
    with st.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM consent_event").fetchone()[0] == 1


def test_write_lock_is_per_operator():
    a1 = store.write_lock("0123456789abcdef")
    a2 = store.write_lock("0123456789abcdef")
    b = store.write_lock("fedcba9876543210")
    assert a1 is a2 and a1 is not b


def test_concurrent_writers_serialize(st):
    errors = []

    def worker(i):
        try:
            with st.write() as conn:
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                             (f"k{i}", str(i)))
        except Exception as e:  # pragma: no cover - the assertion reports it
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with st.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'k%'").fetchone()[0] == 20


def test_no_leaked_connections(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "w")
    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        s = store.open_store("aaaaaaaaaaaaaaaa")
        with s.write() as conn:
            conn.execute("INSERT INTO meta(key, value) VALUES ('a', 'b')")
        with s.read() as conn:
            conn.execute("SELECT * FROM meta").fetchall()
        s.close()
        gc.collect()
    # The file can be unlinked straight away (nothing holds it open).
    removed = store.delete_store_files("aaaaaaaaaaaaaaaa")
    assert removed and not s.path.exists()


def test_delete_store_files_removes_sidecars(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "w")
    s = store.open_store("bbbbbbbbbbbbbbbb")
    with s.write() as conn:
        conn.execute("INSERT INTO meta(key, value) VALUES ('a', 'b')")
    s.close()
    for suffix in ("-wal", "-shm"):
        s.path.with_name(s.path.name + suffix).write_bytes(b"x")
    store.delete_store_files("bbbbbbbbbbbbbbbb")
    assert list((tmp_path / "w").iterdir()) == []


def test_day_bounds_and_local_day():
    start, end = store.day_bounds("2026-10-20", -14400)
    assert end - start == 86_400_000
    assert store.local_day(start, -14400) == "2026-10-20"
    assert store.local_day(end - 1, -14400) == "2026-10-20"
    assert store.local_day(end, -14400) == "2026-10-21"


def test_an_erased_store_is_never_brought_back(tmp_path, monkeypatch):
    """Only open_store(create=True) makes the file. A Store left over from
    before an erase (a request in flight) gets StoreGone on read and on
    write, and nothing re-creates the file or a sidecar."""
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "w")
    s = store.open_store("cccccccccccccccc")
    with s.write() as conn:
        conn.execute("INSERT INTO meta(key, value) VALUES ('a', 'b')")
    store.delete_store_files("cccccccccccccccc")
    with pytest.raises(store.StoreGone):
        s.meta_get("a")
    with pytest.raises(store.StoreGone):
        s.meta_set("a", "c")
    with pytest.raises(store.StoreGone):
        store.open_store("cccccccccccccccc", create=False)
    with pytest.raises(store.StoreGone):
        store.Store("cccccccccccccccc").init(create=False)
    assert list((tmp_path / "w").iterdir()) == []
    assert store.open_store("cccccccccccccccc").meta_get("a") is None   # a NEW one, empty


def test_a_flagged_multi_year_session_touches_one_day(tmp_path, monkeypatch):
    """A bogus sleep session reaching back years is stored flagged; it marks
    only its end day dirty and never stretches the span coverage rows are
    back-filled over."""
    import time as _time

    from Orchestrator.wellness import ingest, schema
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "w")
    s = store.open_store("dddddddddddddddd")
    now = store.day_bounds(DAY, OFF)[0] + 12 * 3_600_000
    batch = {"schema": "bbx.wellness.ingest/1", "batch_id": "b-long",
             "enrollment_id": "en_0000000a", "epoch": 1, "origins": [{"key": "o1", "package": "com.example.app",
                                      "device_type": "WATCH"}],
             "sessions": [{"kind": "sleep", "hc_id": "s-bogus", "origin": "o1",
                           "start": now - 20 * 365 * 86_400_000, "end": now - 3_600_000,
                           "offset_s": OFF, "stages": []}]}
    t0 = _time.monotonic()
    res = ingest.process_batch(s, schema.IngestBatch.model_validate(batch),
                               enrollment_id="en_0000000a", epoch=1, now_ms=now)
    assert res.flagged == 1 and res.dirty_days == [DAY]
    assert s.data_span() is None
    assert _time.monotonic() - t0 < 2.0


def test_an_origin_is_saved_only_when_a_kept_row_names_it(st):
    """Ingest resolves an origin on its first KEPT row (ingest.py): rows past
    the clock, an empty group or an empty replace day list no source; an
    empty replace day for an origin already saved still clears that day."""
    from Orchestrator.wellness import ingest, schema
    now = T0 + 12 * 3_600_000
    late = now + 2 * 86_400_000
    origins = [{"key": "w", "package": "com.example.watch", "device_type": "WATCH"},
               {"key": "p", "package": "com.example.phone", "device_type": "PHONE"}]

    def run(batch_id, **parts):
        batch = {"schema": "bbx.wellness.ingest/1", "batch_id": batch_id,
                 "enrollment_id": "en_0000000a", "epoch": 1, "origins": origins, **parts}
        return ingest.process_batch(st, schema.IngestBatch.model_validate(batch),
                                    enrollment_id="en_0000000a", epoch=1, now_ms=now)

    def steps_today():
        return st.buckets("steps", "1h", T0, T0 + 86_400_000)

    res = run("b-dropped", buckets=[
        {"metric": "steps", "grain": "1h", "origin": "w", "rows": [[late, 10.0]]},
        {"metric": "steps", "grain": "1h", "origin": "p", "rows": []}],
        sessions=[{"kind": "sleep", "hc_id": "s1", "origin": "w", "start": late,
                   "end": late + 3_600_000, "offset_s": OFF, "stages": []}],
        readings=[{"metric": "spo2", "hc_id": "r1", "origin": "p", "t": late,
                   "offset_s": OFF, "v": 97.0}])
    assert (res.accepted, res.dropped) == (0, 3)
    assert st.origins() == {}
    empty_day = {"metric": "steps", "grain": "1h", "origin": "w", "replace_day": DAY,
                 "offset_s": OFF, "rows": []}
    run("b-empty-day", buckets=[empty_day])
    assert st.origins() == {}
    run("b-kept", buckets=[dict(empty_day, rows=[[T0 + 3_600_000, 100.0]])])
    [(oid, origin)] = st.origins().items()
    assert origin["package"] == "com.example.watch" and len(steps_today()) == 1
    run("b-cleared", buckets=[empty_day])
    assert steps_today() == [] and list(st.origins()) == [oid]


def test_the_span_ignores_flagged_rows(st):
    with st.write() as conn:
        st.set_day_offset(conn, DAY, OFF)
        oid = st.ensure_origin(conn, {"package": "p"})
        st.upsert_buckets(conn, "steps", "1h", oid, 1, [[T0 + 3_600_000, 100.0]], [0])
        st.upsert_buckets(conn, "steps", "1h", oid, 1,
                          [[T0 - 900 * 86_400_000, 999_999.0]], [1])
        st.upsert_reading(conn, 1, {"hc_id": "r-old", "metric": "spo2",
                                    "t": T0 - 400 * 86_400_000, "offset_s": OFF,
                                    "origin_id": oid, "v": 12.0, "flags": 1})
    assert st.data_span() == (DAY, DAY)


def test_unreferenced_store_files_are_counted(tmp_path, monkeypatch):
    from Orchestrator.tests.wellness_support import sandbox, write_record
    sandbox(tmp_path, monkeypatch)
    write_record(op_key="1111111111111111", detached_op_keys=["2222222222222222"])
    for k in ("1111111111111111", "2222222222222222", "3333333333333333"):
        store.open_store(k)
    assert store.unreferenced_dbs() == ["3333333333333333"]
