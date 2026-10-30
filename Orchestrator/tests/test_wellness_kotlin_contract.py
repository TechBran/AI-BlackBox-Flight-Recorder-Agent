"""The Kotlin-to-Python contract (M7.2): the phone and the box agree on the
wire, checked against the phone's FROZEN goldens.

The Android unit tests freeze what the app emits (IngestPayloadBuilderGoldenTest
and CapabilityProbeTest: the exact wire bytes plus one newline, under
app/src/test/resources/wellness/), and its OriginSignerGoldenTest reproduces the
box's signer vectors. This file replays the same bytes through the box:

  1. every ingest golden through the real POST /wellness/ingest, gzip and
     signed the way IngestClient sends it, on a temp box where the fixture
     operator's phone enrolled through the real handshake: the counts, consent
     at the door (blood pressure is off by default, then on), the stored rows
     round-trip, re-sends are idempotent, and the local days follow offset_s
     (a fall-back day in the same wire form stands in for the DST golden the
     Android side has not frozen);
  2. the signer vectors (the box's file, which the Kotlin test reads, and any
     copy the Android side keeps): verified by verify_request, reproduced by
     the box and by the simulator's independent signer, with the query string,
     a lower-case method, a 16-byte nonce and the clock-skew edges;
  3. the privacy policy the phone shows is the one the box serves, and its
     SHA-256 is the literal the Android side pins (once that file exists);
  4. the CapabilityProbe report is taken whole by POST /wellness/capabilities
     and is what the signed status and wellness_sources reflect;
  5. the phone's catalog mirror (res/raw/wellness_catalog.json) names the box's
     categories, defaults, metrics and Health Connect permissions.

Hermetic: a temp box (wellness_support.make_box) with a FakePhone, no network.
The box's data clock is pinned at NOW_MS (the goldens are dated November 2026,
and the box drops rows more than a day ahead of its clock); signatures use the
real clock on both sides, as the phone and the box do. A golden the box
refuses is a real contract break: its test is marked xfail(strict=True) with
the field and the side the spec supports, never skipped.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from Orchestrator.routes import wellness_routes
from Orchestrator.tests.wellness_support import OPERATOR, close_box, load_sim, make_box
from Orchestrator.wellness import (api, catalog, coverage, enroll, ingest, origin_proof, schema,
                                   store)

REPO = Path(__file__).resolve().parents[2]
ANDROID = (REPO / "AI_BlackBox_Portal_Android_MVP (2)" / "AI_BlackBox_Portal_Android_MVP"
           / "AI_BlackBox_Portal" / "app" / "src")
GOLDENS = ANDROID / "test" / "resources" / "wellness"
KOTLIN_TESTS = ANDROID / "test" / "java" / "com" / "aiblackbox" / "portal" / "data" / "wellness"
PHONE_POLICY = ANDROID / "main" / "res" / "raw" / "health_privacy.md"
PHONE_CATALOG = ANDROID / "main" / "res" / "raw" / "wellness_catalog.json"
POLICY_SHA = GOLDENS / "policy_sha256.txt"
BOX_POLICY = REPO / "Orchestrator" / "wellness" / "policy" / "health_privacy.md"
BOX_VECTORS = (REPO / "Orchestrator" / "tests" / "fixtures" / "wellness"
               / "origin_proof_vectors.json")

# The goldens IngestPayloadBuilderGoldenTest freezes (a new one is picked up by the glob).
FROZEN = ("ingest_sleep_heart.json", "ingest_activity.json", "ingest_blood_pressure.json",
          "ingest_deletions.json")
INGEST = sorted(p.name for p in GOLDENS.glob("ingest_*.json"))
NOW_MS = int(datetime(2026, 11, 5, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
QUERY = "?operator=" + OPERATOR
SIM = load_sim()


# ------------------------------------------------------------------ the goldens

def _golden(name: str):
    """(the exact wire bytes, the batch): a golden is the bytes plus one newline."""
    raw = (GOLDENS / name).read_bytes()
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n") and b"\r" not in raw, name
    wire = raw[:-1]
    return wire, json.loads(wire)


def _compact(batch: dict) -> bytes:
    """A batch in the phone's wire form (compact, field order kept)."""
    return json.dumps(batch, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


CAPS_WIRE, CAPS = _golden("capability_report.json")
GOLDEN_ENR = json.loads((GOLDENS / FROZEN[0]).read_bytes())["enrollment_id"]


def _iso_ms(text: str) -> int:
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text), text
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def _phone_day(ts_ms: int, offset_s: int) -> str:
    """The local day of an instant at the offset the phone stated."""
    zone = timezone(timedelta(seconds=offset_s))
    return datetime.fromtimestamp(ts_ms / 1000, tz=zone).date().isoformat()


def _phone_bounds(day: str, offset_s: int, next_offset_s: int):
    """[start, end) of a local day as the phone means it: its midnight at
    offset_s to the next midnight at the next day's offset (23 or 25 hours
    across a DST change)."""
    def midnight(d: str, off: int) -> int:
        local = datetime.fromisoformat(d).replace(tzinfo=timezone(timedelta(seconds=off)))
        return int(local.timestamp() * 1000)
    nxt = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
    return midnight(day, offset_s), midnight(nxt, next_offset_s)


def _category(kind: str, name: str) -> str:
    return {"bucket": ingest.BUCKET_CATEGORY, "session": ingest.SESSION_CATEGORY,
            "reading": ingest.READING_CATEGORY}[kind][name]


