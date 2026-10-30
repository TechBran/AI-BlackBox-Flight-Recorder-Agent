"""Rollups and baselines: the numbers every wellness answer is built from
(spec §3.5). Deterministic: time is injected (`now_ms`), nothing reads the
clock, and a gap is null, never 0.

Per local day D (the rollup table, grain "night" or "day", bucket = D):

  night (the main sleep session ENDING on D, 03:00-14:00 local, so "last
  night" on D is bucket D):
    sleep_duration       minutes in asleep stages (codes 2, 4, 5, 6), or the
                         session length when the source recorded no stages
    sleep_light/deep/rem/awake   minutes per stage
    sleep_onset, sleep_wake      minutes after local midnight of D (onset is
                                 negative before midnight)
    sleeping_hr_median   median of the 5-minute averages inside asleep stages,
    sleeping_hr_p10      and the 10th percentile of their minimums; only on a
                         worn night ("BlackBox estimate from 5-minute averages")
  day:
    steps, distance, active_calories, total_calories   hourly sums (Health
                         Connect's de-duplicated hc_merged origin when present)
    heart_rate           median of the day's 5-minute averages, min, max
    resting_hr           the source's own record ("reported by ..."), else the
                         lowest 30-minute mean during the main sleep
    hrv, spo2, respiratory_rate   median/min/max of the day's readings
    bp_systolic, bp_diastolic     mean/min/max of the day's readings
    exercise             workout minutes (n = sessions)

Baselines ("your usual"): a 28-day median and MAD over the VALID days or
nights before D (worn nights for sleep, days with 10 h of wear for sums),
needing at least 14; below that the median is null with the reason.

Recompute runs only for dirty days, plus the days whose 14-day HR mode they
change and the 28 baselines they feed (a late night recomputes everything it
touches, and nothing else).

Local times (07:00, 03:00-14:00, the bedtime) are wall-clock times read
through the store's Offsets, so a 23- or 25-hour day keeps them.
"""
from __future__ import annotations

import math
import statistics
from typing import Dict, Iterable, List, Optional, Set, Tuple

from Orchestrator.wellness import catalog, coverage as cov
from Orchestrator.wellness.store import HC_MERGED, Offsets, Store, add_days, day_range

BASELINE_WINDOW_DAYS = 28
BASELINE_MIN_DAYS = 14
USUAL_Z = 1.5
MAD_SCALE = 1.4826
RESTING_WINDOW_MIN = 30
RESTING_MIN_COVER = 0.8           # a 30-minute window needs 80 % of its buckets
P10 = 0.10

NIGHT_METRICS = ("sleep_duration", "sleep_light", "sleep_deep", "sleep_rem", "sleep_awake",
                 "sleep_onset", "sleep_wake", "sleeping_hr_median", "sleeping_hr_p10")
SUM_METRICS = ("steps", "distance", "active_calories", "total_calories")
READING_DAY_METRICS = {"hrv": "hrv", "spo2": "spo2", "respiratory_rate": "respiratory_rate"}
DAY_METRICS = SUM_METRICS + ("heart_rate", "resting_hr", "hrv", "spo2", "respiratory_rate",
                             "bp_systolic", "bp_diastolic", "exercise")

# metric -> (grain, which days count): night = worn nights, wear = 10 h days.
BASELINE_METRICS = {
    "sleep_duration": ("night", "night"),
    "sleeping_hr_median": ("night", "night"),
    "sleeping_hr_p10": ("night", "night"),
    "resting_hr": ("day", "any"),
    "heart_rate": ("day", "wear"),
    "steps": ("day", "wear"),
    "distance": ("day", "wear"),
    "active_calories": ("day", "wear"),
    "total_calories": ("day", "wear"),
    "hrv": ("day", "any"),
    "spo2": ("day", "any"),
    "respiratory_rate": ("day", "any"),
    "bp_systolic": ("day", "any"),
    "bp_diastolic": ("day", "any"),
}

