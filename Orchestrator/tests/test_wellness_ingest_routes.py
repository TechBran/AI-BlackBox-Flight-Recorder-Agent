"""POST /wellness/ingest and /wellness/capabilities through the real app:
200 / duplicate, 401 (unsigned, wrong key, stale clock, replayed nonce),
410, 413 (compressed and decompressed caps), 422 (malformed structure only),
429, paused and the kill switch, flagged values, pending notices, and rows
of a category whose consent is off dropped at the door."""
from __future__ import annotations

import gzip
import json
import os
import time

import pytest

from Orchestrator.tests.wellness_support import (OPERATOR, close_box, fixtures, make_box)
from Orchestrator.wellness import enroll, ingest, origin_proof

FX = fixtures()


@pytest.fixture
def box(isolated_stores, toolvault_embed_sync, monkeypatch):
    b = make_box(isolated_stores, monkeypatch)
    r = b.enroll()
    assert r.status_code == 200, r.text
    b.enr, b.epoch = r.json()["enrollment_id"], r.json()["epoch"]
    yield b
    close_box(b)


def _batch(box, i=0, **over):
    b = dict(FX.load("days_10")["batches"][i], enrollment_id=box.enr, epoch=box.epoch)
    b.update(over)
    return b


def _store(box):
    return enroll.store_for(enroll.get_enrollment(OPERATOR))


def _bp_rows(batch):
    return sum(1 for r in batch["readings"] if r["metric"] == "blood_pressure")


def _put(box, body, signed=True):
    return box.post("/wellness/settings", dict({"operator": OPERATOR}, **body), signed=signed,
                    method="PUT")


def test_accepted_then_duplicate(box):
    r = box.push(_batch(box))
    assert r.status_code == 200, r.text
    body = r.json()
    # Blood pressure is off by default: its readings are dropped and counted.
    assert body["accepted"] > 1000 and body["dropped"] == _bp_rows(_batch(box)) > 0
    assert body["next_min_interval_s"] == 900 and body["pending_notices"] == []
    assert set(body) == {"accepted", "dropped", "next_min_interval_s", "pending_notices"}
    again = box.push(_batch(box))
    assert again.status_code == 200
    assert again.json()["accepted"] == 0 and again.json()["duplicate"] is True
    assert enroll.get_enrollment(OPERATOR)["last_push_at"]


def test_plain_json_is_accepted_too(box):
    r = box.push(_batch(box), gzip_it=False)
    assert r.status_code == 200 and r.json()["accepted"] > 0


def test_unsigned_is_401(box):
    data = gzip.compress(json.dumps(_batch(box)).encode())
    r = box.post("/wellness/ingest", raw=data, headers={"Content-Encoding": "gzip"})
    assert r.status_code == 401 and r.json() == {"error": "bad_signature"}


def test_wrong_key_stale_clock_and_replay_are_401(box):
    data = json.dumps(_batch(box)).encode()
    forged = origin_proof.signed_headers(bytes(32), box.enr, "POST", "/wellness/ingest", data)
    assert box.post("/wellness/ingest", raw=data, headers=forged).status_code == 401
    key = origin_proof.decode_key(box.phone.key)
    stale = origin_proof.signed_headers(key, box.enr, "POST", "/wellness/ingest", data,
                                        now=int(time.time()) - 300)
    assert box.post("/wellness/ingest", raw=data, headers=stale).status_code == 401
    good = origin_proof.signed_headers(key, box.enr, "POST", "/wellness/ingest", data)
    assert box.post("/wellness/ingest", raw=data, headers=good).status_code == 200
    replay = box.post("/wellness/ingest", raw=data, headers=good)
    assert replay.status_code == 401 and replay.json() == {"error": "bad_signature"}


def test_a_signature_for_another_path_or_body_fails(box):
    data = json.dumps(_batch(box)).encode()
    h = box.phone.headers("POST", "/wellness/capabilities", data)
    assert box.post("/wellness/ingest", raw=data, headers=h).status_code == 401
    h = box.phone.headers("POST", "/wellness/ingest", data + b" ")
    assert box.post("/wellness/ingest", raw=data, headers=h).status_code == 401


def test_revoked_key_is_410(box):
    key, enr = box.phone.key, box.phone.enrollment_id
    r = box.post("/wellness/disconnect", {"operator": OPERATOR, "keep_history": True})
    assert r.status_code == 200 and r.json() == {"state": "revoked"}
    assert box.phone.key is None                      # the phone was told to forget
    box.phone.key, box.phone.enrollment_id = key, enr  # ... and one that missed it pushes
    r = box.push(_batch(box))
    assert r.status_code == 410 and r.json() == {"error": "revoked"}