def _counts_for(batch: dict, on: set):
    """(rows kept, rows dropped as category_off) for a batch under a consent."""
    rows = [(_category("bucket", g["metric"]), len(g["rows"])) for g in batch["buckets"]]
    rows += [(_category("session", s["kind"]), 1) for s in batch["sessions"]]
    rows += [(_category("reading", r["metric"]), 1) for r in batch["readings"]]
    return sum(n for c, n in rows if c in on), sum(n for c, n in rows if c not in on)


def _origin(batch: dict, key: str):
    if key == store.HC_MERGED:
        return (store.HC_MERGED, None, None)
    o = next(o for o in batch["origins"] if o["key"] == key)
    return (o["package"], o.get("device_type"), o.get("model"))


def _expected(batch: dict, on: set) -> dict:
    """What the store must hold after `batch` alone, with the categories `on`
    consented: every row of the golden, keyed the way _stored reads them back.
    Attrs are mirrored whole, so a field the box silently drops shows here."""
    epoch = batch["epoch"]
    buckets, sessions, readings, origins = {}, {}, {}, set()
    for g in batch["buckets"]:
        if _category("bucket", g["metric"]) not in on:
            continue
        o = _origin(batch, g["origin"])
        for row in g["rows"]:
            origins.add(o)
            vals = (row[1], row[2], row[3], None, row[4]) if len(row) == 5 else \
                (None, None, None, row[1], None)
            buckets[(g["metric"], g["grain"], o, epoch, row[0])] = vals + (0,)
    for s in batch["sessions"]:
        if _category("session", s["kind"]) not in on:
            continue
        o = _origin(batch, s["origin"])
        origins.add(o)
        attrs = None
        if s["kind"] == "exercise":
            code = s.get("exercise_type", s.get("subtype"))
            attrs = {"type": catalog.exercise_type_name(code)}
            attrs.update({k: v for k, v in (s.get("attrs") or {}).items() if v is not None and
                          not (k in ingest.SESSION_HEART_ATTRS and "heart" not in on)})
        sessions[(epoch, s["hc_id"])] = {
            "kind": s["kind"], "origin": o, "start": s["start"], "end": s["end"],
            "offset_s": s.get("offset_s"), "subtype": s.get("subtype"), "attrs": attrs,
            "last_modified": s.get("last_modified"),
            "stages": [tuple(x) for x in s.get("stages") or ()]}
    for r in batch["readings"]:
        if _category("reading", r["metric"]) not in on:
            continue
        o = _origin(batch, r["origin"])
        origins.add(o)
        attrs = {k: v for k, v in (r.get("attrs") or {}).items() if v is not None}
        readings[(epoch, r["hc_id"])] = {
            "metric": r["metric"], "origin": o, "t": r["t"], "offset_s": r.get("offset_s"),
            "v": r["v"], "v2": r.get("v2"), "attrs": attrs or None, "flags": 0,
            "last_modified": r.get("last_modified")}
    return {"buckets": buckets, "sessions": sessions, "readings": readings, "origins": origins}


# ------------------------------------------------------------------ the box

class _GoldenIds:
    """`secrets` as enroll sees it: the bind mints the enrollment id the
    goldens were frozen with (enroll draws it as token_hex(4)); every other
    draw stays random."""

    def __init__(self, enr: str):
        self._hex = enr[len("en_"):]

    def token_hex(self, nbytes=None):
        return self._hex if nbytes == 4 else secrets.token_hex(nbytes)

    def __getattr__(self, name):
        return getattr(secrets, name)


@pytest.fixture
def box(isolated_stores, toolvault_embed_sync, monkeypatch):
    """A temp box where the fixture operator's phone enrolled through the real
    handshake (with the Kotlin capability report), at epoch 1 under the
    goldens' enrollment id, the data clock at NOW_MS and a fresh rate limit."""
    monkeypatch.setattr(enroll, "secrets", _GoldenIds(GOLDEN_ENR))
    monkeypatch.setattr(enroll, "_now_ms",
                        lambda now_ms=None: NOW_MS if now_ms is None else int(now_ms))
    monkeypatch.setattr(wellness_routes, "_now_ms", lambda: NOW_MS)
    monkeypatch.setattr(wellness_routes, "RATE", wellness_routes._RateLimit())
    b = make_box(isolated_stores, monkeypatch)
    r = b.enroll(capability_report=CAPS)
    assert r.status_code == 200, r.text
    assert (r.json()["enrollment_id"], r.json()["epoch"]) == (GOLDEN_ENR, 1)
    yield b
    close_box(b)


def _send(box, method: str, target: str, body: bytes = b"", *, gzip_body: bool = False,
          signed: bool = True):
    """One request the way IngestClient.send makes it: the signature over the
    exact bytes sent (the gzip bytes for a batch) and the request target with
    its query, by the simulator's signer (written apart from origin_proof.py);
    the client header and the JSON type; through tailscale serve."""
    headers = {"X-BlackBox-Client": "native-android/1.0", "X-Forwarded-For": box.ip}
    if signed:
        headers[origin_proof.HEADER] = SIM.origin_header(box.phone.key, box.phone.enrollment_id,
                                                         method, target, body)
    if gzip_body:
        headers["Content-Encoding"] = "gzip"
    if method != "GET":
        headers["Content-Type"] = "application/json; charset=utf-8"
    return box.client.request(method, target, content=body, headers=headers)


def _push(box, wire: bytes):
    return _send(box, "POST", "/wellness/ingest", gzip.compress(wire, mtime=0), gzip_body=True)


