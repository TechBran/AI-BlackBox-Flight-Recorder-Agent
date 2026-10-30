#!/usr/bin/env python3
"""The wellness fixture: 120 deterministic days for the test-only operator
`wellness-fixture`, as bbx.wellness.ingest/1 batches (Orchestrator/wellness/
schema.py documents the batch). Stdlib only: scripts/wellness_sim_phone.py
imports it too.

What the main variant (continuous_120) holds, with day 0 = 2026-10-28 (the
push happens at 12:00 local, UTC-4, so day 0 is a partial day):

  * heart rate in 5-minute buckets while worn (n=5 a bucket: continuous
    mode), plus 1-minute buckets inside the one run (day -2, 18:00-18:45);
  * a main sleep session with stages every night, from the watch;
  * a 4-night rise in sleeping heart rate (wake days -12..-9, +6 bpm);
  * 3 unworn nights in the last week (wake days -6, -4, -1): no watch data,
    and on -4 a phone-only session with no heart rate, so "last 7 days" reads
    "worn 4 of 7 nights";
  * every 9th day the watch is off 12:00-19:30 (under 10 h of wear, so that
    day does not count for step baselines);
  * hourly steps, distance and calories from Health Connect's merged origin;
  * resting HR reported on even days (estimated on the others), SpO2 and
    breathing rate nightly, HRV every third day;
  * blood pressure for the last 21 days, morning and evening, two readings a
    minute apart (the local-only category);
  * the leak-scan SENTINELS: a sleeping HR median of exactly 47.3 on the night
    ending day -5, exactly 12,345 steps on day -3, and 118/79 as the first
    morning blood pressure reading of day -2.

Variants: every_10_min_21 and manual_21 (the other two HR modes), and three
views of the main variant computed from it: days_10 and days_60 (a recent
enrollment) and history_capped_30 (no past-data permission, so Health Connect
returns only 30 days).

Two DST variants, built on demand (never committed), live in US Eastern time
with its 2026 rule instead of a fixed UTC-4: dst_spring_21 (day 0 =
2026-03-18; 2026-03-08, day -10, is a 23-hour day) and dst_fall_21 (day 0 =
2026-11-09; 2026-11-01, day -8, is a 25-hour day, and the night ending on it
spans the change). Every record carries the offset in effect at its own time
and every bucket group the offset at the start of its day, the way the phone
sends them; wall-clock times (07:00, 23:00, the bedtime) are local.

    python Orchestrator/tests/fixtures/wellness/generate_fixtures.py   # rewrite the files

The committed .json.gz files must equal what build() returns (a test checks
the decompressed JSON, since zlib builds differ in their compressed bytes).
"""
from __future__ import annotations

import gzip
import json
import math
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURE_OPERATOR = "wellness-fixture"
SCHEMA = "bbx.wellness.fixture/1"
TODAY = "2026-10-28"
OFFSET_S = -14400
PUSH_HOUR = 12
PLACEHOLDER_ENR = "en_00000000"
BATCH_DAYS = 7
MIN = 60_000

SENTINEL_SLEEPING_HR = 47.3
SENTINEL_STEPS = 12345
SENTINEL_BP = (118.0, 79.0)
SENTINEL_STRINGS = ("47.3", "12345", "12,345", "118/79")
SENTINEL_DAYS = {"sleeping_hr": -5, "steps": -3, "bp": -2}

RISE_NIGHTS = range(-12, -8)       # wake days -12..-9
UNWORN_NIGHTS = (-6, -4, -1)
PHONE_ONLY_NIGHT = -4
RUN_DAY = -2

BASE_VARIANTS = {
    "continuous_120": {"days": 120, "mode": "continuous"},
    "every_10_min_21": {"days": 21, "mode": "every_10_min"},
    "manual_21": {"days": 21, "mode": "manual"},
}
VIEWS = {
    "days_10": {"of": "continuous_120", "days": 10, "history": True},
    "days_60": {"of": "continuous_120", "days": 60, "history": True},
    "history_capped_30": {"of": "continuous_120", "days": 30, "history": False},
}

WATCH = {"key": "o1", "package": "com.sec.android.app.shealth", "label": "Samsung Health",
         "device_type": "WATCH", "manufacturer": "samsung", "model": None,
         "recording_method": None}
