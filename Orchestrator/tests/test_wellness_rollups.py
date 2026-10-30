"""Rollups and baselines (spec §3.5) over the deterministic fixture: exact
sentinel values, asleep-stage sums, the 4-night rise against the usual range,
baselines null under 14 days, late data recomputing what it feeds, order
independence, flagged values excluded, deletions, the effective-epoch rule,
and no wall-clock reads."""
from __future__ import annotations

import time

import pytest

from Orchestrator.tests.wellness_support import (fixtures, fresh_store, ingest_batches,
                                                 ingest_fixture, rollup)
from Orchestrator.wellness import catalog, rollups, store as store_mod

FX = fixtures()
DAY = FX.day_of


@pytest.fixture(scope="module")
def full(tmp_path_factory):
    """continuous_120 ingested once for the read-only tests of this module."""
    mp = pytest.MonkeyPatch()
    st = fresh_store(tmp_path_factory.mktemp("full"), mp)
    ingest_fixture(st, "continuous_120")
    yield st
    mp.undo()


def _night(st, metric, i):
    r = rollup(st, metric, "night", DAY(i))
    return None if r is None else r["value"]


def _day(st, metric, i):
    r = rollup(st, metric, "day", DAY(i))
    return None if r is None else r["value"]


# ------------------------------------------------------------------ exact values

def test_sentinels_come_out_exactly(full):
    assert _night(full, "sleeping_hr_median", -5) == 47.3
    assert _day(full, "steps", -3) == 12345
    start, end = store_mod.day_bounds(DAY(-2), FX.OFFSET_S)
    bp = full.readings("blood_pressure", start, end)
    assert (bp[0]["v1"], bp[0]["v2"]) == (118.0, 79.0)


def test_sleep_duration_is_the_sum_of_asleep_stages(full):
    s0, s1, stages = FX._sleep("continuous_120", -10)
    asleep = sum(b - a for a, b, c in stages if c in catalog.ASLEEP_STAGES) / 60_000
    awake = sum(b - a for a, b, c in stages if c in (1, 7)) / 60_000
    assert _night(full, "sleep_duration", -10) == pytest.approx(asleep, abs=0.01)
    assert _night(full, "sleep_awake", -10) == pytest.approx(awake, abs=0.01)
    r = rollup(full, "sleep_duration", "night", DAY(-10))
    assert r["method"].startswith("sum of asleep stages recorded by Samsung Health")
    assert _night(full, "sleep_onset", -10) == pytest.approx((s0 - FX.ms(-10, 0)) / 60_000)
    assert _night(full, "sleep_onset", -10) < 0          # before midnight


def test_four_night_rise_reads_above_the_usual_range(full):
    for i in range(-12, -8):
        value = _night(full, "sleeping_hr_median", i)
        cmp = rollups.vs_usual("sleeping_hr_median", value,
                               full.baseline("sleeping_hr_median", DAY(i)))
        assert cmp["label"] == "above your usual range", (i, value, cmp)
        assert cmp["delta_display"].endswith("bpm higher")
    calm = rollups.vs_usual("sleeping_hr_median", _night(full, "sleeping_hr_median", -20),
                            full.baseline("sleeping_hr_median", DAY(-20)))
    assert calm["label"] == "within your usual range"


def test_resting_hr_reported_or_estimated(full):
    reported = rollup(full, "resting_hr", "day", DAY(-10))     # even day: the watch wrote one
    estimated = rollup(full, "resting_hr", "day", DAY(-11))
    assert reported["method"] == "reported by Samsung Health, a watch"
    assert estimated["method"] == rollups.METHOD_RESTING_ESTIMATE
    assert 40 < estimated["value"] < 70


def test_the_run_has_one_minute_heart_rate_and_a_session(full):
    start, end = store_mod.day_bounds(DAY(-2), FX.OFFSET_S)
    one_min = full.buckets("heart_rate", "1min", start, end)
    assert len(one_min) == 45
    runs = full.sessions("exercise", start, end)
    run = [s for s in runs if s["attrs"].get("type") == "run"]
    assert len(run) == 1 and run[0]["attrs"]["distance_m"] == 7210.0
    assert _day(full, "exercise", -2) == 75.0        # the run plus the lunchtime walk


def test_gaps_are_null_never_zero(full):
    assert _night(full, "sleep_duration", -6) is None      # no session at all
    assert _night(full, "sleeping_hr_median", -6) is None
    assert _night(full, "sleeping_hr_median", -4) is None  # phone session, not worn
    assert _night(full, "sleep_duration", -4) is not None
    cov = full.coverage_days(DAY(-6), DAY(-4))
    assert cov[DAY(-6)]["night_worn"] is None and cov[DAY(-4)]["night_worn"] == 0


# ------------------------------------------------------------------ baselines

