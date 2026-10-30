"""One SQLite file per operator: data/wellness/<op_key>.db (spec §3.4).

Stdlib sqlite3, WAL, STRICT tables, WITHOUT ROWID for the buckets and sleep
stages. The file is created 0600 before SQLite opens it (SQLite gives the
-wal/-shm files the main file's mode) inside the 0700 data/wellness/ dir, and
secure_all() re-applies both at boot. The name is a random op_key, never the
operator's name, and the whole store is erased with one unlink (plus its
sidecars).

Times are Unix milliseconds (UTC). A row's local day comes from the zone
offsets the phone sent (meta "tzoff:<day>": the offset at that day's local
midnight); a day with none uses the nearest earlier day that has one. A day
runs from its own midnight to the next day's, so a spring-forward day is 23
hours and a fall-back day 25 (Offsets). A bucket group's offset is the one at
the start of its day and is authoritative; a session or reading carries the
offset at its own time, which only fills a day nobody has stated yet, or,
when it differs from its day's, says the zone changed inside that day (the
next day's offset).

The effective-epoch rule: a re-enrolled phone (a new epoch) re-sends history,
so for each local day and metric every reader keeps only the rows of the
HIGHEST epoch that has data for that day, and nothing is double-counted.

Writers hold the operator's write lock (one RLock per op_key, shared by every
Store object of that operator) and one BEGIN IMMEDIATE transaction. Every
connection is opened per operation and closed after it, so nothing holds the
file when erase unlinks it.

Only open_store(op_key) (create=True) ever creates the file. Every connection
opens it with SQLite's mode=rw, so a store that erase unlinked is never
brought back by a late writer or reader: they get StoreGone instead.

Retention (spec §3.6, decision D7; Store.prune, prune_all, run hourly by
Orchestrator/maintenance.py): with [wellness] detail_years set, buckets,
sessions and readings older than that are deleted and the rollups, coverage,
baselines and digests stay. The first day whose detail is kept is the
"detail horizon" (meta); the days before it are frozen: nothing rebuilds
them from the little detail left, and ingest drops rows for them
("past_retention"). The horizon only moves forward, and never more than two
days past the newest detail the store holds, so a store keeps collecting
whatever the clock said. Retention reads its own clock, which moves at most a
day per pass, so a box clock that jumps years ahead prunes a day more at
most. The ingest log rolls over at 90 days and the delivered log at 400,
whatever the setting.
"""
from __future__ import annotations

import bisect
import contextlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

from Orchestrator.wellness import paths

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DAY_MS = 86_400_000
HC_MERGED = "hc_merged"
# Inside a day whose two offsets differ (a DST change, or travel), a
# wall-clock time before 02:00 reads on the day's first offset and a later
# one on the second. That is exact where the change happens at 02:00 on the
# first offset: all of North America (the repeated 01:00-02:00 of a
# fall-back night reads 01:xx both times), and in Europe, where every change
# is at 01:00 UTC, the central-European spring and the British autumn
# changes. The other European changes (the central-European autumn at 03:00
# local, the British spring at 01:00, eastern Europe at 03:00 and 04:00),
# the Australian and New Zealand autumn change (03:00) and the few zones
# that change at midnight read an hour off between the real change and
# 02:00; a day's bounds and sums never depend on the switch. Travel is
# approximate the same way; exact everywhere would need the zone's name.
ZONE_SWITCH_MIN = 120
# A day can run this many hours past its 24th (a fall-back day is 25; flying
# west across zones a little more). A replace never reaches further than
# that past the day's end, and ingest drops a group's rows beyond it.
MAX_DAY_STRETCH_H = 3
MAX_DAY_STRETCH_MS = MAX_DAY_STRETCH_H * 3_600_000

DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT) STRICT;
CREATE TABLE IF NOT EXISTS epoch(epoch INTEGER PRIMARY KEY, enrollment_id TEXT, device_id TEXT, model TEXT,
                   started_at INTEGER, ended_at INTEGER, earliest_available TEXT) STRICT;
CREATE TABLE IF NOT EXISTS origin(origin_id INTEGER PRIMARY KEY, package TEXT, label TEXT, device_type TEXT,
                    manufacturer TEXT, model TEXT, first_seen INTEGER, last_seen INTEGER) STRICT;
CREATE TABLE IF NOT EXISTS bucket(metric TEXT, grain TEXT, ts INTEGER, origin_id INTEGER, epoch INTEGER,
                    avg REAL, min REAL, max REAL, sum REAL, n INTEGER, flags INTEGER DEFAULT 0,
                    PRIMARY KEY(metric, grain, ts, origin_id, epoch)) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS session(epoch INTEGER, hc_id TEXT, kind TEXT, origin_id INTEGER, start_ts INTEGER,
                     end_ts INTEGER, tz_offset_s INTEGER, subtype INTEGER, attrs TEXT,
                     last_modified INTEGER, PRIMARY KEY(epoch, hc_id)) STRICT;
CREATE TABLE IF NOT EXISTS stage(epoch INTEGER, hc_id TEXT, start_ts INTEGER, end_ts INTEGER, code INTEGER,
                   PRIMARY KEY(epoch, hc_id, start_ts)) STRICT, WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS reading(epoch INTEGER, hc_id TEXT, metric TEXT, ts INTEGER, tz_offset_s INTEGER,
                     origin_id INTEGER, v1 REAL, v2 REAL, attrs TEXT, flags INTEGER DEFAULT 0,
                     last_modified INTEGER, PRIMARY KEY(epoch, hc_id)) STRICT;
CREATE TABLE IF NOT EXISTS rollup(metric TEXT, grain TEXT, bucket TEXT, n INTEGER, value REAL, median REAL,
                    p10 REAL, min REAL, max REAL, coverage TEXT, method TEXT, computed_at INTEGER,
                    PRIMARY KEY(metric, grain, bucket)) STRICT;