STAGE_ROLLUPS = {"sleep_light": {catalog.STAGE_LIGHT}, "sleep_deep": {catalog.STAGE_DEEP},
                 "sleep_rem": {catalog.STAGE_REM},
                 "sleep_awake": {catalog.STAGE_AWAKE, catalog.STAGE_AWAKE_IN_BED}}

METHOD_SLEEP_STAGES = "sum of asleep stages recorded by {source}"
METHOD_SLEEP_SESSION = "length of the sleep session recorded by {source} (no stages)"
METHOD_SLEEPING_HR = "BlackBox estimate from 5-minute averages"
METHOD_RESTING_REPORTED = "reported by {source}"
METHOD_RESTING_ESTIMATE = "BlackBox estimate: lowest 30-minute mean during main sleep"


# ------------------------------------------------------------------ small maths

def median(values: List[float]) -> Optional[float]:
    return float(statistics.median(values)) if values else None


def percentile(values: List[float], q: float) -> Optional[float]:
    """Linear-interpolated percentile (q in 0..1), deterministic."""
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return float(xs[lo])
    return float(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo))


def mad(values: List[float]) -> Optional[float]:
    if not values:
        return None
    m = statistics.median(values)
    return float(statistics.median([abs(v - m) for v in values]))


def _r(x: Optional[float], nd: int = 2) -> Optional[float]:
    return None if x is None else round(float(x), nd)


# ------------------------------------------------------------------ usual range

def _scale(metric: str, base_median: float, base_mad: float) -> float:
    m = catalog.METRICS.get(metric)
    floor = 0.0
    if m is not None:
        if m.floor is not None:
            floor = m.floor
        elif m.floor_pct is not None:
            floor = abs(base_median) * m.floor_pct
    return max(MAD_SCALE * (base_mad or 0.0), floor, 1e-9)


def usual(metric: str, baseline: Optional[dict]) -> Tuple[Optional[dict], Optional[str]]:
    """({median, low, high, n} or None, the reason it is None)."""
    if not baseline or baseline.get("median") is None:
        n = int((baseline or {}).get("n") or 0)
        return None, f"not enough history yet ({min(n, BASELINE_MIN_DAYS)} of " \
                     f"{BASELINE_MIN_DAYS} days)"
    med, spread = float(baseline["median"]), _scale(metric, float(baseline["median"]),
                                                    float(baseline.get("mad") or 0.0))
    return {"median": _r(med, 1), "low": _r(med - USUAL_Z * spread, 1),
            "high": _r(med + USUAL_Z * spread, 1), "n": int(baseline["n"])}, None


def vs_usual(metric: str, value: Optional[float], baseline: Optional[dict]) -> Optional[dict]:
    """{label, delta_display}: within / above / below your usual range. Never
    "normal" or "abnormal"."""
    if value is None or not baseline or baseline.get("median") is None:
        return None
    med = float(baseline["median"])
    z = (value - med) / _scale(metric, med, float(baseline.get("mad") or 0.0))
    if abs(z) < USUAL_Z:
        label = "within your usual range"
    else:
        label = "above your usual range" if z > 0 else "below your usual range"
    delta = value - med
    return {"label": label,
            "delta_display": catalog.delta_display(metric, delta) if abs(delta) > 1e-9 else None}


# ------------------------------------------------------------------ HR mode

def hr_mode_for(conn, store: Store, offsets: Offsets, day: str) -> Optional[str]:
    """The heart-rate mode over the 14 days ending on `day`."""
    first = add_days(day, -(cov.HR_MODE_WINDOW_DAYS - 1))
    start = offsets.bounds(first)[0]
    end = offsets.bounds(day)[1]
    rows = conn.execute(
        "SELECT epoch, ts / 3600000 AS h, SUM(COALESCE(n, 1)) FROM bucket WHERE "
        "metric='heart_rate' AND grain='5min' AND flags=0 AND ts>=? AND ts<? GROUP BY epoch, h",
        (start, end)).fetchall()
    if not rows:
        return None
    best: Dict[str, int] = {}
    keyed = []
    for epoch, h, s in rows:
        d = offsets.day_of(int(h) * 3_600_000)
        keyed.append((d, epoch, s))
        best[d] = max(best.get(d, -1), epoch)
    samples = hours = 0
    for d, epoch, s in keyed:
        if epoch == best[d]:
            samples += s or 0
            hours += 1
    return cov.hr_mode_from_rate(samples / hours if hours else None)