def _rebind(box, epoch: int) -> None:
    """Move the enrollment to `epoch` by re-binding the same phone (the
    enroll handshake with replace, from the same address). The goldens keep
    one enrollment id across their two epochs; a real re-bind mints a new
    one, which only the signature and the batch header carry, so the id the
    bind mints stays the goldens'."""
    while enroll.get_enrollment(OPERATOR)["epoch"] < epoch:
        r = box.enroll(replace=True, categories=None, capability_report=CAPS)
        assert r.status_code == 200, r.text
        assert r.json()["enrollment_id"] == GOLDEN_ENR
    assert enroll.get_enrollment(OPERATOR)["epoch"] == epoch == box.phone.epoch


def _consent(box, **categories) -> None:
    """Change consent from the enrolled phone (signed PUT /wellness/settings)."""
    body = json.dumps({"operator": OPERATOR, "categories": categories}).encode()
    r = _send(box, "PUT", "/wellness/settings", body)
    assert r.status_code == 200, r.text
    assert {c: r.json()["categories"][c] for c in categories} == categories


def _on() -> set:
    cats = enroll.get_enrollment(OPERATOR)["categories"]
    return {c for c, v in cats.items() if v in catalog.CONSENTED}


def _store() -> store.Store:
    return enroll.store_for(enroll.get_enrollment(OPERATOR), create=False)


def _log(st: store.Store, batch_id: str) -> dict:
    with st.read() as conn:
        row = conn.execute("SELECT n_rows, n_deleted, status, dropped FROM ingest_log "
                           "WHERE batch_id=?", (batch_id,)).fetchone()
    assert row is not None, batch_id
    return {"n_rows": row[0], "n_deleted": row[1], "status": row[2],
            "dropped": json.loads(row[3]) if row[3] else None}


def _counts(st: store.Store) -> dict:
    with st.read() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("bucket", "session", "stage", "reading", "origin", "ingest_log")}


def _stored(st: store.Store) -> dict:
    """The store's data rows, keyed the way the wire names them."""
    with st.read() as conn:
        origin = {r["origin_id"]: (r["package"], r["device_type"], r["model"])
                  for r in conn.execute("SELECT * FROM origin")}
        buckets = {(r["metric"], r["grain"], origin[r["origin_id"]], r["epoch"], r["ts"]):
                   (r["avg"], r["min"], r["max"], r["sum"], r["n"], r["flags"])
                   for r in conn.execute("SELECT * FROM bucket")}
        stages = {}
        for r in conn.execute("SELECT * FROM stage ORDER BY start_ts"):
            stages.setdefault((r["epoch"], r["hc_id"]), []).append(
                (r["start_ts"], r["end_ts"], r["code"]))
        sessions = {(r["epoch"], r["hc_id"]): {
            "kind": r["kind"], "origin": origin[r["origin_id"]], "start": r["start_ts"],
            "end": r["end_ts"], "offset_s": r["tz_offset_s"], "subtype": r["subtype"],
            "attrs": json.loads(r["attrs"]) if r["attrs"] else None,
            "last_modified": r["last_modified"],
            "stages": stages.get((r["epoch"], r["hc_id"]), [])}
            for r in conn.execute("SELECT * FROM session")}
        readings = {(r["epoch"], r["hc_id"]): {
            "metric": r["metric"], "origin": origin[r["origin_id"]], "t": r["ts"],
            "offset_s": r["tz_offset_s"], "v": r["v1"], "v2": r["v2"],
            "attrs": json.loads(r["attrs"]) if r["attrs"] else None, "flags": r["flags"],
            "last_modified": r["last_modified"]}
            for r in conn.execute("SELECT * FROM reading")}
    return {"buckets": buckets, "sessions": sessions, "readings": readings,
            "origins": set(origin.values())}