CREATE TABLE IF NOT EXISTS baseline(metric TEXT, as_of TEXT, window_days INTEGER, n INTEGER, median REAL,
                      mad REAL, PRIMARY KEY(metric, as_of)) STRICT;
CREATE TABLE IF NOT EXISTS coverage_day(local_day TEXT PRIMARY KEY, day_wear_h REAL, night_worn INTEGER,
                          hr_gap_s REAL, hr_mode TEXT, history_capped INTEGER, troubles TEXT) STRICT;
CREATE TABLE IF NOT EXISTS digest(kind TEXT, period TEXT, body TEXT, computed_at INTEGER,
                    PRIMARY KEY(kind, period)) STRICT;
CREATE TABLE IF NOT EXISTS ingest_log(batch_id TEXT PRIMARY KEY, received_at INTEGER, epoch INTEGER,
                        n_rows INTEGER, n_deleted INTEGER, status TEXT, dropped TEXT) STRICT;
CREATE TABLE IF NOT EXISTS delivered(ref TEXT PRIMARY KEY, ts INTEGER, turn_key TEXT, surface TEXT,
                       provider TEXT, model TEXT, tool TEXT, args TEXT, categories TEXT,
                       payload TEXT, masked_at INTEGER) STRICT;
CREATE TABLE IF NOT EXISTS notice(id TEXT PRIMARY KEY, created_at INTEGER, kind TEXT, title TEXT, body TEXT,
                    delivered_at INTEGER, expires_at INTEGER) STRICT;
CREATE TABLE IF NOT EXISTS consent_event(at INTEGER, change TEXT, surface TEXT, policy_version TEXT) STRICT;
CREATE TABLE IF NOT EXISTS task_ref(task_id TEXT PRIMARY KEY, created_at INTEGER) STRICT;
CREATE INDEX IF NOT EXISTS session_kind_end ON session(kind, end_ts);
CREATE INDEX IF NOT EXISTS reading_metric_ts ON reading(metric, ts);
CREATE INDEX IF NOT EXISTS delivered_turn ON delivered(turn_key);
"""

SIDECARS = ("-wal", "-shm", "-journal")

# Retention (spec §3.6): the logs roll over whatever detail_years says.
INGEST_LOG_KEEP_DAYS = 90
DELIVERED_KEEP_DAYS = 400
META_DETAIL_HORIZON = "detail_horizon"
# Retention keeps its own clock, which moves at most a day per pass past the
# last pass (a store's first pass: past its newest batch). The pass is hourly,
# so a box that was off catches up a day every hour, and one wrong reading of
# the box clock, years ahead, cannot prune years of detail or roll the logs
# over. A clock that reads behind prunes nothing and is not kept.
META_PRUNE_CLOCK = "prune_clock"
PRUNE_CLOCK_STEP_MS = DAY_MS

_LOCKS: Dict[str, threading.RLock] = {}
_INITIALIZED: Set[str] = set()      # store files this process has applied the DDL to
_LOCKS_GUARD = threading.Lock()


def write_lock(op_key: str) -> threading.RLock:
    """The operator's write lock (one per op_key for the whole process)."""
    with _LOCKS_GUARD:
        lock = _LOCKS.get(op_key)
        if lock is None:
            lock = _LOCKS[op_key] = threading.RLock()
        return lock


def new_op_key() -> str:
    return secrets.token_hex(8)


def _now_ms() -> int:
    return int(time.time() * 1000)


# ------------------------------------------------------------------ time

_EPOCH = date(1970, 1, 1)


def _midnight_ms(day: str) -> int:
    """00:00 UTC of a calendar day, in Unix ms."""
    return (date.fromisoformat(day) - _EPOCH).days * DAY_MS


def day_bounds(day: str, offset_s: int, next_offset_s: Optional[int] = None) -> Tuple[int, int]:
    """[start, end) of a local day in Unix ms: from its midnight at `offset_s`
    to the next day's midnight at `next_offset_s` (the next day's offset; the
    same when omitted, which is a 24-hour day)."""
    m = _midnight_ms(day)
    start = m - int(offset_s) * 1000
    nxt = offset_s if next_offset_s is None else next_offset_s
    return start, max(start, m + DAY_MS - int(nxt) * 1000)


def local_day(ts_ms: int, offset_s: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000 + offset_s, tz=timezone.utc).date().isoformat()


def local_clock(ts_ms: int, offset_s: int) -> str:
    """"07:10": the wall-clock time of an instant at a zone offset."""
    return datetime.fromtimestamp(ts_ms / 1000 + offset_s, tz=timezone.utc).strftime("%H:%M")