# ------------------------------------------------------------------ one day

def _pick_origin(rows: List[dict]) -> List[dict]:
    """One origin for heart rate: the one with the most buckets (ties: the lowest id)."""
    if not rows:
        return rows
    counts: Dict[int, int] = {}
    for r in rows:
        counts[r["origin_id"]] = counts.get(r["origin_id"], 0) + 1
    chosen = sorted(counts, key=lambda o: (-counts[o], o))[0]
    return [r for r in rows if r["origin_id"] == chosen]


def _source_label(origin: Optional[dict]) -> str:
    if not origin:
        return "your phone"
    label = origin.get("label") or origin.get("package") or "your phone"
    kind = (origin.get("device_type") or "").lower()
    if kind in ("watch", "ring"):
        return f"{label}, {'a watch' if kind == 'watch' else 'a ring'}"
    return label


def _clip(a: int, b: int, lo: int, hi: int) -> int:
    return max(0, min(b, hi) - max(a, lo))


def _main_sleep(conn, store: Store, offsets: Offsets, day: str) -> Optional[dict]:
    lo = offsets.at_local(day, cov.NIGHT_END_EARLIEST_H * 60)
    hi = offsets.at_local(day, cov.NIGHT_END_LATEST_H * 60)
    cands = store.sessions("sleep", lo, hi, conn=conn)
    if not cands:
        return None
    return sorted(cands, key=lambda s: (-(s["end_ts"] - s["start_ts"]), -s["end_ts"]))[0]


def _resting_estimate(buckets: List[dict], mode: Optional[str]) -> Optional[float]:
    if mode not in (cov.MODE_CONTINUOUS, cov.MODE_TEN_MIN) or not buckets:
        return None
    spacing = 5 if mode == cov.MODE_CONTINUOUS else 10
    need = math.ceil(RESTING_MIN_COVER * RESTING_WINDOW_MIN / spacing)
    ts = [b["ts"] for b in buckets]
    best = None
    j = 0
    for i in range(len(buckets)):
        if j < i:
            j = i
        while j < len(buckets) and ts[j] < ts[i] + RESTING_WINDOW_MIN * 60_000:
            j += 1
        window = [b["avg"] for b in buckets[i:j] if b["avg"] is not None]
        if len(window) >= need:
            m = sum(window) / len(window)
            best = m if best is None or m < best else best
    return best