def test_baseline_needs_fourteen_valid_days(tmp_path, monkeypatch):
    st = fresh_store(tmp_path, monkeypatch)
    ingest_fixture(st, "days_10")
    b = st.baseline("sleep_duration", DAY(0))
    assert b["median"] is None and b["n"] < 14
    usual, reason = rollups.usual("sleep_duration", b)
    assert usual is None
    assert reason == f"not enough history yet ({b['n']} of 14 days)"


def test_baseline_with_history(full):
    b = full.baseline("sleep_duration", DAY(0))
    assert b["n"] >= 14 and b["median"] is not None
    usual, reason = rollups.usual("sleep_duration", b)
    assert reason is None and usual["low"] < usual["median"] < usual["high"]
    # Steps baselines skip the days with under 10 h of wear (every 9th day).
    with full.read() as conn:
        vals = rollups.baseline_values(conn, full, "steps", DAY(0))
    assert len(vals) == full.baseline("steps", DAY(0))["n"]
    assert len(vals) < 28                      # the short-wear days were left out


def test_usual_range_floors_and_labels():
    base = {"median": 60.0, "mad": 0.0, "n": 20}
    usual, _ = rollups.usual("sleeping_hr_median", base)
    assert (usual["low"], usual["high"]) == (57.0, 63.0)     # floor 2 bpm x 1.5
    assert rollups.vs_usual("sleeping_hr_median", 62.9, base)["label"] == "within your usual range"
    assert rollups.vs_usual("sleeping_hr_median", 63.1, base)["label"] == "above your usual range"
    assert rollups.vs_usual("sleeping_hr_median", 56.0, base)["label"] == "below your usual range"
    steps = rollups.usual("steps", {"median": 8000.0, "mad": 10.0, "n": 20})[0]
    assert steps["high"] == pytest.approx(8000 + 1.5 * 800)           # floor 10 %
    sleep = rollups.usual("sleep_duration", {"median": 420.0, "mad": 5.0, "n": 20})[0]
    assert sleep["low"] == pytest.approx(420 - 1.5 * 20)              # floor 20 min
    for word in ("normal", "abnormal"):
        assert word not in rollups.vs_usual("steps", 1000.0, {"median": 8000.0, "mad": 10.0,
                                                               "n": 20})["label"]


# ------------------------------------------------------------------ recompute

def _snapshot(st):
    with st.read() as conn:
        out = {}
        for table, cols in (("rollup", "metric, grain, bucket, n, value, median, p10, min, max, "
                                       "coverage, method"),
                            ("baseline", "metric, as_of, window_days, n, median, mad"),
                            ("coverage_day", "*"), ("digest", "kind, period, body")):
            out[table] = sorted(tuple(r) for r in conn.execute(f"SELECT {cols} FROM {table}"))
        return out


def test_order_independence(full, tmp_path, monkeypatch):
    """Pushing the same batches in the opposite order yields identical rollups,
    baselines, coverage and digests (the late-data recompute is complete)."""
    st = fresh_store(tmp_path, monkeypatch, op_key="00000000000000aa")
    ingest_fixture(st, "continuous_120", reverse=True)
    assert _snapshot(st) == _snapshot(full)