def test_compressed_cap_is_413(box):
    big = os.urandom(ingest.MAX_COMPRESSED_BYTES + 10)
    r = box.post("/wellness/ingest", raw=big, signed=True, headers={"Content-Encoding": "gzip"})
    assert r.status_code == 413 and r.json() == {"error": "too_large"}


def test_gzip_bomb_is_413_without_inflating_it(box):
    bomb = gzip.compress(b"{" + b" " * (ingest.MAX_DECOMPRESSED_BYTES + 100) + b"}")
    assert len(bomb) < ingest.MAX_COMPRESSED_BYTES
    r = box.post("/wellness/ingest", raw=bomb, signed=True, headers={"Content-Encoding": "gzip"})
    assert r.status_code == 413


def test_decompress_streams_with_a_cap():
    ok = ingest.decompress(gzip.compress(b"x" * 1000), "gzip")
    assert ok == b"x" * 1000
    with pytest.raises(ingest.IngestError) as e:
        ingest.decompress(gzip.compress(b"0" * (ingest.MAX_DECOMPRESSED_BYTES + 1)), "gzip")
    assert e.value.status == 413
    with pytest.raises(ingest.IngestError) as e:
        ingest.decompress(b"not gzip at all", "gzip")
    assert e.value.status == 422
    with pytest.raises(ingest.IngestError) as e:
        ingest.decompress(gzip.compress(b"abc")[:-6], "gzip")
    assert e.value.status == 422
    with pytest.raises(ingest.IngestError):
        ingest.decompress(b"{}", "br")


@pytest.mark.parametrize("mutate,detail", [
    (lambda b: dict(b, surprise=1), "surprise"),
    (lambda b: dict(b, epoch="1"), "epoch"),
    (lambda b: dict(b, buckets=[{"metric": "steps", "grain": "2h", "origin": "hc_merged",
                                 "rows": [[1, 2]]}]), "grain"),
    (lambda b: dict(b, readings=[{"metric": "spo2", "hc_id": "x", "origin": "o1",
                                  "t": "soon", "v": 97}]), "t"),
    (lambda b: dict(b, schema="bbx.wellness.ingest/2"), "schema"),
])
def test_malformed_structure_is_422(box, mutate, detail):
    r = box.push(mutate(_batch(box)))
    assert r.status_code == 422, r.text
    assert r.json()["error"] == "invalid" and detail in r.json()["detail"]


def test_not_json_and_nan_are_422(box):
    for raw in (b"{not json", b'{"x": NaN}', b"[1, 2]"):
        r = box.post("/wellness/ingest", raw=raw, signed=True)
        assert r.status_code == 422, raw


def test_epoch_or_enrollment_mismatch_is_422(box):
    assert box.push(_batch(box, epoch=7)).status_code == 422
    assert box.push(_batch(box, enrollment_id="en_ffffffff")).status_code == 422


def test_one_implausible_value_is_flagged_never_422(box):
    assert _put(box, {"categories": {"blood_pressure": "ask"}}).status_code == 200
    t = FX.ms(-1, 3, 2)
    b = _batch(box, batch_id="fx-flag", buckets=[{
        "metric": "heart_rate", "grain": "5min", "origin": "o1", "replace_day": None,
        "rows": [[t, 400.0, 390.0, 410.0, 5], [t + 300_000, 61.0, 58.0, 64.0, 5]]}],
        sessions=[], readings=[{"metric": "blood_pressure", "hc_id": "bp-x", "origin": "o2",
                                "t": t, "v": 80.0, "v2": 120.0}], deleted_ids=[])
    r = box.push(b)
    assert r.status_code == 200 and r.json()["accepted"] == 3
    with _store(box).read() as conn:
        dropped = conn.execute("SELECT dropped FROM ingest_log WHERE batch_id='fx-flag'"
                               ).fetchone()[0]
    assert json.loads(dropped) == {"flagged": 2}


def test_rate_limit_is_429_with_retry_after(box, monkeypatch):
    from Orchestrator.routes import wellness_routes
    monkeypatch.setattr(wellness_routes, "RATE", wellness_routes._RateLimit(capacity=2,
                                                                            window_s=600))
    assert box.push(_batch(box, 0)).status_code == 200
    assert box.push(_batch(box, 1)).status_code == 200
    r = box.push(_batch(box, 0, batch_id="fx-third"))
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