def _next_day(day: str) -> str:
    return (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()


def _assert_local_days(st: store.Store, batch: dict, on: set) -> None:
    """The box's local days are the phone's. A replace_day group's offset is
    that day's, and the day starts at its midnight at that offset. It ends at
    the next midnight at the next day's offset when the batch states one (a
    DST change across two stated days is checked exactly); otherwise at 24
    hours unless the group's rows run past that (a fall-back day the box
    stretches by the hours they show). Every row lies inside the box's day,
    and every session and reading is filed under its local day at its own
    offset. A backfill's window is the bounds of the days it replaces."""
    offsets = st.offsets()
    stated = {g["replace_day"]: g["offset_s"] for g in batch["buckets"]
              if g.get("replace_day") and _category("bucket", g["metric"]) in on}
    days = {}
    for day, off in stated.items():
        start, end = _phone_bounds(day, off, stated.get(_next_day(day), off))
        rows = [row[0] for g in batch["buckets"] if g.get("replace_day") == day
                and _category("bucket", g["metric"]) in on for row in g["rows"]]
        box_start, box_end = offsets.bounds(day)
        assert offsets.by_day[day] == off and box_start == start, day
        if _next_day(day) in stated or all(t < end for t in rows):
            assert box_end == end, day
        else:
            assert box_end > end, day           # stretched by the hours the rows show
        assert all(box_start <= t < box_end for t in rows), day
        days[day] = (start, box_end)
    for s in batch["sessions"]:
        if _category("session", s["kind"]) in on:
            assert offsets.day_of(s["start"]) == _phone_day(s["start"], s["offset_s"])
            assert offsets.day_of(s["end"]) == _phone_day(s["end"], s["offset_s"])
    for r in batch["readings"]:
        if _category("reading", r["metric"]) in on:
            assert offsets.day_of(r["t"]) == _phone_day(r["t"], r["offset_s"]), r["hc_id"]
    if batch["window"]["type"] == "backfill" and days:
        first, last = min(days), max(days)
        assert (_iso_ms(batch["window"]["from"]), _iso_ms(batch["window"]["to"])) == \
            (days[first][0], days[last][1])


def _no_merged_source() -> None:
    """The reserved hc_merged origin is never listed as a source."""
    summary = api.summary(OPERATOR, "last_7_days", now_ms=NOW_MS)
    assert summary["ok"] is True, summary
    assert store.HC_MERGED not in json.dumps(summary["sources"])
    sources = api.sources(OPERATOR, now_ms=NOW_MS)
    assert sources["ok"] is True and store.HC_MERGED not in json.dumps(sources)


# ------------------------------------------------------------------ 1. ingest replay

def test_the_goldens_are_all_there_and_well_formed():
    """Every frozen golden is present (a moved directory must fail here, not
    parametrize to nothing), parses as bbx.wellness.ingest/1 (a 422 would be a
    quarantine on the phone), names only metrics, grains and kinds the box
    knows, and only declared origins (or hc_merged)."""
    assert set(FROZEN) <= set(INGEST)
    enrollment_ids = set()
    for name in INGEST:
        _wire, batch = _golden(name)
        parsed = ingest.parse_batch(batch)
        assert parsed.schema_ == schema.SCHEMA_INGEST
        enrollment_ids.add(batch["enrollment_id"])
        assert batch["window"]["type"] in ("changes", "backfill", "reconcile"), name
        assert _iso_ms(batch["window"]["from"]) <= _iso_ms(batch["window"]["to"]), name
        keys = {o["key"] for o in batch["origins"]} | {store.HC_MERGED}
        for g in batch["buckets"]:
            kind = catalog.BUCKET_METRICS[g["metric"]]
            assert (kind == "hr") == (g["grain"] in ("1min", "5min")), (name, g["metric"])
            assert g["origin"] in keys and all(len(r) == (5 if kind == "hr" else 2)
                                               for r in g["rows"])
        for s in batch["sessions"]:
            assert s["kind"] in catalog.SESSION_KINDS and s["origin"] in keys
        for r in batch["readings"]:
            assert r["metric"] in catalog.READING_METRICS and r["origin"] in keys
    assert enrollment_ids == {GOLDEN_ENR}


@pytest.mark.parametrize("bp", ["off", "ask"], ids=["bp-off", "bp-on"])
@pytest.mark.parametrize("name", INGEST)
def test_a_golden_replays_through_the_real_ingest_route(box, name, bp):
    """The exact bytes, gzip and signed: 200, every row of a consented
    category accepted and stored as sent (blood pressure off by default is
    dropped as category_off and counted), the ingest log says the same, the
    local days follow offset_s, hc_merged is never a source, and a re-send
    (the same batch, or the same rows under a new batch id) changes nothing."""
    wire, batch = _golden(name)
    _rebind(box, batch["epoch"])
    if bp != "off":
        _consent(box, blood_pressure=bp)
    on = _on()
    assert ("blood_pressure" in on) == (bp != "off")
    kept, off = _counts_for(batch, on)

    r = _push(box, wire)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"accepted": kept, "dropped": off,
                    "next_min_interval_s": ingest.NEXT_MIN_INTERVAL_S, "pending_notices": []}
    st = _store()
    assert _log(st, batch["batch_id"]) == {
        "n_rows": kept, "n_deleted": len(batch["deleted_ids"]), "status": "ok",
        "dropped": {"category_off": off} if off else None}
    stored, want = _stored(st), _expected(batch, on)
    assert stored["buckets"] == want["buckets"]
    assert stored["sessions"] == want["sessions"]
    assert stored["readings"] == want["readings"]
    # Exactly the origins a kept row names: a declared origin whose rows were
    # all dropped, or whose group was empty, is no source (ingest.py).
    assert stored["origins"] == want["origins"]
    _assert_local_days(st, batch, on)
    _no_merged_source()

    before = _counts(st)
    again = _push(box, wire)
    assert again.status_code == 200
    assert again.json() == dict(body, accepted=0, dropped=0, duplicate=True)
    assert _counts(st) == before and _stored(st) == stored
    renamed = _push(box, _compact(dict(batch, batch_id=batch["batch_id"] + "-r")))
    assert renamed.status_code == 200 and renamed.json()["accepted"] == kept
    after = _counts(st)
    assert after == dict(before, ingest_log=before["ingest_log"] + 1)
    assert _stored(st) == stored


def test_the_deletions_golden_alone_stores_no_origin(box):
    """The deletions golden declares the watch and sends two heart-rate days
    with no rows (and two deleted ids). Replayed alone at epoch 2, on a store
    that never had a row from that watch, it keeps nothing, so it lists no
    source: no origin row, nothing in a summary's sources."""
    wire, batch = _golden("ingest_deletions.json")
    assert batch["origins"] and all(not g["rows"] for g in batch["buckets"])
    _rebind(box, 2)
    r = _push(box, wire)
    assert r.status_code == 200, r.text
    assert (r.json()["accepted"], r.json()["dropped"]) == (0, 0)
    st = _store()
    assert _stored(st)["origins"] == set()
    with st.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM origin").fetchone()[0] == 0
    summary = api.summary(OPERATOR, "last_7_days", now_ms=NOW_MS)
    assert summary["ok"] is True and summary["sources"] == []


