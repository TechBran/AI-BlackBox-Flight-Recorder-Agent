"""Daylight saving time and other zone changes: a local day runs from its own
midnight to the next one, so 2026-03-08 (US spring forward) is a 23-hour day
and 2026-11-01 (fall back) a 25-hour day. Replace-day, the readers, the
rollups (sums, the sleep clock times, the main-sleep window), coverage (wear
07:00-23:00 local) and the tools all follow it, whatever order the days
arrive in. The DST fixture variants live in US Eastern time
(Orchestrator/tests/fixtures/wellness/generate_fixtures.py)."""
from __future__ import annotations

import pytest

from Orchestrator.tests.wellness_support import (enrolled_fixture, fixtures, fresh_store,
                                                 ingest_batches, rollup)
from Orchestrator.wellness import api, catalog, store as store_mod

FX = fixtures()
SPRING, FALL = "dst_spring_21", "dst_fall_21"
EST, EDT = FX.EST, FX.EDT
H = 3_600_000
SPRING_DAY, FALL_DAY = "2026-03-08", "2026-11-01"


# ------------------------------------------------------------------ the day model

def test_a_day_ends_at_the_next_days_midnight():
    s, e = store_mod.day_bounds(SPRING_DAY, EST, EDT)
    assert e - s == 23 * H
    s, e = store_mod.day_bounds(FALL_DAY, EDT, EST)
    assert e - s == 25 * H
    assert store_mod.day_bounds("2026-10-20", EDT) == store_mod.day_bounds("2026-10-20", EDT, EDT)
    assert store_mod.day_bounds("2026-10-20", EDT)[1] - store_mod.day_bounds("2026-10-20", EDT)[0] \
        == 24 * H


@pytest.mark.parametrize("variant", [SPRING, FALL])
def test_the_days_tile_the_timeline_across_the_change(variant):
    z = FX.zone(variant)
    days = [z.day_of(i) for i in range(-20, 1)]
    offs = store_mod.Offsets({d: z.day_offset(i) for i, d in zip(range(-20, 1), days)})
    for a, b in zip(days, days[1:]):
        assert offs.bounds(a)[1] == offs.bounds(b)[0]
    t, end = z.ms(-20, 0), z.ms(0, 0)
    while t < end:                                   # every 20 minutes, both directions
        d = offs.day_of(t)
        assert d == z.day_of(z.index(t)), t
        assert offs.bounds(d)[0] <= t < offs.bounds(d)[1]
        assert offs.offset_at(t) == z.offset_at(t), t
        t += 20 * 60_000
    for t in reversed(range(z.ms(-20, 0), end, 37 * 60_000)):
        assert offs.day_of(t) == z.day_of(z.index(t))


@pytest.mark.parametrize("variant,hours", [(SPRING, (0, 1, 3, 7, 12, 23)),
                                           (FALL, (0, 1, 2, 3, 7, 12, 23))])
def test_wall_clock_times_on_the_change_day(variant, hours):
    z = FX.zone(variant)
    i = FX.DST_VARIANTS[variant]["dst_day"]
    day = z.day_of(i)
    offs = store_mod.Offsets({day: z.day_offset(i), z.day_of(i + 1): z.day_offset(i + 1)})
    for h in hours:
        t = z.ms(i, h)
        assert offs.at_local(day, h * 60) == t, h
        assert offs.local_minutes(day, t) == h * 60, h
    if variant == FALL:                             # 01:30 happens twice; both read 01:30
        first = z.ms(i, 1, 30)
        assert offs.local_minutes(day, first) == 90
        assert offs.local_minutes(day, first + H) == 90


# ------------------------------------------------------------------ ingest

def _rows(variant, metric, grain):
    out = {}
    for b in FX.load(variant)["batches"]:
        for g in b["buckets"]:
            if (g["metric"], g["grain"]) == (metric, grain):
                for r in g["rows"]:
                    out[r[0]] = (g["replace_day"], r)
    return out


@pytest.fixture(scope="module")
def dst(tmp_path_factory):
    """Both DST variants, ingested the way the phone backfills (newest week first)."""
    mp = pytest.MonkeyPatch()
    out = {}
    for k, variant in enumerate((SPRING, FALL)):
        st = fresh_store(tmp_path_factory.mktemp(variant), mp, op_key=f"{k + 1:016x}")
        data = FX.load(variant)
        ingest_batches(st, data["batches"], now_ms=data["now_ms"])
        out[variant] = st
    yield out
    mp.undo()