def test_paused_holds_batches_and_resume_needs_the_phone(box):
    assert box.post("/wellness/pause", {"operator": OPERATOR}).json() == {"state": "paused"}
    r = box.push(_batch(box))
    assert r.status_code == 503 and r.json() == {"error": "paused"} and r.headers["Retry-After"]
    assert box.post("/wellness/resume", {"operator": OPERATOR}).status_code == 401
    r = box.post("/wellness/resume", {"operator": OPERATOR}, signed=True)
    assert r.status_code == 200 and r.json()["state"] in ("active", "stale")
    assert box.push(_batch(box)).status_code == 200


def test_kill_switch_holds_batches(box):
    from Orchestrator.utils.config_ini import update_config_ini

    def off(p):
        if not p.has_section("wellness"):
            p.add_section("wellness")
        p.set("wellness", "enabled", "false")
    update_config_ini(off)
    r = box.push(_batch(box))
    assert r.status_code == 503 and r.json() == {"error": "wellness_disabled"}


def test_pending_notices_ride_back_once(box):
    st = _store(box)
    nid = st.add_notice("briefing", "Your morning briefing", "Open BlackBox",
                        now=int(time.time() * 1000))
    first = box.push(_batch(box, 0)).json()["pending_notices"]
    assert [n["id"] for n in first] == [nid] and first[0]["title"] == "Your morning briefing"
    assert box.push(_batch(box, 1)).json()["pending_notices"] == []


def test_capabilities_report(box):
    report = json.loads(json.dumps(FX.load("history_capped_30")["capabilities"]))
    report["health_connect"]["granted"] = [g for g in report["health_connect"]["granted"]
                                           if not g.endswith("READ_SLEEP")]
    report["future_field"] = {"ignored": True}
    r = box.post("/wellness/capabilities", report, signed=True)
    assert r.status_code == 200 and r.json() == {"ok": True}
    rec = enroll.get_enrollment(OPERATOR)
    assert "permission_revoked:sleep" in rec["phone_troubles"]
    assert "history_window_capped" in rec["phone_troubles"]
    assert enroll.effective_state(rec) == "needs_attention"
    assert box.post("/wellness/capabilities", report).status_code == 401
    assert "future_field" not in json.dumps(_store(box).meta_get("capabilities"))


def test_sync_now_nudges_the_phone(box):
    r = box.post("/wellness/sync-now", {"operator": OPERATOR})
    assert r.status_code == 200 and r.json() == {"nudged": True}
    assert ("POST", "/wellness/sync") in [(m, p) for m, p, _ in box.phone.calls]
    box.phone.down = True
    assert box.post("/wellness/sync-now", {"operator": OPERATOR}).json() == {"nudged": False}
    assert "source_unreachable" in enroll.get_enrollment(OPERATOR)["phone_troubles"]


def test_the_kill_switch_stops_capability_reports(box):
    from Orchestrator.utils.config_ini import update_config_ini

    def off(p):
        if not p.has_section("wellness"):
            p.add_section("wellness")
        p.set("wellness", "enabled", "false")
    update_config_ini(off)
    rep = json.dumps(FX.load("continuous_120")["capabilities"]).encode()
    r = box.post("/wellness/capabilities", raw=rep, signed=True)
    assert r.status_code == 503 and r.json() == {"error": "wellness_disabled"}
    assert box.post("/wellness/sync-now", {"operator": OPERATOR}).status_code == 503


def test_a_push_clears_a_failed_nudge(box):
    """A nudge the phone did not answer marks it unreachable (needs
    attention); the next batch from it proves otherwise."""
    box.phone.down = True
    assert box.post("/wellness/sync-now", {"operator": OPERATOR}).json() == {"nudged": False}
    assert box.get(f"/wellness/status?operator={OPERATOR}").json()["state"] == \
        "needs_attention"
    box.phone.down = False
    assert box.push(_batch(box)).status_code == 200
    assert "source_unreachable" not in enroll.get_enrollment(OPERATOR)["phone_troubles"]
    assert box.get(f"/wellness/status?operator={OPERATOR}").json()["state"] == "active"


# ------------------------------------------------------------------ consent at the door

def test_rows_of_a_category_turned_off_are_never_stored(box):
    """Defense in depth (the phone should not send them): a row whose
    category is off in the enrollment is dropped at ingest and counted, and
    nothing of it reaches the store; a category turned back on starts from
    the next push."""
    first, second = _batch(box, 0), _batch(box, 1)
    assert _bp_rows(first) and _bp_rows(second)
    r = box.push(first)
    assert r.status_code == 200 and r.json()["dropped"] == _bp_rows(first)
    with _store(box).read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reading WHERE metric='blood_pressure'"
                            ).fetchone()[0] == 0
        dropped = conn.execute("SELECT dropped FROM ingest_log WHERE batch_id=?",
                               (first["batch_id"],)).fetchone()[0]
    assert json.loads(dropped) == {"category_off": _bp_rows(first)}

    assert _put(box, {"categories": {"blood_pressure": "ask"}}).status_code == 200
    r = box.push(second)
    assert r.status_code == 200 and r.json()["dropped"] == 0
    with _store(box).read() as conn:
        days = {d for (d,) in conn.execute(
            "SELECT DISTINCT date(ts / 1000 - 14400, 'unixepoch') FROM reading "
            "WHERE metric='blood_pressure'")}
    second_days = {g["replace_day"] for g in second["buckets"]}
    first_days = {g["replace_day"] for g in first["buckets"]}
    assert days and days <= second_days and not days & (first_days - second_days)