def test_blood_pressure_waits_for_its_consent(box):
    """Blood pressure is off by default: the golden's readings are dropped and
    counted, and the app that wrote them leaves no trace. Turned on from the
    phone, the same batch is a duplicate (the box received it once and keeps
    nothing it dropped); the phone's next read of the same records is stored,
    systolic/diastolic and attrs exactly as sent."""
    wire, batch = _golden("ingest_blood_pressure.json")
    _rebind(box, batch["epoch"])
    assert enroll.get_enrollment(OPERATOR)["categories"]["blood_pressure"] == "off"
    n = len(batch["readings"])
    assert n and all(r["metric"] == "blood_pressure" for r in batch["readings"])
    r = _push(box, wire)
    assert r.status_code == 200 and (r.json()["accepted"], r.json()["dropped"]) == (0, n)
    st = _store()
    assert _log(st, batch["batch_id"])["dropped"] == {"category_off": n}
    cuff = _origin(batch, batch["readings"][0]["origin"])
    assert _stored(st)["readings"] == {} and cuff not in _stored(st)["origins"]

    _consent(box, blood_pressure="ask")
    again = _push(box, wire)
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert _stored(st)["readings"] == {}
    nxt = dict(batch, batch_id=batch["batch_id"] + "-next")
    r = _push(box, _compact(nxt))
    assert r.status_code == 200 and (r.json()["accepted"], r.json()["dropped"]) == (n, 0)
    assert _stored(st)["readings"] == _expected(nxt, _on())["readings"]
    assert cuff in _stored(st)["origins"]
    sample = next(iter(_stored(st)["readings"].values()))
    assert set(sample["attrs"]) == {"body_position", "measurement_location", "recording_method"}


def test_the_goldens_as_one_phone_s_life(box):
    """The goldens in the order a phone sends them: sleep and heart, then the
    day's activity (backfill) at epoch 1; a re-bind; blood pressure (off) and
    the deletions at epoch 2. Stage codes and merged sums mean the same on
    both sides; a deleted Health Connect id leaves every epoch (an unknown one
    is harmless) and its night is recomputed away; a notice rides back in
    the shape the phone's outbox reads."""
    sleep_wire, sleep = _golden("ingest_sleep_heart.json")
    act_wire, act = _golden("ingest_activity.json")
    bp_wire, bp = _golden("ingest_blood_pressure.json")
    del_wire, dels = _golden("ingest_deletions.json")
    assert (sleep["epoch"], act["epoch"], bp["epoch"], dels["epoch"]) == (1, 1, 2, 2)
    assert (sleep["window"]["type"], act["window"]["type"]) == ("changes", "backfill")

    for wire in (sleep_wire, act_wire):
        assert _push(box, wire).status_code == 200
    st = _store()
    on = _on()
    for batch in (sleep, act):
        _assert_local_days(st, batch, on)
    night = "2026-11-03"
    assert st.offsets().by_day == {"2026-11-02": -18000, night: -18000}

    # Health Connect stage codes (4 light, 5 deep, 1 awake, 6 REM): asleep is
    # every stage but the awake one.
    stages = sleep["sessions"][0]["stages"]
    asleep_min = sum(b - a for a, b, code in stages if code in catalog.ASLEEP_STAGES) / 60_000
    assert asleep_min == 430
    sleep_row = st.rollups("night", night, night, metric="sleep_duration")
    assert [r["value"] for r in sleep_row] == [asleep_min]
    # Steps arrive as Health Connect's merged hourly sums; the day is their total.
    steps = sum(row[1] for g in act["buckets"] if g["metric"] == "steps" for row in g["rows"])
    assert [r["value"] for r in st.rollups("day", night, night, metric="steps")] == [steps]
    run = _stored(st)["sessions"][(1, "run-20261103")]
    assert run["attrs"]["type"] == "run" and run["subtype"] == 56   # HC EXERCISE_TYPE_RUNNING
    _no_merged_source()

    _rebind(box, 2)
    nid = st.add_notice("briefing", "Your morning briefing", "Open BlackBox", now=NOW_MS)
    r = _push(box, bp_wire)
    assert r.status_code == 200 and r.json()["dropped"] == len(bp["readings"])
    notices = r.json()["pending_notices"]
    # WellnessGraph.postNotices reads id, title and body of each notice
    # (WellnessOutbox.parseNotices hands the objects on whole, and the
    # phone's Json ignores keys it does not know).
    assert [n["id"] for n in notices] == [nid]
    assert {"id", "title", "body"} <= set(notices[0])
    assert isinstance(r.json()["next_min_interval_s"], int)

    heart_before = {k: v for k, v in _stored(st)["buckets"].items() if k[0] == "heart_rate"}
    r = _push(box, del_wire)
    assert r.status_code == 200 and (r.json()["accepted"], r.json()["dropped"]) == (0, 0)
    assert _log(st, dels["batch_id"])["n_deleted"] == len(dels["deleted_ids"]) == 2
    stored = _stored(st)
    assert set(stored["sessions"]) == {(1, "run-20261103")}
    with st.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM stage WHERE hc_id='sleep-20261103'"
                            ).fetchone()[0] == 0
    assert all(r["value"] is None for r in st.rollups("night", night, night,
                                                      metric="sleep_duration"))
    # The empty heart-rate days replace epoch 2's rows only; epoch 1's are the
    # day's until a later epoch sends some (the effective-epoch rule, §3.4).
    assert {k: v for k, v in stored["buckets"].items() if k[0] == "heart_rate"} == heart_before
    assert not [k for k in stored["buckets"] if k[3] == 2]