@pytest.mark.parametrize("variant", [SPRING, FALL])
def test_the_change_day_keeps_its_offset_and_length(dst, variant):
    """The group states the offset at the day's midnight; the readings after
    the change carry the new offset and must not move the day's start."""
    st, z = dst[variant], FX.zone(variant)
    i = FX.DST_VARIANTS[variant]["dst_day"]
    offs = st.offsets()
    assert offs.for_day(z.day_of(i)) == z.day_offset(i)
    assert offs.bounds(z.day_of(i)) == (z.ms(i, 0), z.ms(i + 1, 0))


@pytest.mark.parametrize("variant", [SPRING, FALL])
def test_every_row_lands_in_its_own_day(dst, variant):
    st, offs = dst[variant], dst[variant].offsets()
    for metric, grain in (("heart_rate", "5min"), ("steps", "1h"), ("heart_rate", "1min")):
        want = _rows(variant, metric, grain)
        with st.read() as conn:
            got = {r[0] for r in conn.execute("SELECT ts FROM bucket WHERE metric=? AND grain=?",
                                              (metric, grain))}
        assert got == set(want), (metric, grain)
        for ts, (day, _row) in want.items():
            assert offs.day_of(ts) == day, (metric, ts)


def test_resending_a_23_hour_day_leaves_the_next_days_first_hour(dst):
    """Re-sending 2026-03-08 must not delete 00:00-01:00 of 2026-03-09 (a
    24-hour replace would)."""
    st = dst[SPRING]
    data = FX.load(SPRING)
    groups = [g for b in data["batches"] for g in b["buckets"] if g["replace_day"] == SPRING_DAY]
    nxt = st.offsets().bounds("2026-03-09")
    with st.read() as conn:
        before = conn.execute("SELECT COUNT(*) FROM bucket WHERE ts>=? AND ts<?",
                              (nxt[0], nxt[0] + H)).fetchone()[0]
    assert before > 0
    ingest_batches(st, [{"schema": "bbx.wellness.ingest/1", "batch_id": "fx-spring-again",
                         "origins": [FX.WATCH, FX.PHONE], "buckets": groups}],
                   now_ms=data["now_ms"])
    with st.read() as conn:
        after = conn.execute("SELECT COUNT(*) FROM bucket WHERE ts>=? AND ts<?",
                             (nxt[0], nxt[0] + H)).fetchone()[0]
    assert after == before


def test_resending_a_25_hour_day_replaces_its_last_hour(tmp_path, monkeypatch):
    """The phone re-aggregates 2026-11-01 after deleting its last hour of
    heart rate: the 25th hour goes too (a 24-hour replace would keep it)."""
    data = FX.load(FALL)
    st = fresh_store(tmp_path, monkeypatch)
    ingest_batches(st, data["batches"], now_ms=data["now_ms"])
    z, i = FX.zone(FALL), FX.DST_VARIANTS[FALL]["dst_day"]
    last_hour = (z.ms(i + 1, 0) - H, z.ms(i + 1, 0))
    group = next(g for b in data["batches"] for g in b["buckets"]
                 if g["replace_day"] == FALL_DAY and g["metric"] == "heart_rate"
                 and g["grain"] == "5min")
    kept = [r for r in group["rows"] if r[0] < last_hour[0]]
    assert len(kept) < len(group["rows"])
    ingest_batches(st, [{"schema": "bbx.wellness.ingest/1", "batch_id": "fx-fall-again",
                         "origins": [FX.WATCH], "buckets": [dict(group, rows=kept)]}],
                   now_ms=data["now_ms"])
    with st.read() as conn:
        left = conn.execute("SELECT COUNT(*) FROM bucket WHERE metric='heart_rate' AND "
                            "grain='5min' AND ts>=? AND ts<?", last_hour).fetchone()[0]
    assert left == 0