def test_turning_a_category_off_stops_it_on_the_next_push(box):
    assert box.push(_batch(box, 1)).status_code == 200
    with _store(box).read() as conn:
        before = conn.execute("SELECT COUNT(*) FROM reading WHERE metric='spo2'").fetchone()[0]
    assert before > 0
    assert _put(box, {"categories": {"heart": "off"}}, signed=False).status_code == 200
    b = _batch(box, 0)                      # the week with the run (1-minute HR, avg/max HR)
    r = box.push(b)
    assert r.status_code == 200
    heart = sum(len(g["rows"]) for g in b["buckets"] if g["metric"] == "heart_rate") + \
        sum(1 for x in b["readings"] if x["metric"] in ("resting_hr", "hrv", "spo2",
                                                        "respiratory_rate"))
    assert r.json()["dropped"] == heart + _bp_rows(b)
    st = _store(box)
    lo = st.offsets().bounds(min(g["replace_day"] for g in b["buckets"]))[0]
    with st.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reading WHERE metric='spo2'"
                            ).fetchone()[0] == before
        assert conn.execute("SELECT COUNT(*) FROM bucket WHERE metric='heart_rate' AND ts>=?",
                            (lo,)).fetchone()[0] == 0
        # Workouts are activity: kept, without the heart rate they carry.
        attrs = {hc: json.loads(a or "{}") for hc, a in conn.execute(
            "SELECT hc_id, attrs FROM session WHERE kind='exercise' AND end_ts>=?", (lo,))}
    run = attrs[f"fx-run-{FX.day_of(FX.RUN_DAY)}"]
    assert run["distance_m"] == 7210.0 and not {"avg_hr", "max_hr"} & set(run)


def test_every_wire_row_has_a_consent_category():
    from Orchestrator.wellness import catalog
    assert set(ingest.BUCKET_CATEGORY) == set(catalog.BUCKET_METRICS)
    assert set(ingest.READING_CATEGORY) == set(catalog.READING_METRICS)
    assert set(ingest.SESSION_CATEGORY) == set(catalog.SESSION_KINDS)
    every = set(ingest.BUCKET_CATEGORY.values()) | set(ingest.READING_CATEGORY.values()) | \
        set(ingest.SESSION_CATEGORY.values())
    assert every == set(catalog.CATEGORIES)


def test_the_consent_is_read_when_the_batch_is_written(tmp_path, monkeypatch):
    """process_batch asks for the consent under the store's write lock (a
    settings change that lands while a batch is being parsed wins), and an
    enrollment that has no consent map gets nothing stored."""
    from Orchestrator.tests.wellness_support import ENR, fresh_store
    from Orchestrator.wellness import schema
    st = fresh_store(tmp_path, monkeypatch)
    b = dict(FX.load("days_10")["batches"][0], enrollment_id=ENR, epoch=1)
    asked = []

    def consent():
        asked.append(1)
        return {"sleep": "ask", "heart": "off", "activity": "off", "blood_pressure": "off"}

    res = ingest.process_batch(st, schema.IngestBatch.model_validate(b), enrollment_id=ENR,
                               epoch=1, now_ms=FX.now_ms(), consent=consent)
    assert asked == [1]
    assert res.accepted == sum(1 for x in b["sessions"] if x["kind"] == "sleep")
    with st.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bucket").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM reading").fetchone()[0] == 0
    st2 = fresh_store(tmp_path / "b", monkeypatch, op_key="00000000000000dd")
    res = ingest.process_batch(st2, schema.IngestBatch.model_validate(b), enrollment_id=ENR,
                               epoch=1, now_ms=FX.now_ms(), consent=lambda: None)
    assert res.accepted == 0 and res.dropped > 0


def test_the_drop_is_logged_as_a_count(box, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="Orchestrator.wellness.ingest"):
        assert box.push(_batch(box)).status_code == 200
    line = next(r.getMessage() for r in caplog.records if "[WELLNESS] ingest" in r.getMessage())
    assert f"off={_bp_rows(_batch(box))}" in line
    assert "118" not in line and "blood" not in line