def _utc_ms(*ymdh: int) -> int:
    return int(datetime(*ymdh, tzinfo=timezone.utc).timestamp() * 1000)


def _utc_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("next_day_stated", [True, False], ids=["both-days", "one-day"])
def test_a_fall_back_day_in_the_phone_s_wire_form(box, next_day_stated):
    """The Android side has frozen no DST golden, so this is the activity
    golden retimed to New York's 2026 fall-back (1 November, 25 hours: its
    midnight at -04:00, the next at -05:00) in the same wire form: each
    replace_day group carries the offset at its own midnight, and one steps
    row sits in the day's 25th hour. With the next day stated too, the box's
    day is exactly the phone's; with the long day alone, its rows stretch it
    to the same 25 hours."""
    _wire, act = _golden("ingest_activity.json")
    fall = "2026-11-01"
    groups = [{"metric": "steps", "grain": "1h", "origin": store.HC_MERGED, "replace_day": fall,
               "offset_s": -14400, "rows": [[_utc_ms(2026, 11, 1, 12), 3000],
                                            [_utc_ms(2026, 11, 2, 4), 4100]]}]
    end = _utc_ms(2026, 11, 2, 5)
    if next_day_stated:
        groups.append({"metric": "steps", "grain": "1h", "origin": store.HC_MERGED,
                       "replace_day": "2026-11-02", "offset_s": -18000,
                       "rows": [[_utc_ms(2026, 11, 2, 13), 3000]]})
        end = _utc_ms(2026, 11, 3, 5)
    batch = dict(act, batch_id="dst-fall-back", origins=[], sessions=[], buckets=groups,
                 window={"type": "backfill", "from": "2026-11-01T04:00:00Z", "to": _utc_iso(end)})
    r = _push(box, _compact(batch))
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == sum(len(g["rows"]) for g in groups)
    st = _store()
    _assert_local_days(st, batch, _on())
    start, stop = st.offsets().bounds(fall)
    assert stop - start == 25 * 3_600_000


# ------------------------------------------------------------------ 2. signer vectors

def _vector_files():
    """The box's vectors (the file the Kotlin test reads from the repo root)
    and any copy the Android side keeps among its goldens."""
    extra = set(GOLDENS.glob("*origin_proof*.json")) | set(GOLDENS.glob("*vectors*.json"))
    return [BOX_VECTORS] + sorted(extra)


def _vector_body(v: dict) -> bytes:
    """A vector's body as the Kotlin test reads it: body_b64, else body_hex,
    else body_utf8."""
    if v.get("body_b64") is not None:
        return base64.b64decode(v["body_b64"])
    if v.get("body_hex") is not None:
        return bytes.fromhex(v["body_hex"])
    return (v.get("body_utf8") or "").encode("utf-8")


def _verify(v: dict, *, method=None, path=None, now=None, header=None):
    key = origin_proof.decode_key(v["k_b64url"])
    header = header or origin_proof.format_header(v["enr"], v["ts"], v["nonce"], v["expected_sig"])
    return origin_proof.verify_request(header, method or v["method"], path or v["path"],
                                       _vector_body(v), lookup_key=lambda enr: key,
                                       now=v["ts"] if now is None else now,
                                       nonces=origin_proof.NonceCache())


def test_the_phone_signs_against_the_box_vectors():
    """OriginSignerGoldenTest reads THIS file from the repo root, so the
    phone's signer and the box's can never be checked against two copies."""
    source = (KOTLIN_TESTS / "OriginSignerGoldenTest.kt").read_text(encoding="utf-8")
    assert "Orchestrator/tests/fixtures/wellness/origin_proof_vectors.json" in source
    assert BOX_VECTORS.is_file()


@pytest.mark.parametrize("path", _vector_files(), ids=lambda p: p.name)
def test_every_vector_verifies_on_the_box(path):
    """Each vector's signature is reproduced by the box and by the
    simulator's independent signer over the request target (with its query,
    when the vector names one), its canonical string is the box's, and
    verify_request accepts it: as sent, with the method lower-cased, with a
    query string on the path, at the edges of the clock-skew window (and not
    one second past them)."""
    vectors = json.loads(path.read_text(encoding="utf-8"))
    assert len(vectors) >= 6
    for v in vectors:
        key = origin_proof.decode_key(v["k_b64url"])
        body = _vector_body(v)
        target = v.get("request_target") or v["path"]
        assert origin_proof.sign(key, v["enr"], v["ts"], v["nonce"], v["method"], target,
                                 body) == v["expected_sig"], v["path"]
        assert SIM.sign(key, v["enr"], v["ts"], v["nonce"], v["method"], target,
                        body) == v["expected_sig"], v["path"]
        if v.get("canonical") is not None:
            assert origin_proof.canonical(v["enr"], v["ts"], v["nonce"], v["method"], v["path"],
                                          body).decode("utf-8") == v["canonical"]
        assert _verify(v).enrollment_id == v["enr"]
        assert _verify(v, method=v["method"].lower()).enrollment_id == v["enr"]
        assert _verify(v, path=v["path"] + QUERY).enrollment_id == v["enr"]
        for skew in (origin_proof.MAX_SKEW_S, -origin_proof.MAX_SKEW_S):
            assert _verify(v, now=v["ts"] + skew).enrollment_id == v["enr"]
            with pytest.raises(origin_proof.OriginProofError) as e:
                _verify(v, now=v["ts"] + skew + (1 if skew > 0 else -1))
            assert e.value.status == 401 and e.value.code == "bad_signature"


