"""The batch pipeline (spec §3.3): signed bytes -> duplicate check -> validate
-> one transaction -> recompute the dirty days -> the response.

The route (routes/wellness_routes.py) is a plain `def`, so all of this runs in
the threadpool, never on the event loop. The body arrives capped at 2 MB
compressed; decompress() streams it and stops at 8 MB, so a gzip bomb never
exhausts memory. The signature is checked on the exact bytes received,
before any decompression.

Values: a structurally valid batch is never refused for one implausible value.
Such a row is stored FLAGGED (excluded from every rollup) and counted; a row
this build cannot use (an unknown metric, a grain that does not fit, a time in
the future) is dropped and counted. Both counts go to the ingest log, never
the values.

Consent, in depth: the phone should not read a category that is off, and a
row of one that arrives anyway is dropped here (counted as "category_off",
logged as a count, never stored). The consent is read when the batch is
written, under the store's write lock, so a category turned off while a
batch was in flight is already off; one turned back on starts with the next
push (nothing dropped is kept for later). A workout is activity, so with
heart off it is kept without the heart rate it carries.

Zone offsets (store.py has the day model): a bucket group's offset_s is the
offset at its day's midnight and is stored as the day's; a session or reading
carries the offset at its own time, which fills a day nobody stated yet or,
when it differs from its day's, gives the next day's (the zone changed that
day). Rows a group puts past the day's 24th hour say the same. A day whose
offset changes is recomputed with the day before it, whose end moved.

Logs: counts only, `[WELLNESS] ingest op=<first 4 of op_key> rows=.. ms=..`.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time
import zlib
from typing import Callable, Dict, List, Mapping, Optional, Set, Union

from pydantic import ValidationError

from Orchestrator.wellness import catalog, rollups, schema
from Orchestrator.wellness import store as store_mod
from Orchestrator.wellness.store import HC_MERGED, Store, add_days, day_range, local_day

logger = logging.getLogger(__name__)

MAX_COMPRESSED_BYTES = 2 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 8 * 1024 * 1024
NEXT_MIN_INTERVAL_S = 900
FUTURE_SLACK_MS = 26 * 3_600_000      # a phone clock ahead by a day is still accepted

# Plausibility (spec §3.3): outside these a value is stored flagged.
HR_RANGE = (20.0, 250.0)
SPO2_RANGE = (50.0, 100.0)
BP_SYS_RANGE = (40.0, 300.0)
BP_DIA_RANGE = (20.0, 200.0)
HRV_RANGE = (1.0, 500.0)
RESP_RANGE = (3.0, 60.0)
SUM_MAX_PER_HOUR = {"steps": 50_000.0, "distance": 50_000.0,
                    "active_calories": 5_000.0, "total_calories": 10_000.0}
SESSION_MAX_H = {"sleep": 24.0, "exercise": 24.0}
READING_RANGES = {"resting_hr": HR_RANGE, "hrv": HRV_RANGE, "spo2": SPO2_RANGE,
                  "respiratory_rate": RESP_RANGE}
GRAIN_HOURS = {"1min": 1 / 60.0, "5min": 5 / 60.0, "1h": 1.0}
GRAIN_MS = {"1min": 60_000, "5min": 300_000, "1h": 3_600_000}

# The consent category (catalog.CATEGORIES) of every row the phone can send.
BUCKET_CATEGORY = {"heart_rate": "heart", "steps": "activity", "distance": "activity",
                   "active_calories": "activity", "total_calories": "activity"}
READING_CATEGORY = {"resting_hr": "heart", "hrv": "heart", "spo2": "heart",
                    "respiratory_rate": "heart", "blood_pressure": "blood_pressure"}
SESSION_CATEGORY = {"sleep": "sleep", "exercise": "activity"}
# Heart rate a workout carries in its attrs (heart, not activity).
SESSION_HEART_ATTRS = ("avg_hr", "max_hr")
# A day can run this many hours past 24 (a fall-back day is 25; flying west
# across zones a little more); rows further out than that infer nothing.
MAX_DAY_STRETCH_H = 3


class IngestError(Exception):
    def __init__(self, status: int, code: str, detail: str = ""):
        self.status, self.code, self.detail = status, code, detail
        super().__init__(code)


@dataclasses.dataclass
class IngestResult:
    accepted: int = 0
    dropped: int = 0
    flagged: int = 0
    deleted: int = 0
    duplicate: bool = False
    dirty_days: List[str] = dataclasses.field(default_factory=list)
    pending_notices: List[dict] = dataclasses.field(default_factory=list)
    ms: int = 0

    def response(self) -> dict:
        body = {"accepted": self.accepted, "dropped": self.dropped,
                "next_min_interval_s": NEXT_MIN_INTERVAL_S,
                "pending_notices": self.pending_notices}
        if self.duplicate:
            body["duplicate"] = True
        return body


# ------------------------------------------------------------------ bytes

def decompress(raw: bytes, encoding: Optional[str]) -> bytes:
    """The body as JSON bytes: gunzip when Content-Encoding says gzip, capped
    at MAX_DECOMPRESSED_BYTES (413 past it, 422 when it is not gzip)."""
    enc = (encoding or "").strip().lower()
    if enc in ("", "identity"):
        if len(raw) > MAX_DECOMPRESSED_BYTES:
            raise IngestError(413, "too_large")
        return raw
    if enc != "gzip":
        raise IngestError(422, "invalid", "unsupported Content-Encoding")
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = bytearray()
    try:
        chunk = d.decompress(raw, MAX_DECOMPRESSED_BYTES + 1)
        out += chunk
        while d.unconsumed_tail and len(out) <= MAX_DECOMPRESSED_BYTES:
            out += d.decompress(d.unconsumed_tail, MAX_DECOMPRESSED_BYTES + 1 - len(out))
        if len(out) > MAX_DECOMPRESSED_BYTES:
            raise IngestError(413, "too_large")
        out += d.flush()
    except zlib.error:
        raise IngestError(422, "invalid", "the body is not valid gzip") from None
    if len(out) > MAX_DECOMPRESSED_BYTES:
        raise IngestError(413, "too_large")
    if not d.eof:
        raise IngestError(422, "invalid", "the gzip stream is truncated")
    return bytes(out)


def _no_constants(name):
    raise ValueError(f"{name} is not JSON")


def parse_json(data: bytes) -> dict:
    try:
        obj = json.loads(data.decode("utf-8"), parse_constant=_no_constants)
    except (UnicodeDecodeError, ValueError):
        raise IngestError(422, "invalid", "the body is not JSON") from None
    if not isinstance(obj, dict):
        raise IngestError(422, "invalid", "the body must be a JSON object")
    return obj


def validation_detail(exc: ValidationError) -> str:
    """A short, value-free description of what was malformed (field paths only)."""
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('type', 'invalid')}")
    return "; ".join(parts)


def parse_batch(obj: dict) -> schema.IngestBatch:
    try:
        return schema.IngestBatch.model_validate(obj)
    except ValidationError as e:
        raise IngestError(422, "invalid", validation_detail(e)) from None


# ------------------------------------------------------------------ checks

def _outside(v, lo_hi) -> bool:
    return v is None or not (lo_hi[0] <= v <= lo_hi[1])


def _bucket_flag(metric: str, grain: str, row: list) -> int:
    if metric == "heart_rate":
        avg, lo, hi = row[1], row[2], row[3]
        n = row[4]
        if _outside(avg, HR_RANGE) or (lo is not None and _outside(lo, HR_RANGE)) or \
                (hi is not None and _outside(hi, HR_RANGE)) or (n is not None and n < 0):
            return 1
        return 0
    v = row[1]
    cap = SUM_MAX_PER_HOUR[metric] * GRAIN_HOURS[grain]
    return 1 if v is None or v < 0 or v > cap else 0


def _reading_flag(metric: str, v: float, v2: Optional[float]) -> int:
    if metric == "blood_pressure":
        return 1 if _outside(v, BP_SYS_RANGE) or _outside(v2, BP_DIA_RANGE) or v2 >= v else 0
    return 1 if _outside(v, READING_RANGES[metric]) else 0


# ------------------------------------------------------------------ pipeline

Consent = Union[Mapping[str, str], Callable[[], Optional[Mapping[str, str]]], None]


def consented(consent: Consent) -> Optional[Set[str]]:
    """The categories a batch may store: those whose consent is ask or
    proactive. None (no consent given: the fixture loaders) keeps every
    category; a callable is asked now, and an answer of None keeps none."""
    if consent is None:
        return None
    answer = consent() if callable(consent) else consent
    return {c for c in catalog.CATEGORIES if (answer or {}).get(c) in catalog.CONSENTED}


def process_batch(st: Store, batch: schema.IngestBatch, *, enrollment_id: str, epoch: int,
                  now_ms: int, device_id: Optional[str] = None, model: Optional[str] = None,
                  earliest_available: Optional[str] = None,
                  still_enrolled: Optional[Callable[[], bool]] = None,
                  consent: Consent = None) -> IngestResult:
    """Store one validated batch and recompute what it touched.

    `still_enrolled` is asked under the operator's write lock, before
    anything is written: a batch whose signature was checked before an erase
    or a revocation must not land after it (410 revoked). Erase unlinks the
    store under that same lock, and a store that is gone raises StoreGone,
    which is answered the same way; nothing ever re-creates it.

    `consent` (the enrollment's category map, or a callable returning it,
    asked under the same lock): rows of a category that is not consented are
    dropped as "category_off". The route always passes it."""
    t0 = time.monotonic()
    if batch.enrollment_id != enrollment_id:
        raise IngestError(422, "invalid", "enrollment_id does not match the signature")
    if batch.epoch != epoch:
        raise IngestError(422, "invalid", "epoch does not match the enrollment")
    res = IngestResult()
    dropped: Dict[str, int] = {}

    def drop(reason: str, n: int = 1):
        dropped[reason] = dropped.get(reason, 0) + n
        res.dropped += n

    future = now_ms + FUTURE_SLACK_MS
    dirty: Set[str] = set()
    with store_mod.write_lock(st.op_key):
        if still_enrolled is not None and not still_enrolled():
            raise IngestError(410, "revoked")
        allowed = consented(consent)

        def off(category: Optional[str]) -> bool:
            return allowed is not None and category not in allowed
        try:
            with st.write() as conn:
                if st.has_batch(conn, batch.batch_id):
                    res.duplicate = True
                else:
                    st.record_epoch(conn, epoch, enrollment_id, device_id, model, now_ms)
                    offsets = st.offsets(conn)
                    ids = {}
                    for o in batch.origins:
                        ids[o.key] = st.ensure_origin(conn, o.model_dump(), now=now_ms)

                    def origin_id(key: str) -> Optional[int]:
                        if key == HC_MERGED:
                            if HC_MERGED not in ids:
                                ids[HC_MERGED] = st.ensure_origin(conn, {"package": HC_MERGED},
                                                                  now=now_ms)
                            return ids[HC_MERGED]
                        return ids.get(key)

                    def moved(day: str, before: int, after: int):
                        # The day's start and the day before's end moved.
                        if before != after:
                            dirty.update((add_days(day, -1), day))

                    def note_day(day: str, off: Optional[int]):
                        """A bucket group's offset: the one at `day`'s midnight."""
                        if off is None or offsets.by_day.get(day) == off:
                            return
                        before = offsets.for_day(day)
                        st.set_day_offset(conn, day, off)
                        offsets.set(day, off)
                        moved(day, before, off)

                    def hint_day(day: str, off: int):
                        if offsets.knows(day):
                            return
                        before = offsets.for_day(day)
                        st.hint_day_offset(conn, day, off)
                        offsets.set(day, off)
                        moved(day, before, off)

                    def note_record(t: int, off: Optional[int]):
                        """A session's or reading's offset: the one in effect at t."""
                        if off is None:
                            return
                        day = local_day(t, off)
                        if not offsets.knows(day):
                            hint_day(day, off)
                        elif offsets.by_day[day] != off:
                            hint_day(add_days(day, 1), off)

                    def note_long_day(day: str, grain: str, rows: List[list]):
                        """Rows past the day's end (the next day's offset not
                        known yet): the zone fell back that day."""
                        nxt = add_days(day, 1)
                        if not rows or offsets.knows(nxt):
                            return
                        over = max(r[0] for r in rows) + GRAIN_MS[grain] - offsets.bounds(day)[1]
                        hours = -(-over // 3_600_000)
                        if 0 < hours <= MAX_DAY_STRETCH_H:
                            hint_day(nxt, offsets.for_day(nxt) - hours * 3600)

                    # ---- buckets
                    for g in batch.buckets:
                        kind = catalog.BUCKET_METRICS.get(g.metric)
                        if kind is None:
                            drop("unknown_metric", len(g.rows))
                            continue
                        if off(BUCKET_CATEGORY.get(g.metric)):
                            drop("category_off", len(g.rows))
                            continue
                        if (kind == "hr") != (g.grain in ("1min", "5min")):
                            drop("bad_grain", len(g.rows))
                            continue
                        oid = origin_id(g.origin)
                        if oid is None:
                            drop("unknown_origin", len(g.rows))
                            continue
                        want = 5 if kind == "hr" else 2
                        rows, flags = [], []
                        for row in g.rows:
                            if len(row) != want:
                                drop("bad_row")
                                continue
                            if row[0] > future:
                                drop("future")
                                continue
                            rows.append([int(row[0])] + list(row[1:]))
                            flags.append(_bucket_flag(g.metric, g.grain, row))
                        if g.replace_day:
                            note_day(g.replace_day, g.offset_s)
                            note_long_day(g.replace_day, g.grain, rows)
                            st.replace_bucket_day(
                                conn, g.metric, g.grain, oid, epoch, g.replace_day,
                                offsets.for_day(g.replace_day), rows, flags,
                                next_offset_s=offsets.for_day(add_days(g.replace_day, 1)))
                            dirty.add(g.replace_day)
                        else:
                            st.upsert_buckets(conn, g.metric, g.grain, oid, epoch, rows, flags)
                        days = {offsets.day_of(r[0]) for r in rows}
                        if g.metric == "heart_rate":
                            # A day's evening heart rate belongs to the night that
                            # ends the next morning (rollups read 12 h back).
                            days |= {add_days(d, 1) for d in days | (
                                {g.replace_day} if g.replace_day else set())}
                        dirty |= days
                        res.accepted += len(rows)
                        res.flagged += sum(flags)

                    # ---- sessions
                    for s in batch.sessions:
                        if off(SESSION_CATEGORY.get(s.kind)):
                            drop("category_off")
                            continue
                        oid = origin_id(s.origin)
                        if oid is None:
                            drop("unknown_origin")
                            continue
                        if s.end > future or s.start > future:
                            drop("future")
                            continue
                        flag = 0
                        hours = (s.end - s.start) / 3_600_000
                        if s.end <= s.start or hours > SESSION_MAX_H[s.kind]:
                            flag = 1
                        stages = []
                        for a, b, code in s.stages or ():
                            if b <= a or not 0 <= code <= 7 or b < s.start - 3_600_000 or \
                                    a > s.end + 3_600_000:
                                drop("bad_stage")
                                continue
                            stages.append((a, b, code))
                        attrs = {}
                        if s.kind == "exercise":
                            code = s.exercise_type if s.exercise_type is not None else s.subtype
                            attrs["type"] = catalog.exercise_type_name(code)
                            if s.attrs is not None:
                                attrs.update({k: v for k, v in s.attrs.model_dump().items()
                                              if v is not None and not (
                                                  k in SESSION_HEART_ATTRS and off("heart"))})
                        if flag:
                            attrs["flags"] = 1
                        subtype = s.subtype if s.subtype is not None else (
                            s.exercise_type if isinstance(s.exercise_type, int) else None)
                        if not flag:
                            note_record(s.start, s.offset_s)
                        end_day = offsets.day_of(s.end)
                        st.upsert_session(conn, epoch, {
                            "kind": s.kind, "hc_id": s.hc_id, "origin_id": oid, "start": s.start,
                            "end": s.end, "offset_s": s.offset_s, "subtype": subtype,
                            "attrs": attrs or None, "stages": stages,
                            "last_modified": s.last_modified})
                        if s.end > s.start and not flag:
                            dirty.update(day_range(offsets.day_of(s.start), end_day))
                        else:
                            # A flagged session feeds no rollup: only its end day
                            # is touched, however far back a bogus start reaches.
                            dirty.add(end_day)
                        res.accepted += 1
                        res.flagged += flag

                    # ---- readings
                    for r in batch.readings:
                        if r.metric not in catalog.READING_METRICS:
                            drop("unknown_metric")
                            continue
                        if off(READING_CATEGORY.get(r.metric)):
                            drop("category_off")
                            continue
                        oid = origin_id(r.origin)
                        if oid is None:
                            drop("unknown_origin")
                            continue
                        if r.t > future:
                            drop("future")
                            continue
                        flag = _reading_flag(r.metric, r.v, r.v2)
                        note_record(r.t, r.offset_s)
                        day = offsets.day_of(r.t)
                        attrs = {k: v for k, v in (r.attrs.model_dump() if r.attrs else {}).items()
                                 if v is not None}
                        st.upsert_reading(conn, epoch, {
                            "hc_id": r.hc_id, "metric": r.metric, "t": r.t, "offset_s": r.offset_s,
                            "origin_id": oid, "v": r.v, "v2": r.v2, "attrs": attrs or None,
                            "flags": flag, "last_modified": r.last_modified})
                        dirty.add(day)
                        res.accepted += 1
                        res.flagged += flag

                    # ---- deletions
                    if batch.deleted_ids:
                        dirty |= st.delete_hc_ids(conn, batch.deleted_ids)
                        res.deleted = len(batch.deleted_ids)

                    log_dropped = dict(dropped)
                    if res.flagged:
                        log_dropped["flagged"] = res.flagged
                    st.log_batch(conn, batch.batch_id, now_ms, epoch, res.accepted, res.deleted,
                                 "ok", log_dropped or None)
            if not res.duplicate and dirty:
                rollups.recompute(st, dirty, now_ms=now_ms, earliest_available=earliest_available)
            res.dirty_days = sorted(dirty)
            # Notices are read under the lock too, so an erase never finds
            # this batch still reading the store it unlinks.
            notices = st.pending_notices(now=now_ms)
            if notices:
                st.mark_notices_delivered([n["id"] for n in notices], now=now_ms)
            res.pending_notices = notices
        except store_mod.StoreGone:
            raise IngestError(410, "revoked") from None
    res.ms = int((time.monotonic() - t0) * 1000)
    logger.info("[WELLNESS] ingest op=%s rows=%d flagged=%d dropped=%d off=%d deleted=%d "
                "days=%d%s ms=%d", st.op_key[:4], res.accepted, res.flagged, res.dropped,
                dropped.get("category_off", 0), res.deleted, len(res.dirty_days),
                " duplicate" if res.duplicate else "", res.ms)
    return res