PHONE = {"key": "o2", "package": "com.sec.android.app.shealth", "label": "Samsung Health",
         "device_type": "PHONE", "manufacturer": "samsung", "model": "SM-TEST1",
         "recording_method": None}

PERMISSIONS = ["READ_STEPS", "READ_DISTANCE", "READ_TOTAL_CALORIES_BURNED",
               "READ_ACTIVE_CALORIES_BURNED", "READ_EXERCISE", "READ_SLEEP", "READ_HEART_RATE",
               "READ_RESTING_HEART_RATE", "READ_HEART_RATE_VARIABILITY",
               "READ_OXYGEN_SATURATION", "READ_RESPIRATORY_RATE", "READ_BLOOD_PRESSURE"]


# ------------------------------------------------------------------ time

DAY_MS = 86_400_000
EST, EDT = -18000, -14400


def _utc_ms(year: int, month: int, day: int, hour: int) -> int:
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp() * 1000)


class Zone:
    """Local time for a variant: a fixed offset, or a standard offset with a
    summer offset between two instants (the US rule for one year)."""

    def __init__(self, today: str, std: int, summer: int = None, summer_from: int = None,
                 summer_to: int = None):
        self.today, self.std, self.summer = today, std, summer
        self.summer_from, self.summer_to = summer_from, summer_to

    def offset_at(self, t: int) -> int:
        if self.summer is not None and self.summer_from <= t < self.summer_to:
            return self.summer
        return self.std

    def day_of(self, i: int) -> str:
        return (date.fromisoformat(self.today) + timedelta(days=i)).isoformat()

    def ms(self, i: int, hour: float, minute: float = 0) -> int:
        """Unix ms of local `hour:minute` on day i (hour may be < 0 or >= 24).
        A wall time that occurs twice (the fall-back hour) is its first
        occurrence; one that never occurs (the spring-forward hour) reads on
        standard time."""
        d = date.fromisoformat(self.day_of(i))
        utc_midnight = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
        for off in ([self.summer] if self.summer is not None else []) + [self.std]:
            t = int((utc_midnight - off) * 1000 + (hour * 60 + minute) * MIN)
            if self.offset_at(t) == off:
                return t
        return int((utc_midnight - self.std) * 1000 + (hour * 60 + minute) * MIN)

    def now_ms(self) -> int:
        """When the fixture is pushed: 12:00 local on day 0."""
        return self.ms(0, PUSH_HOUR)

    def index(self, t: int) -> int:
        """The day index (relative to day 0) local time t falls on."""
        today = (date.fromisoformat(self.today) - date(1970, 1, 1)).days
        return (t + self.offset_at(t) * 1000) // DAY_MS - today

    def day_offset(self, i: int) -> int:
        """The offset at the start of local day i (what a bucket group sends)."""
        return self.offset_at(self.ms(i, 0))


FIXED = Zone(TODAY, OFFSET_S)
US_EASTERN_SPRING = Zone("2026-03-18", EST, EDT, _utc_ms(2026, 3, 8, 7), _utc_ms(2026, 11, 1, 6))
US_EASTERN_FALL = Zone("2026-11-09", EST, EDT, _utc_ms(2026, 3, 8, 7), _utc_ms(2026, 11, 1, 6))


def day_of(i: int) -> str:
    return FIXED.day_of(i)


def ms(i: int, hour: float, minute: float = 0) -> int:
    """Unix ms of local `hour:minute` on day i of the main variants."""
    return FIXED.ms(i, hour, minute)


def now_ms() -> int:
    """When the main variants are pushed: 12:00 local on day 0."""
    return FIXED.now_ms()


# Built on demand, never committed: the same days in a zone with daylight
# saving time (day -10 of dst_spring_21 is 23 hours long, day -8 of
# dst_fall_21 is 25; the evening before each next night falls in its last
# hour).
DST_VARIANTS = {
    "dst_spring_21": {"days": 21, "mode": "continuous", "zone": US_EASTERN_SPRING,
                      "dst_day": -10},
    "dst_fall_21": {"days": 21, "mode": "continuous", "zone": US_EASTERN_FALL, "dst_day": -8},
}
VARIANTS = tuple(BASE_VARIANTS) + tuple(VIEWS) + tuple(DST_VARIANTS)