def add_days(day: str, n: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def day_range(first: str, last: str) -> List[str]:
    a, b = date.fromisoformat(first), date.fromisoformat(last)
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def detail_horizon_day(today: str, years: int) -> str:
    """The first local day a `years` retention keeps: the same date `years`
    earlier (29 February falls back to the 28th)."""
    d = date.fromisoformat(today)
    try:
        return d.replace(year=d.year - years).isoformat()
    except ValueError:
        return d.replace(year=d.year - years, day=28).isoformat()


class Offsets:
    """The per-day zone offsets of one store (the offset at each day's local
    midnight), with nearest-earlier-day fallback. A day ends where the next
    one starts, so the days tile the timeline across a zone change."""

    def __init__(self, by_day: Dict[str, int]):
        self.by_day = dict(by_day)
        self._days = sorted(self.by_day)
        self._bounds: Dict[str, Tuple[int, int]] = {}
        self._hit: Optional[Tuple[int, int, str]] = None

    def for_day(self, day: str) -> int:
        if day in self.by_day:
            return self.by_day[day]
        if not self._days:
            return 0
        i = bisect.bisect_left(self._days, day)
        near = self._days[i - 1] if i > 0 else self._days[0]
        return self.by_day[near]

    def knows(self, day: str) -> bool:
        return day in self.by_day

    def set(self, day: str, offset_s: int) -> None:
        """Record a day's offset in this view (store.set_day_offset persists it)."""
        if day not in self.by_day:
            bisect.insort(self._days, day)
        self.by_day[day] = int(offset_s)
        self._bounds.clear()
        self._hit = None

    def latest(self) -> int:
        return self.by_day[self._days[-1]] if self._days else 0

    def bounds(self, day: str) -> Tuple[int, int]:
        b = self._bounds.get(day)
        if b is None:
            b = self._bounds[day] = day_bounds(day, self.for_day(day),
                                               self.for_day(add_days(day, 1)))
        return b

    def day_of(self, ts_ms: int) -> str:
        hit = self._hit
        if hit is not None and hit[0] <= ts_ms < hit[1]:
            return hit[2]
        day = local_day(ts_ms, self.for_day(local_day(ts_ms, self.latest())))
        for _ in range(4):
            start, end = self.bounds(day)
            if ts_ms < start:
                day = add_days(day, -1)
            elif ts_ms >= end:
                day = add_days(day, 1)
            else:
                self._hit = (start, end, day)
                break
        return day

    def _pair(self, day: str) -> Tuple[int, int]:
        return self.for_day(day), self.for_day(add_days(day, 1))

    def offset_at(self, ts_ms: int) -> int:
        """The zone offset in effect at an instant (for showing local times)."""
        day = self.day_of(ts_ms)
        first, second = self._pair(day)
        if first == second:
            return first
        switch = _midnight_ms(day) + ZONE_SWITCH_MIN * 60_000 - first * 1000
        return first if ts_ms < switch else second

    def at_local(self, day: str, minutes: float) -> int:
        """The instant of a wall-clock time on a local day (minutes after its
        midnight)."""
        first, second = self._pair(day)
        off = first if first == second or minutes < ZONE_SWITCH_MIN else second
        return _midnight_ms(day) + int(minutes * 60_000) - off * 1000

    def local_minutes(self, day: str, ts_ms: int) -> float:
        """Minutes after the local midnight of `day` that an instant reads on
        the wall clock (negative the evening before)."""
        first, second = self._pair(day)
        m = (ts_ms + first * 1000 - _midnight_ms(day)) / 60_000.0
        if first == second or m < ZONE_SWITCH_MIN:
            return m
        return (ts_ms + second * 1000 - _midnight_ms(day)) / 60_000.0


# ------------------------------------------------------------------ files

def _create_private(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    os.close(fd)


def delete_store_files(op_key: str) -> List[str]:
    """Unlink the operator's DB and its sidecars. Returns what was removed."""
    db = paths.db_path(op_key)
    removed = []
    with write_lock(op_key):
        for p in (db, *(db.with_name(db.name + s) for s in SIDECARS)):
            try:
                p.unlink()
                removed.append(str(p))
            except FileNotFoundError:
                pass
        _INITIALIZED.discard(str(db))
    return removed


def secure_all() -> Dict[str, int]:
    """Boot: data/wellness/ at 0700 and every file in it (and the key file)
    at 0600. Returns counts only."""
    from Orchestrator.migrations.tasks_db_location import secure_db_files
    d = paths.ensure_private_dir()
    dbs = files = 0
    for p in sorted(d.iterdir()):
        if p.is_file() and p.suffix == ".db":
            secure_db_files(p, log_tag="WELLNESS")
            dbs += 1
        elif p.is_file() and not p.name.endswith(SIDECARS):
            try:
                os.chmod(p, 0o600)
                files += 1
            except OSError:
                pass
    keys = paths.keys_path()
    if keys.exists():
        try:
            os.chmod(keys, 0o600)
            files += 1
        except OSError:
            pass
    return {"dirs": 1, "dbs": dbs, "files": files}


def unreferenced_dbs() -> List[str]:
    """op_keys of store files in data/wellness/ that no enrollment points at
    (attached or detached). Boot reports them; nothing deletes them blindly,
    since an unreadable enrollments.json would make every store look so."""
    from Orchestrator.wellness import enroll
    d = paths.data_dir()
    if not d.is_dir():
        return []
    known = enroll.referenced_op_keys()
    return sorted(p.stem for p in d.iterdir()
                  if p.is_file() and p.suffix == ".db" and p.stem not in known)


def _dumps(value) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _loads(text):
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ store

class StoreGone(Exception):
    """The operator's store file is not there (erased, or never created)."""


class Store:
    """One operator's store. Cheap to construct; holds no open connection."""

    def __init__(self, op_key: str):
        self.op_key = op_key
        self.path = paths.db_path(op_key)

    # -- connections ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # mode=rw: SQLite may add the -wal/-shm sidecars but never creates the
        # database itself, so a connection after erase fails instead of
        # quietly starting an orphan store.
        if not self.path.exists():
            raise StoreGone(self.op_key)
        try:
            conn = sqlite3.connect(Path(self.path).absolute().as_uri() + "?mode=rw", uri=True,
                                   timeout=30, isolation_level=None)
        except sqlite3.OperationalError:
            if not self.path.exists():
                raise StoreGone(self.op_key) from None
            raise
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.OperationalError:
            conn.close()
            if not self.path.exists():
                raise StoreGone(self.op_key) from None
            raise
        return conn

    def init(self, create: bool = True) -> "Store":
        """Apply the schema (idempotent). create=False never makes the file:
        a missing store raises StoreGone."""
        from Orchestrator.migrations.tasks_db_location import secure_db_files
        key = str(self.path)
        if key in _INITIALIZED and self.path.exists():
            return self
        if not create and not self.path.exists():
            raise StoreGone(self.op_key)
        paths.ensure_private_dir()
        with write_lock(self.op_key):
            if create:
                _create_private(self.path)
            conn = self._connect()
            try:
                conn.executescript(DDL)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            finally:
                conn.close()
            secure_db_files(self.path, log_tag="WELLNESS")
            _INITIALIZED.add(key)
        return self

    @contextlib.contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with write_lock(self.op_key):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
            finally:
                conn.close()

    @contextlib.contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def close(self) -> None:
        """Nothing stays open between operations; kept for symmetry."""

    # -- meta ----------------------------------------------------------------

    def meta_get(self, key: str, default=None):
        with self.read() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return _loads(row[0]) if row else default

    def meta_set(self, key: str, value, conn: Optional[sqlite3.Connection] = None) -> None:
        sql = "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)"
        if conn is not None:
            conn.execute(sql, (key, _dumps(value)))
            return
        with self.write() as c:
            c.execute(sql, (key, _dumps(value)))

    def set_day_offset(self, conn: sqlite3.Connection, day: str, offset_s: int) -> None:
        """A day's offset as a bucket group states it (replaces any earlier one)."""
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                     (f"tzoff:{day}", str(int(offset_s))))

    def hint_day_offset(self, conn: sqlite3.Connection, day: str, offset_s: int) -> None:
        """A day's offset inferred from a record's own offset: only fills a day
        that has none."""
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                     (f"tzoff:{day}", str(int(offset_s))))

    def offsets(self, conn: Optional[sqlite3.Connection] = None) -> Offsets:
        def load(c):
            rows = c.execute("SELECT key, value FROM meta WHERE key LIKE 'tzoff:%'").fetchall()
            return Offsets({k[6:]: int(v) for k, v in rows})
        if conn is not None:
            return load(conn)
        with self.read() as c:
            return load(c)

    # -- epochs and origins ----------------------------------------------------

    def record_epoch(self, conn, epoch: int, enrollment_id: str, device_id: Optional[str],
                     model: Optional[str], started_at: int) -> None:
        conn.execute("INSERT OR IGNORE INTO epoch(epoch, enrollment_id, device_id, model, "
                     "started_at) VALUES (?, ?, ?, ?, ?)",
                     (epoch, enrollment_id, device_id, model, started_at))

    def end_epoch(self, conn, epoch: int, ended_at: int) -> None:
        conn.execute("UPDATE epoch SET ended_at=? WHERE epoch=? AND ended_at IS NULL",
                     (ended_at, epoch))

    @staticmethod
    def _origin_key(origin: dict) -> tuple:
        return (origin.get("package") or "", origin.get("device_type"),
                origin.get("manufacturer"), origin.get("model"))

    def find_origin(self, conn, origin: dict) -> Optional[int]:
        """The id of an origin already saved, or None (nothing is written)."""
        row = conn.execute(
            "SELECT origin_id FROM origin WHERE package IS ? AND device_type IS ? "
            "AND manufacturer IS ? AND model IS ?", self._origin_key(origin)).fetchone()
        return int(row[0]) if row else None

    def ensure_origin(self, conn, origin: dict, now: int = 0) -> int:
        key = self._origin_key(origin)
        row = conn.execute(
            "SELECT origin_id FROM origin WHERE package IS ? AND device_type IS ? "
            "AND manufacturer IS ? AND model IS ?", key).fetchone()
        if row:
            conn.execute("UPDATE origin SET last_seen=MAX(COALESCE(last_seen, 0), ?), "
                         "label=COALESCE(?, label) WHERE origin_id=?",
                         (now, origin.get("label"), row[0]))
            return int(row[0])
        cur = conn.execute(
            "INSERT INTO origin(package, label, device_type, manufacturer, model, first_seen, "
            "last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key[0], origin.get("label"), key[1], key[2], key[3], now, now))
        return int(cur.lastrowid)

    def origins(self) -> Dict[int, dict]:
        with self.read() as conn:
            return {r["origin_id"]: dict(r) for r in conn.execute("SELECT * FROM origin")}

    # -- buckets ---------------------------------------------------------------

    def replace_bucket_day(self, conn, metric: str, grain: str, origin_id: int, epoch: int,
                           day: str, offset_s: int, rows: List[list],
                           flags: Optional[List[int]] = None,
                           next_offset_s: Optional[int] = None) -> int:
        """Replace one (metric, grain, origin, epoch) local day with `rows`:
        [t, sum] for activity, [t, avg, min, max, n] for heart rate. The day
        runs from its midnight at `offset_s` to the next one at
        `next_offset_s` (23 or 25 hours across a DST change), and at least to
        the last row the phone put in it, but never more than
        MAX_DAY_STRETCH_H past its end: a stray row must not widen the delete
        into the days after it (ingest drops such rows before they get here)."""
        start, end = day_bounds(day, offset_s, next_offset_s)
        if rows:
            last = max(int(r[0]) for r in rows) + 1
            end = max(end, min(last, end + MAX_DAY_STRETCH_MS))
        conn.execute("DELETE FROM bucket WHERE metric=? AND grain=? AND origin_id=? AND epoch=? "
                     "AND ts>=? AND ts<?", (metric, grain, origin_id, epoch, start, end))
        return self.upsert_buckets(conn, metric, grain, origin_id, epoch, rows, flags)

    def upsert_buckets(self, conn, metric: str, grain: str, origin_id: int, epoch: int,
                       rows: List[list], flags: Optional[List[int]] = None) -> int:
        out = []
        for i, row in enumerate(rows):
            flag = int(flags[i]) if flags else 0
            if len(row) == 2:
                out.append((metric, grain, int(row[0]), origin_id, epoch, None, None, None,
                            row[1], None, flag))
            else:
                n = row[4] if len(row) > 4 else None
                out.append((metric, grain, int(row[0]), origin_id, epoch, row[1],
                            row[2] if len(row) > 2 else None, row[3] if len(row) > 3 else None,
                            None, int(n) if n is not None else None, flag))
        conn.executemany("INSERT OR REPLACE INTO bucket(metric, grain, ts, origin_id, epoch, avg, "
                         "min, max, sum, n, flags) VALUES (?,?,?,?,?,?,?,?,?,?,?)", out)
        return len(out)

    def buckets(self, metric: str, grain: str, start_ms: int, end_ms: int, *,
                include_flagged: bool = False, conn=None, offsets: Optional[Offsets] = None
                ) -> List[dict]:
        sql = ("SELECT metric, grain, ts, origin_id, epoch, avg, min, max, sum, n, flags "
               "FROM bucket WHERE metric=? AND grain=? AND ts>=? AND ts<?"
               + ("" if include_flagged else " AND flags=0") + " ORDER BY ts")
        if conn is None:
            with self.read() as c:
                rows = [dict(r) for r in c.execute(sql, (metric, grain, start_ms, end_ms))]
                offsets = offsets or self.offsets(c)
        else:
            rows = [dict(r) for r in conn.execute(sql, (metric, grain, start_ms, end_ms))]
            offsets = offsets or self.offsets(conn)
        return _effective(rows, offsets, "ts")

    # -- sessions and readings ---------------------------------------------------

    def upsert_session(self, conn, epoch: int, s: dict) -> None:
        conn.execute("DELETE FROM stage WHERE epoch=? AND hc_id=?", (epoch, s["hc_id"]))
        conn.execute(
            "INSERT OR REPLACE INTO session(epoch, hc_id, kind, origin_id, start_ts, end_ts, "
            "tz_offset_s, subtype, attrs, last_modified) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (epoch, s["hc_id"], s["kind"], s.get("origin_id"), int(s["start"]), int(s["end"]),
             s.get("offset_s"), s.get("subtype"), _dumps(s.get("attrs")),
             s.get("last_modified")))
        stages = [(epoch, s["hc_id"], int(a), int(b), int(c)) for a, b, c in s.get("stages") or ()]
        conn.executemany("INSERT OR REPLACE INTO stage(epoch, hc_id, start_ts, end_ts, code) "
                         "VALUES (?,?,?,?,?)", stages)

    def upsert_reading(self, conn, epoch: int, r: dict) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO reading(epoch, hc_id, metric, ts, tz_offset_s, origin_id, v1, "
            "v2, attrs, flags, last_modified) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (epoch, r["hc_id"], r["metric"], int(r["t"]), r.get("offset_s"), r.get("origin_id"),
             r["v"], r.get("v2"), _dumps(r.get("attrs")), int(r.get("flags") or 0),
             r.get("last_modified")))

    def delete_hc_ids(self, conn, ids: Iterable[str]) -> Set[str]:
        """Delete sessions/readings by Health Connect id (every epoch). Returns
        the local days they touched (dirty days)."""
        days: Set[str] = set()
        offsets = self.offsets(conn)
        for hc_id in ids:
            for row in conn.execute("SELECT start_ts, end_ts, tz_offset_s FROM session "
                                    "WHERE hc_id=?", (hc_id,)).fetchall():
                days.update(day_range(offsets.day_of(row[0]), offsets.day_of(row[1])))
                if row[2] is not None:
                    days.update(day_range(local_day(row[0], row[2]), local_day(row[1], row[2])))
            for row in conn.execute("SELECT ts, tz_offset_s FROM reading WHERE hc_id=?",
                                    (hc_id,)).fetchall():
                days.add(offsets.day_of(row[0]))
                if row[1] is not None:
                    days.add(local_day(row[0], row[1]))
            conn.execute("DELETE FROM session WHERE hc_id=?", (hc_id,))
            conn.execute("DELETE FROM stage WHERE hc_id=?", (hc_id,))
            conn.execute("DELETE FROM reading WHERE hc_id=?", (hc_id,))
        return days

    def sessions(self, kind: str, start_ms: int, end_ms: int, *, conn=None,
                 include_flagged: bool = False) -> List[dict]:
        """Sessions of `kind` that END inside [start, end), newest epoch per
        day, with their stages."""
        def load(c):
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM session WHERE kind=? AND end_ts>=? AND end_ts<? ORDER BY start_ts",
                (kind, start_ms, end_ms))]
            for r in rows:
                r["attrs"] = _loads(r["attrs"]) or {}
                r["stages"] = [tuple(x) for x in c.execute(
                    "SELECT start_ts, end_ts, code FROM stage WHERE epoch=? AND hc_id=? "
                    "ORDER BY start_ts", (r["epoch"], r["hc_id"]))]
            return rows, self.offsets(c)
        if conn is None:
            with self.read() as c:
                rows, offsets = load(c)
        else:
            rows, offsets = load(conn)
        if not include_flagged:
            rows = [r for r in rows if not r["attrs"].get("flags")]
        return _effective(rows, offsets, "end_ts", key="kind")

    def readings(self, metric: str, start_ms: int, end_ms: int, *, conn=None,
                 include_flagged: bool = False) -> List[dict]:
        sql = ("SELECT * FROM reading WHERE metric=? AND ts>=? AND ts<?"
               + ("" if include_flagged else " AND flags=0") + " ORDER BY ts")
        if conn is None:
            with self.read() as c:
                rows = [dict(r) for r in c.execute(sql, (metric, start_ms, end_ms))]
                offsets = self.offsets(c)
        else:
            rows = [dict(r) for r in conn.execute(sql, (metric, start_ms, end_ms))]
            offsets = self.offsets(conn)
        for r in rows:
            r["attrs"] = _loads(r["attrs"]) or {}
        return _effective(rows, offsets, "ts")

    def data_span(self, conn=None) -> Optional[Tuple[str, str]]:
        """(first, last) local day holding any bucket, session or reading that
        is not flagged implausible (one bogus multi-year record must not
        stretch the span every coverage row is back-filled over)."""
        def load(c):
            lo, hi = None, None
            for sql in ("SELECT MIN(ts), MAX(ts) FROM bucket WHERE COALESCE(flags, 0)=0",
                        "SELECT MIN(start_ts), MAX(end_ts) FROM session "
                        "WHERE COALESCE(json_extract(attrs, '$.flags'), 0)=0",
                        "SELECT MIN(ts), MAX(ts) FROM reading WHERE COALESCE(flags, 0)=0"):
                a, b = c.execute(sql).fetchone()
                if a is not None:
                    lo = a if lo is None else min(lo, a)
                    hi = b if hi is None else max(hi, b)
            if lo is None:
                return None
            off = self.offsets(c)
            return off.day_of(lo), off.day_of(hi)
        if conn is not None:
            return load(conn)
        with self.read() as c:
            return load(c)

    # -- ingest log -------------------------------------------------------------

    def has_batch(self, conn, batch_id: str) -> bool:
        return conn.execute("SELECT 1 FROM ingest_log WHERE batch_id=?",
                            (batch_id,)).fetchone() is not None

    def log_batch(self, conn, batch_id: str, received_at: int, epoch: int, n_rows: int,
                  n_deleted: int, status: str, dropped: Optional[dict]) -> None:
        conn.execute("INSERT INTO ingest_log(batch_id, received_at, epoch, n_rows, n_deleted, "
                     "status, dropped) VALUES (?,?,?,?,?,?,?)",
                     (batch_id, received_at, epoch, n_rows, n_deleted, status, _dumps(dropped)))

    # -- retention --------------------------------------------------------------

    def detail_horizon(self, conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
        """The first local day whose detail is kept, once retention has pruned
        (None: everything is kept). The days before it are frozen."""
        if conn is None:
            return self.meta_get(META_DETAIL_HORIZON)
        row = conn.execute("SELECT value FROM meta WHERE key=?",
                           (META_DETAIL_HORIZON,)).fetchone()
        return _loads(row[0]) if row else None

    def detail_cutoff(self, conn: sqlite3.Connection) -> Optional[int]:
        """The instant before which no detail is kept: the start of the day
        before the horizon (the horizon's night needs that evening)."""
        horizon = self.detail_horizon(conn)
        if horizon is None:
            return None
        return self.offsets(conn).bounds(add_days(horizon, -1))[0]

    def _newest_detail(self, conn: sqlite3.Connection) -> Optional[int]:
        """The latest instant any bucket, session or reading holds."""
        newest = None
        for sql in ("SELECT MAX(ts) FROM bucket", "SELECT MAX(end_ts) FROM session",
                    "SELECT MAX(ts) FROM reading"):
            v = conn.execute(sql).fetchone()[0]
            if v is not None:
                newest = v if newest is None else max(newest, v)
        return newest

    def prune(self, now_ms: int, detail_years: Optional[int]) -> Dict[str, int]:
        """Apply retention once. Returns counts of rows removed."""
        counts = dict.fromkeys(("buckets", "sessions", "readings", "ingest_log", "delivered"), 0)
        with self.write() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?",
                               (META_PRUNE_CLOCK,)).fetchone()
            last = _loads(row[0]) if row else None
            last = last if isinstance(last, int) and not isinstance(last, bool) else None
            # The first pass over a store steps from its newest batch instead.
            ref = last if last is not None else conn.execute(
                "SELECT MAX(received_at) FROM ingest_log").fetchone()[0]
            clock = int(now_ms) if ref is None else min(int(now_ms), ref + PRUNE_CLOCK_STEP_MS)
            if last is None or clock > last:
                self.meta_set(META_PRUNE_CLOCK, clock, conn=conn)
            counts["ingest_log"] = conn.execute(
                "DELETE FROM ingest_log WHERE received_at<?",
                (clock - INGEST_LOG_KEEP_DAYS * DAY_MS,)).rowcount
            counts["delivered"] = conn.execute(
                "DELETE FROM delivered WHERE ts<?",
                (clock - DELIVERED_KEEP_DAYS * DAY_MS,)).rowcount
            if not detail_years:
                return counts
            offsets = self.offsets(conn)
            old = self.detail_horizon(conn)
            horizon = detail_horizon_day(offsets.day_of(clock), int(detail_years))
            if old is not None and old > horizon:
                horizon = old              # a longer setting cannot bring detail back
            if horizon != old:
                newest = self._newest_detail(conn)
                if newest is None:
                    return counts          # no detail: nothing to prune, nothing to freeze
                # At most two days past the newest detail's day: all of it can
                # go, but the days after that had nothing pruned and are never
                # frozen, whatever the clock said.
                horizon = min(horizon, add_days(offsets.day_of(newest), 2))
                if old is not None and old > horizon:
                    horizon = old
            cutoff = offsets.bounds(add_days(horizon, -1))[0]
            counts["buckets"] = conn.execute("DELETE FROM bucket WHERE ts<?",
                                             (cutoff,)).rowcount
            conn.execute("DELETE FROM stage WHERE (epoch, hc_id) IN "
                         "(SELECT epoch, hc_id FROM session WHERE end_ts<?)", (cutoff,))
            counts["sessions"] = conn.execute("DELETE FROM session WHERE end_ts<?",
                                              (cutoff,)).rowcount
            counts["readings"] = conn.execute("DELETE FROM reading WHERE ts<?",
                                              (cutoff,)).rowcount
            if horizon != old:
                self.meta_set(META_DETAIL_HORIZON, horizon, conn=conn)
        return counts

    def ingest_stats(self) -> dict:
        with self.read() as conn:
            n, rows, last = conn.execute("SELECT COUNT(*), COALESCE(SUM(n_rows), 0), "
                                         "MAX(received_at) FROM ingest_log").fetchone()
        return {"batches": n, "rows": rows, "last_received_at": last}

    # -- rollups, baselines, coverage, digests ----------------------------------

    def put_rollup(self, conn, metric: str, grain: str, bucket: str, *, n=None, value=None,
                   median=None, p10=None, min=None, max=None, coverage=None, method=None,
                   computed_at: int = 0) -> None:
        conn.execute("INSERT OR REPLACE INTO rollup(metric, grain, bucket, n, value, median, p10, "
                     "min, max, coverage, method, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (metric, grain, bucket, n, value, median, p10, min, max, _dumps(coverage),
                      method, computed_at))

    def drop_rollups(self, conn, grain: str, bucket: str) -> None:
        conn.execute("DELETE FROM rollup WHERE grain=? AND bucket=?", (grain, bucket))

    def rollups(self, grain: str, first: str, last: str, metric: Optional[str] = None,
                conn=None) -> List[dict]:
        sql = "SELECT * FROM rollup WHERE grain=? AND bucket>=? AND bucket<=?"
        args: list = [grain, first, last]
        if metric:
            sql += " AND metric=?"
            args.append(metric)
        sql += " ORDER BY bucket"

        def load(c):
            out = [dict(r) for r in c.execute(sql, args)]
            for r in out:
                r["coverage"] = _loads(r["coverage"])
            return out
        if conn is not None:
            return load(conn)
        with self.read() as c:
            return load(c)

    def put_baseline(self, conn, metric: str, as_of: str, window_days: int, n: int,
                     median, mad) -> None:
        conn.execute("INSERT OR REPLACE INTO baseline(metric, as_of, window_days, n, median, mad) "
                     "VALUES (?,?,?,?,?,?)", (metric, as_of, window_days, n, median, mad))

    def baseline(self, metric: str, as_of: str, conn=None) -> Optional[dict]:
        sql = "SELECT * FROM baseline WHERE metric=? AND as_of<=? ORDER BY as_of DESC LIMIT 1"
        if conn is not None:
            row = conn.execute(sql, (metric, as_of)).fetchone()
        else:
            with self.read() as c:
                row = c.execute(sql, (metric, as_of)).fetchone()
        return dict(row) if row else None

    def put_coverage_day(self, conn, day: str, *, day_wear_h, night_worn, hr_gap_s, hr_mode,
                         history_capped, troubles) -> None:
        conn.execute("INSERT OR REPLACE INTO coverage_day(local_day, day_wear_h, night_worn, "
                     "hr_gap_s, hr_mode, history_capped, troubles) VALUES (?,?,?,?,?,?,?)",
                     (day, day_wear_h, night_worn, hr_gap_s, hr_mode, history_capped,
                      _dumps(troubles or [])))

    def coverage_days(self, first: str, last: str, conn=None) -> Dict[str, dict]:
        sql = "SELECT * FROM coverage_day WHERE local_day>=? AND local_day<=? ORDER BY local_day"

        def load(c):
            out = {}
            for r in c.execute(sql, (first, last)):
                d = dict(r)
                d["troubles"] = _loads(d["troubles"]) or []
                out[d["local_day"]] = d
            return out
        if conn is not None:
            return load(conn)
        with self.read() as c:
            return load(c)

    def put_digest(self, conn, kind: str, period: str, body, computed_at: int) -> None:
        conn.execute("INSERT OR REPLACE INTO digest(kind, period, body, computed_at) "
                     "VALUES (?,?,?,?)", (kind, period, _dumps(body), computed_at))

    def digest(self, kind: str, period: str) -> Optional[dict]:
        with self.read() as conn:
            row = conn.execute("SELECT body FROM digest WHERE kind=? AND period=?",
                               (kind, period)).fetchone()
        return _loads(row[0]) if row else None

    # -- the delivered table (disclosure log + durable mask carry) -------------

    def record_delivery(self, ref: str, turn_key: Optional[str], surface: str,
                        provider: Optional[str], model: Optional[str], tool: str, args,
                        categories, payload, *, ts: Optional[int] = None) -> None:
        """One wellness result reached a model: the disclosure log row, the
        durable mask carry (turn_key) and what "Show numbers" returns."""
        if ts is None:
            ts = _now_ms()
        with self.write() as conn:
            conn.execute("INSERT INTO delivered(ref, ts, turn_key, surface, provider, model, tool, "
                         "args, categories, payload, masked_at) VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                         (ref, ts, turn_key, surface, provider, model, tool, _dumps(args or {}),
                          _dumps(sorted(categories or [])), _dumps(payload)))

    @staticmethod
    def _delivery(row, with_payload: bool = True) -> dict:
        d = dict(row)
        d["args"] = _loads(d.get("args")) or {}
        d["categories"] = _loads(d.get("categories")) or []
        if with_payload:
            d["payload"] = _loads(d.get("payload"))
        else:
            d.pop("payload", None)
        return d

    def ref_exists(self, ref: str) -> bool:
        with self.read() as conn:
            return conn.execute("SELECT 1 FROM delivered WHERE ref=?", (ref,)).fetchone() is not None

    def get_delivery(self, ref: str) -> Optional[dict]:
        with self.read() as conn:
            row = conn.execute("SELECT * FROM delivered WHERE ref=?", (ref,)).fetchone()
        return self._delivery(row) if row else None

    def deliveries_for_turn(self, turn_key: str) -> List[dict]:
        """The turn's rows, oldest first (two in one millisecond in the order
        they were written)."""
        with self.read() as conn:
            rows = conn.execute("SELECT * FROM delivered WHERE turn_key=? ORDER BY ts, rowid",
                                (turn_key,)).fetchall()
        return [self._delivery(r) for r in rows]

    def unmatched_deliveries(self, since_ms: int) -> List[dict]:
        with self.read() as conn:
            rows = conn.execute("SELECT * FROM delivered WHERE masked_at IS NULL AND ts>=? "
                                "ORDER BY ts", (since_ms,)).fetchall()
        return [self._delivery(r) for r in rows]

    def mark_masked(self, refs: Iterable[str], at: int) -> None:
        with self.write() as conn:
            conn.executemany("UPDATE delivered SET masked_at=? WHERE ref=? AND masked_at IS NULL",
                             [(at, r) for r in refs])

    def disclosures(self, limit: int = 200) -> List[dict]:
        with self.read() as conn:
            rows = conn.execute("SELECT ref, ts, turn_key, surface, provider, model, tool, args, "
                                "categories, masked_at FROM delivered ORDER BY ts DESC, ref DESC "
                                "LIMIT ?", (int(limit),)).fetchall()
        return [self._delivery(r, with_payload=False) for r in rows]

    def disclosures_count(self) -> int:
        with self.read() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM delivered").fetchone()[0])

    # -- notices -----------------------------------------------------------------

    def add_notice(self, kind: str, title: str, body: str, *, now: int,
                   expires_at: Optional[int] = None) -> str:
        nid = "nt_" + secrets.token_hex(6)
        with self.write() as conn:
            conn.execute("INSERT INTO notice(id, created_at, kind, title, body, delivered_at, "
                         "expires_at) VALUES (?,?,?,?,?,NULL,?)",
                         (nid, now, kind, title, body, expires_at))
        return nid

    def pending_notices(self, now: int) -> List[dict]:
        with self.read() as conn:
            rows = conn.execute("SELECT id, kind, title, body, created_at FROM notice "
                                "WHERE delivered_at IS NULL AND (expires_at IS NULL OR "
                                "expires_at>?) ORDER BY created_at, id", (now,)).fetchall()
        return [dict(r) for r in rows]

    def mark_notices_delivered(self, ids: Iterable[str], now: int) -> None:
        with self.write() as conn:
            conn.executemany("UPDATE notice SET delivered_at=? WHERE id=? AND delivered_at IS NULL",
                             [(now, i) for i in ids])

    # -- consent events and the tasks.db scrub list -------------------------------

    def consent_event(self, *, at: int, change, surface: str, policy_version: str) -> None:
        with self.write() as conn:
            conn.execute("INSERT INTO consent_event(at, change, surface, policy_version) "
                         "VALUES (?,?,?,?)", (at, _dumps(change), surface, policy_version))

    def add_task_ref(self, task_id: str, now: int) -> None:
        with self.write() as conn:
            conn.execute("INSERT OR IGNORE INTO task_ref(task_id, created_at) VALUES (?, ?)",
                         (task_id, now))

    def task_refs(self) -> List[str]:
        with self.read() as conn:
            return [r[0] for r in conn.execute("SELECT task_id FROM task_ref ORDER BY created_at")]