def test_late_data_recomputes(tmp_path, monkeypatch):
    data = FX.load("days_60")
    batches = data["batches"]
    st = fresh_store(tmp_path, monkeypatch)
    held = batches[3]                          # one week in the middle arrives late
    ingest_batches(st, [b for b in batches if b is not held], now_ms=data["now_ms"])
    gap_day = held["buckets"][len(held["buckets"]) // 2]["replace_day"]
    assert rollup(st, "steps", "day", gap_day) is None
    before = st.baseline("steps", store_mod.add_days(gap_day, 10))
    ingest_batches(st, [held], now_ms=data["now_ms"])
    assert rollup(st, "steps", "day", gap_day) is not None
    after = st.baseline("steps", store_mod.add_days(gap_day, 10))
    assert after["n"] > before["n"]
    full_st = fresh_store(tmp_path / "x", monkeypatch, op_key="00000000000000bb")
    ingest_batches(full_st, batches, now_ms=data["now_ms"])
    assert _snapshot(st) == _snapshot(full_st)


def test_flagged_values_are_excluded_and_counted(tmp_path, monkeypatch):
    data = FX.load("days_10")
    st = fresh_store(tmp_path, monkeypatch)
    ingest_batches(st, data["batches"], now_ms=data["now_ms"])
    before = rollup(st, "heart_rate", "day", DAY(-3))
    t = FX.ms(-3, 3, 2)        # between two real buckets, so it overwrites none
    bad = {"schema": "bbx.wellness.ingest/1", "batch_id": "fx-bad-1", "enrollment_id": "x",
           "epoch": 1, "origins": [FX.WATCH],
           "buckets": [{"metric": "heart_rate", "grain": "5min", "origin": "o1",
                        "replace_day": None, "offset_s": FX.OFFSET_S,
                        "rows": [[t, 400.0, 390.0, 410.0, 5]]}],
           "sessions": [], "readings": [
               {"metric": "spo2", "hc_id": "bad-spo2", "origin": "o1", "t": t, "v": 12.0}],
           "deleted_ids": []}
    res = ingest_batches(st, [bad], now_ms=data["now_ms"])[0]
    assert (res.accepted, res.flagged) == (2, 2)
    after = rollup(st, "heart_rate", "day", DAY(-3))
    assert after["max"] == before["max"] and after["n"] == before["n"]
    with st.read() as conn:
        assert conn.execute("SELECT dropped FROM ingest_log WHERE batch_id='fx-bad-1'"
                            ).fetchone()[0] == '{"flagged":2}'


def test_unknown_metric_is_dropped_not_refused(tmp_path, monkeypatch):
    st = fresh_store(tmp_path, monkeypatch)
    t = FX.ms(-1, 9)
    b = {"schema": "bbx.wellness.ingest/1", "batch_id": "fx-new-1", "enrollment_id": "x",
         "epoch": 1, "origins": [FX.WATCH],
         "buckets": [{"metric": "skin_temperature", "grain": "5min", "origin": "o1",
                      "replace_day": None, "rows": [[t, 33.1, 33.0, 33.2, 5]]}],
         "readings": [{"metric": "vo2max", "hc_id": "v1", "origin": "o1", "t": t, "v": 44.0},
                      {"metric": "spo2", "hc_id": "s1", "origin": "o9", "t": t, "v": 97.0}]}
    res = ingest_batches(st, [b], now_ms=FX.now_ms())[0]
    assert (res.accepted, res.dropped) == (0, 3)


def test_deletion_recomputes_the_night(tmp_path, monkeypatch):
    data = FX.load("days_10")
    st = fresh_store(tmp_path, monkeypatch)
    ingest_batches(st, data["batches"], now_ms=data["now_ms"])
    assert _night(st, "sleep_duration", -5) is not None
    gone = {"schema": "bbx.wellness.ingest/1", "batch_id": "fx-del-1", "enrollment_id": "x",
            "epoch": 1, "deleted_ids": [f"fx-sleep-{DAY(-5)}"]}
    res = ingest_batches(st, [gone], now_ms=data["now_ms"])[0]
    assert DAY(-5) in res.dirty_days
    assert _night(st, "sleep_duration", -5) is None
    assert _night(st, "sleeping_hr_median", -5) is None
    assert st.coverage_days(DAY(-5), DAY(-5))[DAY(-5)]["night_worn"] is None


def test_reenrolled_epoch_never_double_counts(tmp_path, monkeypatch):
    data = FX.load("days_10")
    st = fresh_store(tmp_path, monkeypatch)
    ingest_batches(st, data["batches"], now_ms=data["now_ms"], epoch=1)
    second = [dict(b, batch_id=b["batch_id"] + "-e2") for b in data["batches"]]
    ingest_batches(st, second, now_ms=data["now_ms"], epoch=2)
    assert _day(st, "steps", -3) == 12345
    assert _night(st, "sleeping_hr_median", -5) == 47.3


def test_no_wall_clock_inside_the_maths(tmp_path, monkeypatch):
    """The recompute runs with the clock taken away (logging, which stamps
    its own records, is outside it), and the sources never ask for one."""
    import inspect

    from Orchestrator.wellness import coverage, digests
    st = fresh_store(tmp_path, monkeypatch)
    data = FX.load("days_10")
    ingest_batches(st, data["batches"], now_ms=data["now_ms"])
    days = store_mod.day_range(*st.data_span())
    with st.write() as conn:
        conn.execute("DELETE FROM rollup")

    def boom(*a, **k):
        raise AssertionError("the rollups read the wall clock")

    with monkeypatch.context() as m:
        m.setattr(time, "time", boom)
        m.setattr(time, "time_ns", boom)
        m.setattr(time, "monotonic", boom)
        rollups.recompute(st, days, now_ms=data["now_ms"])
    assert _day(st, "steps", -3) == 12345
    for mod in (rollups, coverage, digests):
        src = inspect.getsource(mod)
        for call in ("time.time(", "datetime.now(", "date.today(", "datetime.utcnow("):
            assert call not in src, (mod.__name__, call)


def test_week_note_is_words_and_counts_only(full):
    monday = "2026-10-12"
    note = full.digest("week_note", monday)["text"]
    assert note.startswith("Week of Oct 12: ")
    assert " of " in note
    import re
    numbers = [int(x) for x in re.findall(r"\d+", note.split(": ", 1)[1])]
    assert numbers and max(numbers) <= 7       # counts of nights/days, never a value