def test_the_query_and_case_rule_is_the_phone_s():
    """The Kotlin test's own case (key bytes 0..31, en_0badc0de, ts 100): a
    header for GET /wellness/status equals one for get
    /wellness/status?operator=..., on the box and in the simulator, and the
    box verifies it against the path a request carries. With the vectors this
    closes the loop: the phone and the box both sign the path without its
    query, upper-cased."""
    key, enr, nonce = bytes(range(32)), "en_0badc0de", "0123456789abcdef0123456789abcdef"
    plain = origin_proof.signed_headers(key, enr, "GET", "/wellness/status", now=100, nonce=nonce)
    query = origin_proof.signed_headers(key, enr, "get", "/wellness/status" + QUERY, now=100,
                                        nonce=nonce)
    assert plain == query
    assert SIM.sign(key, enr, 100, nonce, "get", "/wellness/status" + QUERY, b"") == \
        origin_proof.sign(key, enr, 100, nonce, "GET", "/wellness/status", b"")
    v = origin_proof.verify_request(query[origin_proof.HEADER], "GET", "/wellness/status", b"",
                                    lookup_key=lambda e: key, now=100,
                                    nonces=origin_proof.NonceCache())
    assert v.enrollment_id == enr


def test_a_nonce_is_sixteen_bytes_of_lowercase_hex():
    """OriginSigner.newNonce draws 16 bytes as 32 lowercase hex: the box takes
    exactly that and refuses 15 or 17 bytes or upper case."""
    v = json.loads(BOX_VECTORS.read_text(encoding="utf-8"))[1]
    key = origin_proof.decode_key(v["k_b64url"])

    def header(nonce: str) -> str:
        sig = origin_proof.sign(key, v["enr"], v["ts"], nonce, v["method"], v["path"],
                                _vector_body(v))
        return origin_proof.format_header(v["enr"], v["ts"], nonce, sig)

    good = secrets.token_hex(16)
    assert _verify(v, header=header(good)).nonce == good
    for bad in (secrets.token_hex(15), secrets.token_hex(17), good.upper()):
        with pytest.raises(origin_proof.OriginProofError):
            _verify(v, header=header(bad))


def test_a_status_signed_as_the_phone_signs_it_is_credentialed(box):
    """IngestClient.status signs GET /wellness/status?operator=... (the query
    and all): the box answers with the settings view."""
    r = _send(box, "GET", "/wellness/status" + QUERY)
    assert r.status_code == 200 and "categories" in r.json(), r.text
    unsigned = _send(box, "GET", "/wellness/status" + QUERY, signed=False)
    assert unsigned.status_code == 200 and "categories" not in unsigned.json()


# ------------------------------------------------------------------ 3. the privacy policy

def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def test_the_box_serves_the_policy_this_test_reads():
    assert wellness_routes.POLICY_PATH.resolve() == BOX_POLICY.resolve()


def test_the_phone_shows_the_policy_the_box_serves():
    """WellnessRationaleActivity renders the bundled copy; GET
    /wellness/privacy-policy serves the box's. A person must consent to the
    text the box records the version of."""
    assert _lf(PHONE_POLICY.read_bytes()) == _lf(BOX_POLICY.read_bytes())


def test_the_policy_hash_literal_is_the_box_copy():
    assert POLICY_SHA.exists(), ("the Android PolicyHashTest's hash file is gone: the policy "
                                 "check would otherwise go quiet while the copies drift")
    found = re.search(r"\b[0-9a-fA-F]{64}\b", POLICY_SHA.read_text(encoding="utf-8"))
    assert found, "policy_sha256.txt holds no SHA-256 hex digest"
    assert found.group(0).lower() == hashlib.sha256(_lf(BOX_POLICY.read_bytes())).hexdigest()


def test_both_policy_copies_carry_the_box_s_policy_version():
    """The version the box records at every consent (catalog.POLICY_VERSION,
    X-Policy-Version) is the one each copy states."""
    for path in (PHONE_POLICY, BOX_POLICY):
        lines = path.read_text(encoding="utf-8").splitlines()
        assert f"Version {catalog.POLICY_VERSION}" in lines[:5], path


# ------------------------------------------------------------------ 4. the capability report