def _one_day(variant, day, now_ms):
    """Every bucket group, session and reading of one local day (the hourly
    worker's push), as a batch."""
    z = FX.zone(variant)
    out = {"schema": "bbx.wellness.ingest/1", "batch_id": f"fx-day-{day}",
           "origins": [FX.WATCH, FX.PHONE], "buckets": [], "sessions": [], "readings": []}
    for b in FX.load(variant)["batches"]:
        out["buckets"] += [g for g in b["buckets"] if g["replace_day"] == day]
        out["sessions"] += [s for s in b["sessions"] if z.day_of(z.index(s["end"])) == day]
        out["readings"] += [r for r in b["readings"] if z.day_of(z.index(r["t"])) == day]
    return out


def _snapshot(st):
    with st.read() as conn:
        out = {}
        for table, cols in (("rollup", "metric, grain, bucket, n, value, median, p10, min, max, "
                                       "coverage, method"),
                            ("baseline", "metric, as_of, window_days, n, median, mad"),
                            ("coverage_day", "*"), ("digest", "kind, period, body"),
                            ("meta", "key, value")):
            out[table] = sorted(tuple(r) for r in conn.execute(f"SELECT {cols} FROM {table}"))
        return out


@pytest.mark.parametrize("variant", [SPRING, FALL])
def test_day_by_day_pushes_match_the_backfill(dst, variant, tmp_path, monkeypatch):
    """The hourly worker's order (one day at a time, oldest first, the next
    day's offset arriving a day later) ends exactly where the backfill does."""
    data = FX.load(variant)
    z = FX.zone(variant)
    st = fresh_store(tmp_path, monkeypatch, op_key="00000000000000cc")
    days = sorted({g["replace_day"] for b in data["batches"] for g in b["buckets"]})
    ingest_batches(st, [_one_day(variant, d, data["now_ms"]) for d in days],
                   now_ms=data["now_ms"])
    assert z.day_of(FX.DST_VARIANTS[variant]["dst_day"]) in days
    assert _snapshot(st) == _snapshot(dst[variant])


# ------------------------------------------------------------------ rollups and coverage

@pytest.mark.parametrize("variant", [SPRING, FALL])
def test_daily_sums_count_each_hour_once(dst, variant):
    st = dst[variant]
    by_day = {}
    for _ts, (day, row) in _rows(variant, "steps", "1h").items():
        by_day[day] = by_day.get(day, 0) + row[1]
    for day, total in by_day.items():
        assert rollup(st, "steps", "day", day)["value"] == total, day


@pytest.mark.parametrize("variant", [SPRING, FALL])
def test_a_full_day_of_wear_is_16_hours_on_the_change_day(dst, variant):
    """07:00-23:00 local is 16 hours of wall clock on a 23- or 25-hour day."""
    z = FX.zone(variant)
    i = FX.DST_VARIANTS[variant]["dst_day"]
    cover = dst[variant].coverage_days(z.day_of(i - 1), z.day_of(i + 1))
    for k in (i - 1, i, i + 1):
        if k % 9 == 0:                  # the fixture's watch-off afternoon (12:00-19:30)
            assert cover[z.day_of(k)]["day_wear_h"] == 8.5, z.day_of(k)
        else:
            assert cover[z.day_of(k)]["day_wear_h"] == 16.0, z.day_of(k)


@pytest.mark.parametrize("variant", [SPRING, FALL])
def test_bedtime_and_wake_time_read_the_wall_clock(dst, variant):
    st, z = dst[variant], FX.zone(variant)
    i = FX.DST_VARIANTS[variant]["dst_day"]
    for k in (i, i + 1):
        s0, s1, _stages = FX._sleep(variant, k)
        day = z.day_of(k)
        local = lambda t: (t + z.offset_at(t) * 1000 - store_mod._midnight_ms(day)) / 60_000  # noqa: E731
        assert rollup(st, "sleep_wake", "night", day)["value"] == round(local(s1), 2), day
        assert rollup(st, "sleep_onset", "night", day)["value"] == round(local(s0), 2), day
    night = rollup(st, "sleep_duration", "night", z.day_of(i))
    assert night is not None and st.coverage_days(z.day_of(i), z.day_of(i))[z.day_of(i)][
        "night_worn"] == 1