def zone(variant: str) -> Zone:
    """The local time a variant (or the variant a view is seeded from) lives in."""
    return DST_VARIANTS[variant]["zone"] if variant in DST_VARIANTS else FIXED


def _iso(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ one day

def _rng(variant: str, i: int, what: str) -> random.Random:
    return random.Random(f"bbx-wellness-fixture:{variant}:{i}:{what}")


def _sleep(variant: str, i: int):
    """(start, end, stages) of the night ending on day i."""
    r = _rng(variant, i, "sleep")
    z = zone(variant)
    start = z.ms(i, -1, r.randint(-40, 40))
    end = z.ms(i, 6, 30 + r.randint(-30, 40))
    pattern = [(4, 30), (5, 25), (4, 15), (6, 20), (4, 25), (1, 5), (6, 25), (5, 15)]
    stages, t, k = [], start, 0
    while t < end:
        code, length = pattern[k % len(pattern)]
        length += r.randint(-4, 4)
        b = min(end, t + length * MIN)
        stages.append([t, b, code])
        t, k = b, k + 1
    return start, end, stages


def _hr_value(r: random.Random, base: float, spread: float) -> list:
    avg = round(base + r.uniform(-spread, spread), 1)
    return [avg, round(avg - 2 - r.random() * 2, 1), round(avg + 2 + r.random() * 3, 1)]


def _keep_bucket(mode: str, t: int) -> bool:
    if mode == "continuous":
        return True
    if mode == "every_10_min":
        return (t // (5 * MIN)) % 2 == 0
    return False      # manual: only the few spot readings below


def _day(variant: str, mode: str, i: int):
    """What day i adds: ({(metric, grain, origin): rows}, sessions, readings).
    The night ending on day i starts the evening before, so some of its heart
    rate falls on day i - 1; _batches() groups every row by its own local day,
    the way the phone aggregates."""
    z = zone(variant)
    ms = z.ms
    day = z.day_of(i)
    last = i == 0
    cut = z.now_ms() if last else ms(i + 1, 0)
    n_per = 5 if mode == "continuous" else 1
    hr, hr1, sessions, readings = [], [], [], []

    # ---- the night that ENDS this morning
    s0, s1, stages = _sleep(variant, i)
    worn = i not in UNWORN_NIGHTS and mode != "manual"
    if i not in UNWORN_NIGHTS:
        sessions.append({"kind": "sleep", "hc_id": f"fx-sleep-{day}", "origin": "o1",
                         "start": s0, "end": s1, "offset_s": z.offset_at(s0), "stages": stages,
                         "last_modified": s1 + 20 * MIN})
    elif i == PHONE_ONLY_NIGHT:
        sessions.append({"kind": "sleep", "hc_id": f"fx-sleep-phone-{day}", "origin": "o2",
                         "start": s0 + 20 * MIN, "end": s1 - 10 * MIN,
                         "offset_s": z.offset_at(s0 + 20 * MIN),
                         "stages": [], "last_modified": s1})
    if worn:
        r = _rng(variant, i, "night-hr")
        rise = 6.0 if i in RISE_NIGHTS else 0.0
        t = (s0 // (5 * MIN)) * 5 * MIN
        while t < s1:
            if t >= ms(i, 0) - 12 * 3_600_000 and _keep_bucket(mode, t):
                awake = any(a <= t < b and c == 1 for a, b, c in stages)
                if i == SENTINEL_DAYS["sleeping_hr"]:
                    vals = [SENTINEL_SLEEPING_HR, 46.0, 49.0]
                else:
                    vals = _hr_value(r, 52.0 + rise + (7.0 if awake else 0.0), 1.6)
                hr.append([t] + vals + [n_per])
            t += 5 * MIN

    # ---- the day: 07:00-23:00 worn (every 9th day off 12:00-19:30)
    r = _rng(variant, i, "day-hr")
    gap = (ms(i, 12), ms(i, 19, 30)) if i % 9 == 0 and i < 0 else None
    run = (ms(i, 18), ms(i, 18, 45)) if i == RUN_DAY else None
    walk = (ms(i, 12, 30), ms(i, 13)) if i % 3 == 1 or i % 3 == -2 else None
    if mode == "manual":
        for h in (9, 13, 19):
            t = ms(i, h, 2)
            if t < cut:
                hr.append([(t // (5 * MIN)) * 5 * MIN] + _hr_value(r, 70.0, 4.0) + [1])
    else:
        t = ms(i, 7)
        while t < min(ms(i, 23), cut):
            if (gap is None or not gap[0] <= t < gap[1]) and _keep_bucket(mode, t):
                hour = (t - ms(i, 0)) / 3_600_000
                base = 68.0 + 6.0 * math.sin((hour - 7) / 16 * math.pi)
                if run and run[0] <= t < run[1]:
                    base = 150.0
                elif walk and walk[0] <= t < walk[1]:
                    base = 96.0
                hr.append([t] + _hr_value(r, base, 2.5) + [n_per])
            t += 5 * MIN
    if run and mode != "manual":
        r1 = _rng(variant, i, "run-1min")
        for k in range(45):
            t = run[0] + k * MIN
            hr1.append([t] + _hr_value(r1, 146.0 + 15.0 * math.sin(k / 45 * math.pi), 3.0) + [1])
        sessions.append({"kind": "exercise", "hc_id": f"fx-run-{day}", "origin": "o1",
                         "start": run[0], "end": run[1], "offset_s": z.offset_at(run[0]),
                         "exercise_type": 56,
                         "attrs": {"distance_m": 7210.0, "active_kcal": 521.0,
                                   "avg_hr": 152.0, "max_hr": 171.0},
                         "last_modified": run[1] + 5 * MIN})
    if walk and walk[1] <= cut:
        sessions.append({"kind": "exercise", "hc_id": f"fx-walk-{day}", "origin": "o1",
                         "start": walk[0], "end": walk[1], "offset_s": z.offset_at(walk[0]),
                         "exercise_type": 79, "attrs": {"distance_m": 2400.0},
                         "last_modified": walk[1] + 5 * MIN})

    # ---- activity, hourly, from Health Connect's merged origin
    r = _rng(variant, i, "steps")
    hours = [h for h in range(7, 23) if ms(i, h) < cut]
    total = SENTINEL_STEPS if i == SENTINEL_DAYS["steps"] else r.randint(5200, 10800)
    if i == RUN_DAY:
        total += 6100
    weights = [1 + r.random() * 2 for _ in range(16)]
    if last:
        total = int(total * len(hours) / 16)
        weights = weights[:len(hours)]
    steps_by_hour = []
    if hours:
        scale = sum(weights)
        parts = [int(total * w / scale) for w in weights]
        parts[-1] += total - sum(parts)
        steps_by_hour = list(zip(hours, parts))
    steps = [[ms(i, h), s] for h, s in steps_by_hour]
    dist = [[ms(i, h), round(s * 0.74, 1)] for h, s in steps_by_hour]
    active = [[ms(i, h), round(s * 0.041, 1)] for h, s in steps_by_hour]
    total_kcal = [[ms(i, h), round(68.0 + s * 0.041, 1)] for h, s in steps_by_hour]

    # ---- readings
    r = _rng(variant, i, "readings")
    if worn:
        t = s1 - 25 * MIN
        if i % 2 == 0:
            readings.append({"metric": "resting_hr", "hc_id": f"fx-rhr-{day}", "origin": "o1",
                             "t": s1 + 30 * MIN, "offset_s": z.offset_at(s1 + 30 * MIN),
                             "v": float(r.randint(54, 60) + (5 if i in RISE_NIGHTS else 0))})
        readings.append({"metric": "spo2", "hc_id": f"fx-spo2-{day}", "origin": "o1", "t": t,
                         "offset_s": z.offset_at(t), "v": float(r.randint(94, 98))})
        readings.append({"metric": "respiratory_rate", "hc_id": f"fx-resp-{day}",
                         "origin": "o1", "t": t, "offset_s": z.offset_at(t),
                         "v": round(13.5 + r.random() * 2, 1)})
        if i % 3 == 0:
            readings.append({"metric": "hrv", "hc_id": f"fx-hrv-{day}", "origin": "o1",
                             "t": t, "offset_s": z.offset_at(t), "v": float(r.randint(32, 52))})
    if i > -21:
        for label, h, m in (("am", 7, 10), ("pm", 21, 0)):
            for k in range(2):
                t = ms(i, h, m + k)
                if t >= cut:
                    continue
                if i == SENTINEL_DAYS["bp"] and label == "am" and k == 0:
                    sys_, dia = SENTINEL_BP
                else:
                    sys_ = float(r.randint(112, 128))
                    dia = float(r.randint(72, 84))
                readings.append({"metric": "blood_pressure", "hc_id": f"fx-bp-{day}-{label}{k}",
                                 "origin": "o2", "t": t, "offset_s": z.offset_at(t),
                                 "v": sys_, "v2": dia,
                                 "attrs": {"measurement_location": 3, "body_position": 2,
                                           "recording_method": 3}})

    series = {("heart_rate", "5min", "o1"): hr, ("heart_rate", "1min", "o1"): hr1,
              ("steps", "1h", "hc_merged"): steps, ("distance", "1h", "hc_merged"): dist,
              ("active_calories", "1h", "hc_merged"): active,
              ("total_calories", "1h", "hc_merged"): total_kcal}
    return series, sessions, readings


# ------------------------------------------------------------------ variants

def _capabilities(history: bool, days: int, mode: str, z: Zone = FIXED) -> dict:
    granted = ["android.permission.health." + p for p in PERMISSIONS]
    granted.append("android.permission.health.READ_HEALTH_DATA_IN_BACKGROUND")
    if history:
        granted.append("android.permission.health.READ_HEALTH_DATA_HISTORY")
    gap = {"continuous": 60, "every_10_min": 600, "manual": 14400}[mode]
    return {
        "phone": {"manufacturer": "samsung", "model": "SM-TEST1", "device_name": "Fixture phone",
                  "sdk_int": 36, "app_version": "fixture"},
        "health_connect": {"sdk_status": "available", "background_feature": "available",
                           "history_feature": "available", "granted": granted},
        "samsung_health": {"installed": True, "version": "6.31"},
        "origins": [
            {"package": WATCH["package"], "label": "Samsung Health", "device_type": "WATCH",
             "model": None, "manufacturer": "samsung", "recording_method": None,
             "metrics": [{"metric": m, "count_14d": c, "last_sample": _iso(z.now_ms() - 30 * MIN),
                          "median_gap_s": g}
                         for m, c, g in (("heart_rate", 14 * 1440 * 60 // gap, gap),
                                         ("sleep", 11, 86400), ("spo2", 11, 86400),
                                         ("exercise", 6, 86400))]},
            {"package": PHONE["package"], "label": "Samsung Health", "device_type": "PHONE",
             "model": "SM-TEST1", "manufacturer": "samsung", "recording_method": None,
             "metrics": [{"metric": "steps", "count_14d": 14 * 16, "last_sample":
                          _iso(z.now_ms() - 30 * MIN), "median_gap_s": 3600},
                         {"metric": "blood_pressure", "count_14d": 56, "last_sample":
                          _iso(z.ms(0, 7, 11)), "median_gap_s": 43200}]},
        ],
        "reported_at": _iso(z.now_ms()),
    }


def _batches(seed: str, name: str, mode: str, days: int) -> list:
    """Backfill pages, newest to oldest, a week at a time (spec §3.2). Every
    bucket row goes to the group of its own local day (replace_day); the
    evening before the first day becomes a small group of its own."""
    z = zone(seed)
    first = -(days - 1)
    series: dict = {}
    sessions_by_day: dict = {}
    readings_by_day: dict = {}
    for i in range(first, 1):
        s, sess, reads = _day(seed, mode, i)
        for key, rows in s.items():
            for row in rows:
                series.setdefault((z.index(row[0]), key), []).append(row)
        for x in sess:
            sessions_by_day.setdefault(z.index(x["end"]), []).append(x)
        for x in reads:
            readings_by_day.setdefault(z.index(x["t"]), []).append(x)
    out, k = [], 0
    for top in range(0, first - 1, -BATCH_DAYS):
        idx = list(range(max(first, top - BATCH_DAYS + 1), top + 1))
        if idx[0] == first:
            idx.insert(0, first - 1)
        buckets, sessions, readings = [], [], []
        for i in idx:
            for key in sorted({kk for (d, kk) in series if d == i}):
                metric, grain, origin = key
                buckets.append({"metric": metric, "grain": grain, "origin": origin,
                                "replace_day": z.day_of(i), "offset_s": z.day_offset(i),
                                "rows": sorted(series[(i, key)])})
            sessions += sessions_by_day.get(i, [])
            readings += readings_by_day.get(i, [])
        out.append({"schema": "bbx.wellness.ingest/1", "batch_id": f"fx-{name}-{k:03d}",
                    "enrollment_id": PLACEHOLDER_ENR, "epoch": 1,
                    "window": {"type": "backfill", "from": _iso(z.ms(idx[0], 0)),
                               "to": _iso(min(z.ms(idx[-1] + 1, 0), z.now_ms()))},
                    "origins": [dict(WATCH), dict(PHONE)], "buckets": buckets,
                    "sessions": sessions, "readings": readings, "deleted_ids": []})
        k += 1
    return out


def _wrap(variant: str, mode: str, days: int, history: bool, batches: list,
          z: Zone = FIXED) -> dict:
    return {"schema": SCHEMA, "variant": variant, "operator": FIXTURE_OPERATOR,
            "today": z.today, "offset_s": z.offset_at(z.now_ms()), "now_ms": z.now_ms(),
            "days": days, "hr_mode": mode, "history": history,
            "capabilities": _capabilities(history, days, mode, z),
            "sentinels": {"sleeping_hr_median": {"day": z.day_of(SENTINEL_DAYS["sleeping_hr"]),
                                                 "value": SENTINEL_SLEEPING_HR},
                          "steps": {"day": z.day_of(SENTINEL_DAYS["steps"]),
                                    "value": SENTINEL_STEPS},
                          "blood_pressure": {"day": z.day_of(SENTINEL_DAYS["bp"]),
                                             "value": list(SENTINEL_BP)}},
            "batches": batches}


def build(name: str) -> dict:
    """A variant, generated. A view is its base variant's last N days: the
    same seeds, so the same values on the days they share."""
    if name in BASE_VARIANTS:
        v = BASE_VARIANTS[name]
        return _wrap(name, v["mode"], v["days"], True,
                     _batches(name, name, v["mode"], v["days"]))
    if name in VIEWS:
        v = VIEWS[name]
        base = BASE_VARIANTS[v["of"]]
        return _wrap(name, base["mode"], v["days"], v["history"],
                     _batches(v["of"], name, base["mode"], v["days"]))
    if name in DST_VARIANTS:
        v = DST_VARIANTS[name]
        out = _wrap(name, v["mode"], v["days"], True, _batches(name, name, v["mode"], v["days"]),
                    v["zone"])
        out["dst_day"] = v["zone"].day_of(v["dst_day"])
        return out
    raise KeyError(f"unknown wellness fixture {name!r}; one of {', '.join(VARIANTS)}")


def committed_path(name: str) -> Path:
    return HERE / f"{name}.json.gz"


def load(name: str) -> dict:
    """A variant: the committed file for a base variant, else built."""
    if name in BASE_VARIANTS and committed_path(name).is_file():
        return json.loads(gzip.decompress(committed_path(name).read_bytes()).decode("utf-8"))
    return build(name)


def for_enrollment(fixture: dict, enrollment_id: str, epoch: int) -> list:
    """The variant's batches addressed to a real enrollment."""
    out = []
    for b in fixture["batches"]:
        nb = dict(b)
        nb["enrollment_id"] = enrollment_id
        nb["epoch"] = epoch
        out.append(nb)
    return out


def encode(fixture: dict) -> bytes:
    text = json.dumps(fixture, separators=(",", ":"), sort_keys=True)
    return gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)


def write_all() -> None:
    for name in BASE_VARIANTS:
        data = encode(build(name))
        committed_path(name).write_bytes(data)
        print(f"wrote {committed_path(name).name} ({len(data) // 1024} KB)")


if __name__ == "__main__":
    write_all()