def _effective(rows: List[dict], offsets: Offsets, ts_key: str, key: str = "metric") -> List[dict]:
    """Keep, per (local day, metric/kind), only the rows of the highest epoch."""
    if not rows:
        return rows
    best: Dict[Tuple[str, str], int] = {}
    days = []
    for r in rows:
        day = offsets.day_of(int(r[ts_key]))
        days.append(day)
        k = (day, r.get(key))
        if r["epoch"] > best.get(k, -1):
            best[k] = r["epoch"]
    return [r for r, day in zip(rows, days) if r["epoch"] == best[(day, r.get(key))]]


_FROM_CONFIG = object()


def prune_all(now_ms: Optional[int] = None, detail_years=_FROM_CONFIG) -> Dict[str, int]:
    """The retention pass over every store file in data/wellness/ (the
    operator's, a detached one, one no enrollment names). Logs one line of
    counts when it removed anything; never a value or an op_key."""
    if now_ms is None:
        now_ms = _now_ms()
    if detail_years is _FROM_CONFIG:
        from Orchestrator import config
        detail_years = config.wellness_detail_years()
    total = dict.fromkeys(("stores", "buckets", "sessions", "readings", "ingest_log",
                           "delivered"), 0)
    d = paths.data_dir()
    if not d.is_dir():
        return total
    for p in sorted(d.iterdir()):
        if not (p.is_file() and p.suffix == ".db" and paths.valid_op_key(p.stem)):
            continue
        try:
            counts = Store(p.stem).init(create=False).prune(now_ms, detail_years)
        except StoreGone:
            continue                     # erased while the pass ran
        except sqlite3.Error as e:
            logger.warning("[WELLNESS] retention skipped a store: %s", type(e).__name__)
            continue
        total["stores"] += 1
        for k, v in counts.items():
            total[k] += v
    if any(v for k, v in total.items() if k != "stores"):
        logger.info("[WELLNESS] retention stores=%d buckets=%d sessions=%d readings=%d "
                    "ingest_log=%d delivered=%d", total["stores"], total["buckets"],
                    total["sessions"], total["readings"], total["ingest_log"],
                    total["delivered"])
    return total


def open_store(op_key: str, create: bool = True) -> Store:
    """The operator's store, created (0600, schema applied) on first use, or
    with create=False only if it exists (else StoreGone)."""
    return Store(op_key).init(create=create)