def test_the_capability_report_is_taken_whole_and_reflected(box):
    """The probe's report (enroll took it too) is accepted signed, stored with
    every field it carries (none silently dropped by the box's lenient
    model), names only box metric ids, dates samples in ISO-8601 UTC, and is
    what the signed status (every key WellnessStatusParser reads) and
    wellness_sources reflect: all permissions granted, so no trouble."""
    r = _send(box, "POST", "/wellness/capabilities", CAPS_WIRE)
    assert r.status_code == 200 and r.json() == {"ok": True}, r.text
    assert _store().meta_get("capabilities") == CAPS

    metrics = [m for o in CAPS["origins"] for m in o["metrics"]]
    ids = {m["metric"] for m in metrics}
    assert ids <= set(catalog.METRICS)
    assert {"sleep_duration", "sleep_stages", "bp_systolic", "bp_diastolic"} <= ids
    for m in metrics:
        assert coverage.iso_to_ms(m["last_sample"]) == _iso_ms(m["last_sample"])
    assert coverage.iso_to_ms(CAPS["reported_at"]) == _iso_ms(CAPS["reported_at"])

    status = _send(box, "GET", "/wellness/status" + QUERY).json()
    read_by_phone = {"state", "enrolled", "source_label", "last_sync", "troubles", "categories",
                     "local_only", "providers_allowed", "google_paid_key_confirmed",
                     "surfaces_mode", "instinct_prompt", "wear", "sync", "policy_version",
                     "disclosures_count"}
    assert read_by_phone <= set(status)
    assert {"hr_mode", "nights_worn_7"} <= set(status["wear"])
    assert {"last_push_at", "outbox_hint"} <= set(status["sync"])
    assert status["state"] == "active" and status["troubles"] == []
    assert status["categories"] == catalog.DEFAULT_CONSENT
    assert status["policy_version"] == catalog.POLICY_VERSION
    phone = CAPS["phone"]
    assert status["source_label"] == f"{phone['device_name']} ({phone['model']})"

    src = api.sources(OPERATOR, now_ms=NOW_MS)
    assert src["ok"] is True
    assert sorted((o["metric"], o["latest"], o["count_14d"]) for o in src["origins"]) == \
        sorted((m["metric"], m["last_sample"], m["count_14d"]) for m in metrics)
    hc = src["platforms"]["health_connect"]
    assert (hc["status"], hc["background_access"], hc["past_data_access"]) == \
        (CAPS["health_connect"]["sdk_status"], True, True)
    assert hc["earliest_available"] is None and src["sync_mode"] == "background"
    assert src["platforms"]["samsung_health"] == CAPS["samsung_health"]


def test_revoked_permissions_in_the_report_show_as_troubles(box):
    """The same probe on a phone with sleep, past-data and background access
    taken away (and blood pressure's too, which is off, so no trouble): the
    status needs attention with the phone's trouble codes, and coverage
    starts 30 days before the enrollment."""
    report = json.loads(CAPS_WIRE)
    prefix = catalog.HC_PERMISSION_PREFIX
    gone = {prefix + p for p in ("READ_SLEEP", "READ_BLOOD_PRESSURE", catalog.PERMISSION_HISTORY,
                                 catalog.PERMISSION_BACKGROUND)}
    report["health_connect"]["granted"] = [g for g in report["health_connect"]["granted"]
                                           if g not in gone]
    r = _send(box, "POST", "/wellness/capabilities", _compact(report))
    assert r.status_code == 200, r.text
    status = _send(box, "GET", "/wellness/status" + QUERY).json()
    assert status["state"] == "needs_attention"
    assert status["troubles"] == ["permission_revoked:sleep", "history_window_capped",
                                  "foreground_only"]
    src = api.sources(OPERATOR, now_ms=NOW_MS)
    hc = src["platforms"]["health_connect"]
    created = datetime.fromtimestamp(NOW_MS / 1000, tz=timezone.utc).date()
    assert hc["earliest_available"] == (created - timedelta(days=30)).isoformat()
    assert (hc["background_access"], hc["past_data_access"]) == (False, False)
    assert src["sync_mode"] == "when BlackBox is open"


# ------------------------------------------------------------------ 5. the catalog mirror

def test_the_phone_catalog_mirror_is_the_box_catalog():
    """res/raw/wellness_catalog.json (WellnessCatalogTest keeps it equal to
    WellnessCatalog.kt) names the box's four categories with their defaults,
    the box's permission per category in the box's order (the first is the
    one a category cannot work without), the history and background
    permissions, the v1 metric list with its categories, and ships rows in
    the shapes the box ingests."""
    cat = json.loads(PHONE_CATALOG.read_text(encoding="utf-8"))
    assert cat["schema"] == "bbx.wellness.catalog/1"
    assert [c["id"] for c in cat["categories"]] == list(catalog.CATEGORIES)
    types = {t["id"]: t for t in cat["hc_types"]}
    prefix = catalog.HC_PERMISSION_PREFIX
    for c in cat["categories"]:
        cid = c["id"]
        assert (c["label"], c["default_consent"], c["default_local_only"]) == \
            (catalog.CATEGORY_LABELS[cid], catalog.DEFAULT_CONSENT[cid],
             catalog.DEFAULT_LOCAL_ONLY[cid])
        perms = [types[t]["permission"] for t in c["hc_types"]]
        assert perms == [prefix + p for p in catalog.CATEGORY_PERMISSIONS[cid]], cid
        assert {types[t]["category"] for t in c["hc_types"]} == {cid}
    assert cat["special_permissions"] == {"history": prefix + catalog.PERMISSION_HISTORY,
                                          "background": prefix + catalog.PERMISSION_BACKGROUND}
    assert {m["id"]: m["category"] for m in cat["metrics"]} == \
        {k: m.category for k, m in catalog.METRICS.items()}
    shapes = {}
    for t in cat["hc_types"]:
        shapes.setdefault(t["shape"], set()).add(t["id"])
    # A reading's wire metric is the box's metric id (resting_hr, as
    # ingest_sleep_heart.json carries it), not the Health Connect type id.
    wire = {"resting_heart_rate": "resting_hr"}
    assert set(shapes) == {"bucketed", "session", "reading"}
    assert shapes["bucketed"] == set(catalog.BUCKET_METRICS)
    assert shapes["session"] == set(catalog.SESSION_KINDS)
    assert {wire.get(t, t) for t in shapes["reading"]} == set(catalog.READING_METRICS)
    _wire, sleep = _golden("ingest_sleep_heart.json")
    assert "resting_hr" in {r["metric"] for r in sleep["readings"]}