def test_the_main_sleep_window_is_local(dst):
    """A main sleep must end 03:00-14:00 local: on the fall-back day that is
    08:00-19:00 UTC, not the 07:00-18:00 a 24-hour day would give."""
    offs = dst[FALL].offsets()
    z, i = FX.zone(FALL), FX.DST_VARIANTS[FALL]["dst_day"]
    assert offs.at_local(FALL_DAY, 3 * 60) == z.ms(i, 3)
    assert offs.at_local(FALL_DAY, 14 * 60) == z.ms(i, 14)


# ------------------------------------------------------------------ the next day not known yet

def _fall_day_only(tmp_path, monkeypatch, *, readings=(), last_hour=True):
    """2026-11-01 pushed on its own evening: the next day's offset is not
    known yet."""
    z, i = FX.zone(FALL), FX.DST_VARIANTS[FALL]["dst_day"]
    start, end = z.ms(i, 0), z.ms(i + 1, 0)
    rows = [[t, 60.0, 58.0, 62.0, 5] for t in range(start, end if last_hour else end - H,
                                                       5 * 60_000)]
    st = fresh_store(tmp_path, monkeypatch)
    batch = {"schema": "bbx.wellness.ingest/1", "batch_id": "fx-fall-evening",
             "origins": [FX.WATCH],
             "buckets": [{"metric": "heart_rate", "grain": "5min", "origin": "o1",
                          "replace_day": FALL_DAY, "offset_s": EDT, "rows": rows}],
             "readings": list(readings)}
    ingest_batches(st, [batch], now_ms=end - 30 * 60_000)
    return st, z, i


def test_rows_past_24_hours_say_the_day_is_longer(tmp_path, monkeypatch):
    st, z, i = _fall_day_only(tmp_path, monkeypatch)
    offs = st.offsets()
    assert offs.bounds(FALL_DAY) == (z.ms(i, 0), z.ms(i + 1, 0))
    assert offs.day_of(z.ms(i, 23, 30)) == FALL_DAY            # 23:30 EST is still Nov 1


def test_a_reading_after_the_change_gives_the_next_days_offset(tmp_path, monkeypatch):
    z, i = FX.zone(FALL), FX.DST_VARIANTS[FALL]["dst_day"]
    t = z.ms(i, 7)
    st, z, i = _fall_day_only(tmp_path, monkeypatch, last_hour=False, readings=[
        {"metric": "spo2", "hc_id": "fx-dst-spo2", "origin": "o1", "t": t,
         "offset_s": z.offset_at(t), "v": 96.0}])
    offs = st.offsets()
    assert offs.for_day(FALL_DAY) == EDT and offs.for_day("2026-11-02") == EST
    assert offs.bounds(FALL_DAY)[1] - offs.bounds(FALL_DAY)[0] == 25 * H


# ------------------------------------------------------------------ the tools

def test_hourly_points_show_the_local_time_of_each_hour(tmp_path, monkeypatch):
    _rec, _st, data = enrolled_fixture(tmp_path, monkeypatch, name=SPRING)
    env = api.series("wellness-fixture", "steps", start="2026-03-07", end="2026-03-09",
                     granularity="hour", now_ms=data["now_ms"])
    assert env["ok"], env
    labels = [p["t"] for p in env["points"]]
    assert "2026-03-08T01:00-05:00" in labels and "2026-03-08T03:00-04:00" in labels
    assert not any(t.startswith("2026-03-08T02:") for t in labels)
    assert all(t.endswith("-04:00") for t in labels if t >= "2026-03-08T03")
    assert len(labels) == 24 + 23 + 24


def test_last_night_on_the_change_day(tmp_path, monkeypatch):
    _rec, _st, data = enrolled_fixture(tmp_path, monkeypatch, name=FALL)
    i = FX.DST_VARIANTS[FALL]["dst_day"]
    env = api.summary("wellness-fixture", "custom", start=FALL_DAY, end=FALL_DAY,
                      now_ms=data["now_ms"])
    sleep = next(m for m in env["metrics"] if m["metric"] == "sleep_duration")
    s0, s1, _ = FX._sleep(FALL, i)
    assert sleep["wake_time"] == catalog.clock((s1 + EST * 1000 - store_mod._midnight_ms(FALL_DAY))
                                               / 60_000)
    assert env["period"]["start"].endswith("-04:00") and env["period"]["end"].endswith("-05:00")