def compute_day(conn, store: Store, offsets: Offsets, day: str, *, now_ms: int,
                mode: Optional[str], origins: Dict[int, dict],
                earliest_available: Optional[str] = None) -> dict:
    """Recompute and store one local day's rollups and coverage row."""
    start, end = offsets.bounds(day)
    ea = earliest_available
    today = start <= now_ms < end
    upto = now_ms if today else None
    for grain in ("night", "day"):
        store.drop_rollups(conn, grain, day)

    hr_all = _pick_origin(store.buckets("heart_rate", "5min", start - 12 * 3_600_000, end,
                                        conn=conn, offsets=offsets))
    hr_1min = store.buckets("heart_rate", "1min", start - 12 * 3_600_000, end, conn=conn,
                            offsets=offsets)

    def put(metric, grain, **kw):
        store.put_rollup(conn, metric, grain, day, computed_at=now_ms, **kw)

    # ---- night -----------------------------------------------------------
    night_worn: Optional[int] = None
    main = _main_sleep(conn, store, offsets, day)
    sleeping_buckets: List[dict] = []
    if main is not None:
        s0, s1 = main["start_ts"], main["end_ts"]
        origin = origins.get(main["origin_id"])
        source = _source_label(origin)
        stages = main["stages"]
        asleep = [(a, b) for a, b, c in stages if c in catalog.ASLEEP_STAGES]
        if stages:
            asleep_ms = sum(_clip(a, b, s0, s1) for a, b in asleep)
            method = METHOD_SLEEP_STAGES.format(source=source)
        else:
            asleep, asleep_ms = [(s0, s1)], s1 - s0
            method = METHOD_SLEEP_SESSION.format(source=source)
        night_ts = [b["ts"] for b in hr_all if s0 <= b["ts"] < s1] + \
                   [b["ts"] for b in hr_1min if s0 <= b["ts"] < s1]
        hit, total = cov.covered_bins(night_ts, s0, s1, cov.night_bin_minutes(mode))
        pct = 100.0 * hit / total if total else None
        night_worn = int(bool(
            cov.is_wearable((origin or {}).get("device_type"))
            and (s1 - s0) >= cov.NIGHT_MIN_H * 3_600_000
            and pct is not None and pct >= cov.NIGHT_WORN_PCT))
        session_cov = cov.coverage(hit, total, cov.night_coverage_label(pct), ea)
        put("sleep_duration", "night", n=1, value=_r(asleep_ms / 60_000.0), coverage=session_cov,
            method=method)
        for metric, codes in STAGE_ROLLUPS.items():
            if stages:
                ms = sum(_clip(a, b, s0, s1) for a, b, c in stages if c in codes)
                put(metric, "night", n=1, value=_r(ms / 60_000.0), coverage=session_cov,
                    method=method)
        put("sleep_onset", "night", n=1, value=_r(offsets.local_minutes(day, s0)),
            coverage=session_cov, method=method)
        put("sleep_wake", "night", n=1, value=_r(offsets.local_minutes(day, s1)),
            coverage=session_cov, method=method)
        if night_worn:
            sleeping_buckets = [b for b in hr_all if b["avg"] is not None
                                and any(a <= b["ts"] < z for a, z in asleep)]
            avgs = [b["avg"] for b in sleeping_buckets]
            mins = [b["min"] if b["min"] is not None else b["avg"] for b in sleeping_buckets]
            if avgs:
                put("sleeping_hr_median", "night", n=len(avgs), value=_r(median(avgs)),
                    median=_r(median(avgs)), p10=_r(percentile(mins, P10)), min=_r(min(mins)),
                    max=_r(max(avgs)), coverage=session_cov, method=METHOD_SLEEPING_HR)
                put("sleeping_hr_p10", "night", n=len(mins), value=_r(percentile(mins, P10)),
                    coverage=session_cov, method=METHOD_SLEEPING_HR)

    # ---- day: heart rate and wear ------------------------------------------
    day_hr = [b for b in hr_all if start <= b["ts"] < end]
    wear_h, hit, total = cov.day_wear_hours(
        [b["ts"] for b in day_hr], start, mode, upto,
        window=(offsets.at_local(day, cov.DAY_WEAR_START_H * 60),
                offsets.at_local(day, cov.DAY_WEAR_END_H * 60)))
    wear_cov = cov.coverage(hit if total else None, total or None,
                            cov.day_wear_label(wear_h, today), ea)
    if day_hr:
        avgs = [b["avg"] for b in day_hr if b["avg"] is not None]
        mins = [b["min"] for b in day_hr if b["min"] is not None] or avgs
        maxs = [b["max"] for b in day_hr if b["max"] is not None] or avgs
        if avgs:
            put("heart_rate", "day", n=len(avgs), value=_r(median(avgs)), median=_r(median(avgs)),
                p10=_r(percentile(avgs, P10)), min=_r(min(mins)), max=_r(max(maxs)),
                coverage=wear_cov, method="median of the day's 5-minute averages")

    # ---- day: activity sums --------------------------------------------------
    for metric in SUM_METRICS:
        rows = store.buckets(metric, "1h", start, end, conn=conn, offsets=offsets)
        if not rows:
            continue
        by_origin: Dict[int, List[dict]] = {}
        for r in rows:
            by_origin.setdefault(r["origin_id"], []).append(r)
        merged = [o for o in by_origin if (origins.get(o) or {}).get("package") == HC_MERGED]
        chosen = merged[0] if merged else sorted(
            by_origin, key=lambda o: (-sum(x["sum"] or 0 for x in by_origin[o]), o))[0]
        picked = [r for r in by_origin[chosen] if r["sum"] is not None]
        if not picked:
            continue
        put(metric, "day", n=len(picked), value=_r(sum(r["sum"] for r in picked)),
            coverage=wear_cov,
            method="Health Connect hourly total" if merged else
            f"hourly total from {_source_label(origins.get(chosen))}")

    # ---- day: readings ------------------------------------------------------
    resting = store.readings("resting_hr", start, end, conn=conn)
    if resting:
        vals = [r["v1"] for r in resting]
        put("resting_hr", "day", n=len(vals), value=_r(median(vals)), min=_r(min(vals)),
            max=_r(max(vals)), coverage=cov.coverage(len(vals), None, "reported", ea),
            method=METHOD_RESTING_REPORTED.format(
                source=_source_label(origins.get(resting[-1]["origin_id"]))))
    elif night_worn:
        est = _resting_estimate(sleeping_buckets or [b for b in hr_all if main and
                                main["start_ts"] <= b["ts"] < main["end_ts"]], mode)
        if est is not None:
            put("resting_hr", "day", n=1, value=_r(est),
                coverage=cov.coverage(1, 1, "from last night's heart rate", ea),
                method=METHOD_RESTING_ESTIMATE)
    for metric, wire in READING_DAY_METRICS.items():
        rows = store.readings(wire, start, end, conn=conn)
        if rows:
            vals = [r["v1"] for r in rows]
            put(metric, "day", n=len(vals), value=_r(median(vals)), median=_r(median(vals)),
                min=_r(min(vals)), max=_r(max(vals)),
                coverage=cov.coverage(len(vals), None, f"{len(vals)} readings", ea),
                method="median of the day's readings")
    bp = store.readings("blood_pressure", start, end, conn=conn)
    if bp:
        for metric, key in (("bp_systolic", "v1"), ("bp_diastolic", "v2")):
            vals = [r[key] for r in bp if r[key] is not None]
            if vals:
                put(metric, "day", n=len(vals), value=_r(sum(vals) / len(vals)),
                    median=_r(median(vals)), min=_r(min(vals)), max=_r(max(vals)),
                    coverage=cov.coverage(len(vals), None, f"{len(vals)} readings", ea),
                    method="mean of the day's readings")

    # ---- day: workouts --------------------------------------------------------
    workouts = store.sessions("exercise", start, end, conn=conn)
    if workouts:
        minutes = sum((w["end_ts"] - w["start_ts"]) / 60_000.0 for w in workouts)
        put("exercise", "day", n=len(workouts), value=_r(minutes),
            coverage=cov.coverage(len(workouts), None, f"{len(workouts)} workouts", ea),
            method="sum of recorded workouts")

    # ---- coverage row -----------------------------------------------------------
    troubles = []
    if mode == cov.MODE_MANUAL:
        troubles.append("hr_mode_manual")
    if main is not None and not night_worn:
        troubles.append("night_not_worn")
    hr_gap = None
    if day_hr:
        n_samples = sum(b["n"] or 1 for b in day_hr)
        hours = len({b["ts"] // 3_600_000 for b in day_hr})
        hr_gap = _r(3600.0 * hours / n_samples, 1) if n_samples else None
    store.put_coverage_day(conn, day, day_wear_h=_r(wear_h, 2), night_worn=night_worn,
                           hr_gap_s=hr_gap, hr_mode=mode,
                           history_capped=int(bool(ea and day < ea)), troubles=troubles)
    return {"day": day, "night_worn": night_worn, "day_wear_h": wear_h, "mode": mode}


# ------------------------------------------------------------------ baselines

def baseline_values(conn, store: Store, metric: str, as_of: str) -> List[float]:
    grain, valid = BASELINE_METRICS[metric]
    first = add_days(as_of, -BASELINE_WINDOW_DAYS)
    last = add_days(as_of, -1)
    rows = store.rollups(grain, first, last, metric=metric, conn=conn)
    if valid == "any":
        return [r["value"] for r in rows if r["value"] is not None]
    cover = store.coverage_days(first, last, conn=conn)
    out = []
    for r in rows:
        c = cover.get(r["bucket"]) or {}
        if r["value"] is None:
            continue
        if valid == "night" and c.get("night_worn") == 1:
            out.append(r["value"])
        elif valid == "wear" and (c.get("day_wear_h") or 0) >= cov.DAY_VALID_WEAR_H:
            out.append(r["value"])
    return out


def recompute_baselines(conn, store: Store, as_of: str) -> None:
    for metric in BASELINE_METRICS:
        vals = baseline_values(conn, store, metric, as_of)
        if len(vals) >= BASELINE_MIN_DAYS:
            store.put_baseline(conn, metric, as_of, BASELINE_WINDOW_DAYS, len(vals),
                               _r(median(vals), 3), _r(mad(vals), 3))
        else:
            store.put_baseline(conn, metric, as_of, BASELINE_WINDOW_DAYS, len(vals), None, None)


# ------------------------------------------------------------------ driver

def recompute(store: Store, dirty_days: Iterable[str], *, now_ms: int,
              earliest_available: Optional[str] = None) -> dict:
    """Recompute the dirty days and everything they feed. Returns counts."""
    from Orchestrator.wellness import digests
    dirty = sorted(set(dirty_days))
    if not dirty:
        return {"days": 0, "baselines": 0}
    with store.write() as conn:
        offsets = store.offsets(conn)
        origins = {r["origin_id"]: dict(r) for r in conn.execute("SELECT * FROM origin")}
        span = store.data_span(conn)
        if span is None:
            for d in dirty:
                for grain in ("night", "day"):
                    store.drop_rollups(conn, grain, d)
                conn.execute("DELETE FROM coverage_day WHERE local_day=?", (d,))
            return {"days": 0, "baselines": 0}
        first, last = span
        done: Set[str] = set()
        modes: Dict[str, Optional[str]] = {}

        def run(d):
            modes[d] = hr_mode_for(conn, store, offsets, d)
            compute_day(conn, store, offsets, d, now_ms=now_ms, mode=modes[d], origins=origins,
                        earliest_available=earliest_available)
            done.add(d)

        for d in dirty:
            if first <= d <= last:
                run(d)
            else:
                for grain in ("night", "day"):
                    store.drop_rollups(conn, grain, d)
                conn.execute("DELETE FROM coverage_day WHERE local_day=?", (d,))
        # A changed day can change the 14-day HR mode of the days after it.
        stored = store.coverage_days(first, last, conn=conn)
        for d in sorted(done):
            for k in range(1, cov.HR_MODE_WINDOW_DAYS):
                nd = add_days(d, k)
                if nd > last:
                    break
                if nd in done:
                    continue
                if hr_mode_for(conn, store, offsets, nd) != (stored.get(nd) or {}).get("hr_mode"):
                    run(nd)
        # Days inside the span with no data still get a coverage row (a gap).
        for d in day_range(first, last):
            if d not in stored and d not in done:
                run(d)
        as_of_days: Set[str] = set()
        for d in done:
            for k in range(1, BASELINE_WINDOW_DAYS + 1):
                ad = add_days(d, k)
                if ad > add_days(last, 1):
                    break
                as_of_days.add(ad)
        for ad in sorted(as_of_days):
            recompute_baselines(conn, store, ad)
        # A week's note compares its days with their baselines, so the weeks
        # of every changed baseline are rewritten too.
        digests.refresh(conn, store, sorted(done | {d for d in as_of_days if d <= last}),
                        now_ms=now_ms)
    return {"days": len(done), "baselines": len(as_of_days)}
