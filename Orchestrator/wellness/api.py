"""The deterministic answers behind the wellness_* tools (spec §4.2, with the
§10 "generous once consented" deltas). Wave 2 wraps each in a ToolVault
executor that runs the gate first and passes the categories it allows.

  sources()                      what feeds the store; no health values
  summary(period, categories)    every consented metric for a period, with its
                                 usual range, coverage and the notable sessions
  series(metric, range|start/end, granularity, stat)   up to 365 days, <= 120 points
  sessions(kind, range, exercise_type, limit)          sleep or workouts, newest first
  history(start, end)            the day-by-day rollup table, <= 92 days

Every answer is an envelope: {ok, tool, ref ("w-YYYYMMDD-<4 hex>"), as_of,
stale, paused, sources, troubles, notes, ...}. Every number comes with a
`display` and a `spoken` string and, where one exists, the person's usual
range and the coverage behind it. A gap is null ("not recorded"), never 0.

Size: an answer over its token budget (4 characters a token; 8,000 for cloud
and local servers, 1,500 on-device) is COARSENED (fewer, wider points; weeks
instead of days; detail dropped) and re-measured. It is never truncated, so it
is always whole JSON.

When given a Delivery (the turn key, surface, provider and model), the answer
is written to the operator's `delivered` table: the disclosure log, the
durable mask carry and "Show numbers" all read it back.

Time is injected (`now_ms`); nothing here reads the clock except the default.
"""
from __future__ import annotations

import dataclasses
import functools
import hashlib
import json
import secrets
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from Orchestrator.wellness import catalog, coverage as cov, enroll, rollups, store as store_mod
from Orchestrator.wellness.store import add_days, day_range

CHARS_PER_TOKEN = 4
CAP_TOKENS = {"cloud": 8000, "local": 8000, "on_device": 1500}
MAX_HISTORY_DAYS = 92
MAX_CUSTOM_DAYS = 92
MAX_SERIES_DAYS = 365
MAX_POINTS = 120
MAX_SESSIONS = 20
MAX_COARSEN_STEPS = 40
NOTABLE_SESSIONS = 5

PERIODS = ("today", "last_night", "yesterday", "this_week", "last_week", "last_7_days",
           "last_30_days", "custom")
RANGES = {"last_24_hours": 1, "last_7_days": 7, "last_30_days": 30, "last_90_days": 90,
          "last_365_days": 365}
GRANULARITIES = ("auto", "5min", "hour", "day", "week", "month")
STATS = ("auto", "sum", "mean", "median", "min", "max", "p10", "latest")

SUMMARY_METRICS = {
    "sleep": ("sleep_duration", "sleep_stages"),
    "heart": ("sleeping_hr_median", "sleeping_hr_p10", "resting_hr", "heart_rate", "hrv", "spo2",
              "respiratory_rate"),
    "activity": ("steps", "distance", "active_calories", "total_calories", "exercise"),
    "blood_pressure": ("bp_systolic", "bp_diastolic"),
}
NIGHT_GRAIN = {"sleep_duration", "sleeping_hr_median", "sleeping_hr_p10"}
STAGES = ("sleep_light", "sleep_deep", "sleep_rem", "sleep_awake")

HISTORY_COLUMNS = [
    ("sleep_duration", "night", "sleep", "min"), ("sleep_onset", "night", "sleep", "clock"),
    ("sleep_wake", "night", "sleep", "clock"), ("sleeping_hr_median", "night", "heart", "bpm"),
    ("resting_hr", "day", "heart", "bpm"), ("heart_rate", "day", "heart", "bpm"),
    ("hrv", "day", "heart", "ms"), ("spo2", "day", "heart", "%"),
    ("respiratory_rate", "day", "heart", "breaths/min"), ("steps", "day", "activity", "steps"),
    ("distance", "day", "activity", "m"), ("active_calories", "day", "activity", "kcal"),
    ("exercise", "day", "activity", "min"), ("bp_systolic", "day", "blood_pressure", "mmHg"),
    ("bp_diastolic", "day", "blood_pressure", "mmHg"),
]

NOTE_STAGES = "Sleep stages are device estimates; one night is noisy."
NOTE_SLEEPING_HR = "Sleeping heart rate is a BlackBox estimate from 5-minute averages."
NOTE_PAUSED = "Health collection is paused; these are the stored numbers."
NOTE_COARSENED = "Shown at a coarser resolution to fit; ask for a shorter range for detail."
NOTE_OMITTED = ("Some metrics were left out to fit (metrics_omitted); ask about one of them "
                "for its numbers.")
# When a summary still does not fit once compacted, metrics leave it in this
# order (the least asked-about first); sleep, sleeping heart rate, resting
# heart rate and steps stay longest.
DROP_ORDER = ("total_calories", "distance", "sleeping_hr_p10", "respiratory_rate",
              "active_calories", "heart_rate", "spo2", "hrv", "exercise", "sleep_stages",
              "bp_diastolic", "bp_systolic", "steps", "resting_hr", "sleeping_hr_median",
              "sleep_duration")


class ApiError(Exception):
    def __init__(self, code: str, fix: Optional[str] = None):
        self.code, self.fix = code, fix
        super().__init__(code)


@dataclasses.dataclass(frozen=True)
class Delivery:
    turn_key: Optional[str]
    surface: str
    provider: Optional[str]
    model: Optional[str] = None


@dataclasses.dataclass
class Ctx:
    operator: str
    record: dict
    st: store_mod.Store
    now_ms: int
    offsets: store_mod.Offsets
    categories: Tuple[str, ...]

    @property
    def offset(self) -> int:
        """The zone offset in effect now."""
        return self.offsets.offset_at(self.now_ms)

    @property
    def today(self) -> str:
        return self.offsets.day_of(self.now_ms)

    def iso(self, ms: Optional[int]) -> Optional[str]:
        """A local time with the offset in effect at THAT instant (a point
        from last summer reads on summer time)."""
        return None if ms is None else local_iso(ms, self.offsets.offset_at(ms))


# ------------------------------------------------------------------ helpers

NOT_ENROLLED_FIX = "Health isn't set up. Set it up in Settings, Health, on your phone."


def _erased_reads_as_not_enrolled(tool: str):
    """A store erased while an answer was being read (Stop & erase from the
    phone, an operator delete) reads as not set up, never as an exception."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*a, **kw):
            try:
                return fn(*a, **kw)
            except store_mod.StoreGone:
                return error_envelope(tool, "not_enrolled", NOT_ENROLLED_FIX)
        return inner
    return wrap


def _ctx(operator: str, now_ms: Optional[int], categories: Optional[Iterable[str]]) -> Ctx:
    rec = enroll.get_enrollment(operator)
    st = enroll.store_for(rec, create=False) if rec else None
    if not rec or st is None or rec.get("state") in (None, "awaiting_phone"):
        raise ApiError("not_enrolled", NOT_ENROLLED_FIX)
    consented = [c for c in catalog.CATEGORIES
                 if (rec.get("categories") or {}).get(c) in catalog.CONSENTED]
    cats = consented if categories is None else [c for c in categories if c in consented]
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return Ctx(operator, rec, st, now_ms, st.offsets(), tuple(cats))


def estimate_tokens(obj) -> int:
    return (len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False)) + CHARS_PER_TOKEN - 1) \
        // CHARS_PER_TOKEN


def budget_for(provider: Optional[str], model: Optional[str] = None) -> int:
    from Orchestrator.wellness import destinations
    cls = destinations.classify(provider, model) or "cloud"
    return CAP_TOKENS.get(cls, CAP_TOKENS["cloud"])


def local_iso(ms: Optional[int], offset_s: int) -> Optional[str]:
    if ms is None:
        return None
    tz = timezone(timedelta(seconds=offset_s))
    return datetime.fromtimestamp(ms / 1000, tz=tz).isoformat(timespec="minutes")


def tz_label(offset_s: int) -> str:
    sign = "+" if offset_s >= 0 else "-"
    h, m = divmod(abs(offset_s) // 60, 60)
    return f"UTC{sign}{h:02d}:{m:02d}"


def new_ref(ctx: Ctx) -> str:
    day = ctx.today.replace("-", "")
    for _ in range(20):
        ref = f"w-{day}-{secrets.token_hex(2)}"
        if not ctx.st.ref_exists(ref):
            return ref
    return f"w-{day}-{secrets.token_hex(4)}"


def _round(v, nd=1):
    return None if v is None else round(float(v), nd)


def _base(ctx: Ctx, tool: str) -> dict:
    last = cov.iso_to_ms(ctx.record.get("last_push_at"))
    paused = ctx.record.get("state") == "paused"
    env = {"ok": True, "tool": tool, "ref": None,
           "as_of": ctx.iso(last),
           "stale": cov.is_stale(ctx.record.get("last_push_at"), ctx.now_ms),
           "paused": paused, "tz": tz_label(ctx.offset),
           "troubles": [{"code": t["code"], "title": t["title"], "fix": t["fix"]}
                        for t in enroll.troubles_for(ctx.record, ctx.now_ms)],
           "notes": [NOTE_PAUSED] if paused else []}
    return env


def _sources(ctx: Ctx, start_ms: int, end_ms: int) -> List[dict]:
    origins = ctx.st.origins()
    counts: Dict[int, int] = {}
    with ctx.st.read() as conn:
        for oid, n in conn.execute(
                "SELECT origin_id, COUNT(*) FROM bucket WHERE metric='heart_rate' AND "
                "grain='5min' AND ts>=? AND ts<? GROUP BY origin_id", (start_ms, end_ms)):
            counts[oid] = n
    total = sum(counts.values())
    out = []
    for oid, o in sorted(origins.items()):
        if o.get("package") == store_mod.HC_MERGED:
            continue
        if (o.get("last_seen") or 0) < start_ms - 86_400_000 and oid not in counts:
            continue
        kind = (o.get("device_type") or "unknown").lower()
        app = o.get("label") or o.get("package")
        out.append({"label": f"{'Watch' if kind == 'watch' else kind.capitalize()} via {app}",
                    "device_type": kind, "model": o.get("model"),
                    "share": round(counts.get(oid, 0) / total, 2) if total else None})
    return out


def _period(ctx: Ctx, period: str, start: Optional[str], end: Optional[str]
            ) -> Tuple[str, str, str]:
    """(label, first day, last day) of a summary period, in local days."""
    t = ctx.today
    d = date.fromisoformat(t)
    if period == "today":
        return "today", t, t
    if period == "last_night":
        return "last night", t, t
    if period == "yesterday":
        y = add_days(t, -1)
        return "yesterday", y, y
    if period == "this_week":
        return "this week", (d - timedelta(days=d.weekday())).isoformat(), t
    if period == "last_week":
        mon = d - timedelta(days=d.weekday() + 7)
        return "last week", mon.isoformat(), (mon + timedelta(days=6)).isoformat()
    if period == "last_7_days":
        return "the last 7 days", add_days(t, -6), t
    if period == "last_30_days":
        return "the last 30 days", add_days(t, -29), t
    if period == "custom":
        a, b = _dates(start, end)
        if (date.fromisoformat(b) - date.fromisoformat(a)).days + 1 > MAX_CUSTOM_DAYS:
            raise ApiError("invalid_range", f"A custom period is at most {MAX_CUSTOM_DAYS} days.")
        return f"{a} to {b}", a, b
    raise ApiError("invalid_period", "period is one of " + ", ".join(PERIODS))


def _dates(start: Optional[str], end: Optional[str]) -> Tuple[str, str]:
    try:
        a = date.fromisoformat(str(start)[:10]).isoformat()
        b = date.fromisoformat(str(end)[:10]).isoformat()
    except (TypeError, ValueError):
        raise ApiError("invalid_range", "start and end are dates (YYYY-MM-DD).") from None
    if b < a:
        raise ApiError("invalid_range", "end is before start.")
    return a, b


def _gap_reason(ctx: Ctx, metric: str, day: str, cover: dict) -> str:
    cat = catalog.category_of(metric) or ""
    if f"permission_revoked:{cat}" in (ctx.record.get("phone_troubles") or []):
        return "permission off"
    last = ctx.record.get("last_push_at")
    if not last or day > last[:10]:
        return "not synced yet"
    c = cover.get(day) or {}
    if catalog.METRICS.get(metric) and metric in NIGHT_GRAIN | {"sleep_stages"}:
        if c.get("night_worn") == 0:
            return "watch probably not worn"
        if c.get("night_worn") is None:
            return "no sleep recorded"
    if (c.get("day_wear_h") or 0) == 0 and metric not in ("steps", "distance",
                                                          "active_calories", "total_calories"):
        return "watch probably not worn"
    return "not recorded"


def _entry(metric: str, value, *, coverage=None, method=None, baseline=None, flags=None,
           display=None, spoken=None, **extra) -> dict:
    m = catalog.METRICS[metric]
    usual, reason = rollups.usual(metric, baseline) if metric in rollups.BASELINE_METRICS \
        else (None, None)
    entry = {"metric": metric, "category": m.category, "label": m.label,
             "value": _round(value), "unit": m.unit,
             "display": display if display is not None else (
                 catalog.display(metric, value) if value is not None else "not recorded"),
             "spoken": spoken if spoken is not None else (
                 catalog.spoken(metric, value) if value is not None else "not recorded"),
             "usual": usual,
             "vs_usual": rollups.vs_usual(metric, value, baseline) if value is not None else None,
             "coverage": coverage, "method": method, "flags": flags or []}
    if usual is None and reason and metric in rollups.BASELINE_METRICS:
        entry["usual_reason"] = reason
    entry.update(extra)
    return entry


def _record_delivery(ctx: Ctx, env: dict, tool: str, args: dict, categories,
                     delivery: Optional[Delivery]) -> None:
    if delivery is None:
        return
    ctx.st.record_delivery(env["ref"], delivery.turn_key, delivery.surface, delivery.provider,
                           delivery.model, tool, args, sorted(set(categories)), env,
                           ts=ctx.now_ms)


def _finish(ctx: Ctx, env: dict, tool: str, args: dict, categories, delivery,
            budget_tokens: Optional[int]) -> dict:
    budget = budget_tokens or (budget_for(delivery.provider, delivery.model) if delivery
                               else CAP_TOKENS["cloud"])
    env["ref"] = new_ref(ctx)
    env = coarsen(env, budget)
    _record_delivery(ctx, env, tool, args, categories, delivery)
    return env


def error_envelope(tool: str, code: str, fix: Optional[str] = None) -> dict:
    return {"ok": False, "tool": tool, "error": code, "fix": fix}


# ------------------------------------------------------------------ summary

def _night_values(ctx, days, metric):
    return {r["bucket"]: r for r in ctx.st.rollups("night", days[0], days[-1], metric=metric)}


def _day_values(ctx, days, metric):
    return {r["bucket"]: r for r in ctx.st.rollups("day", days[0], days[-1], metric=metric)}


def _summary_metric(ctx: Ctx, metric: str, days: List[str], cover: Dict[str, dict],
                    single: bool) -> Tuple[Optional[dict], Optional[dict]]:
    """(entry, gap) for one metric over `days`."""
    m = catalog.METRICS[metric]
    grain = "night" if metric in NIGHT_GRAIN or metric == "sleep_stages" else "day"
    first = days[0]
    baseline = ctx.st.baseline(metric, first) if metric in rollups.BASELINE_METRICS else None
    if metric == "sleep_stages":
        rows = {s: _night_values(ctx, days, s) for s in STAGES}
        if single:
            parts = {s.split("_", 1)[1]: (rows[s].get(days[0]) or {}).get("value") for s in STAGES}
            if all(v is None for v in parts.values()):
                return None, None
            disp = ", ".join(f"{k} {catalog.duration_display(v)}" for k, v in parts.items()
                             if v is not None)
            return {"metric": "sleep_stages", "category": "sleep", "label": m.label,
                    "value": None, "unit": "min",
                    "parts": {k: _round(v) for k, v in parts.items()},
                    "display": disp, "spoken": None, "usual": None, "vs_usual": None,
                    "coverage": (rows["sleep_light"].get(days[0]) or {}).get("coverage"),
                    "method": "stages recorded by the device", "flags": []}, None
        return None, None
    rows = (_night_values if grain == "night" else _day_values)(ctx, days, metric)
    if single:
        r = rows.get(days[0])
        if r is None or r["value"] is None:
            return _entry(metric, None, baseline=baseline), \
                {"metric": metric, "day": days[0], "reason": _gap_reason(ctx, metric, days[0],
                                                                        cover)}
        extra = {}
        if metric == "heart_rate":
            extra = {"min": _round(r["min"]), "max": _round(r["max"]),
                     "range_display": f"{catalog.display(metric, r['min'])} to "
                                      f"{catalog.display(metric, r['max'])}"}
        if metric == "exercise":
            extra = {"sessions": r["n"]}
        if m.category == "blood_pressure" or metric in ("spo2", "hrv", "respiratory_rate"):
            extra = {"readings": r["n"], "min": _round(r["min"]), "max": _round(r["max"])}
        if metric == "sleep_duration":
            onset = (ctx.st.rollups("night", days[0], days[0], metric="sleep_onset") or [{}])[0]
            wake = (ctx.st.rollups("night", days[0], days[0], metric="sleep_wake") or [{}])[0]
            extra = {"bedtime": catalog.clock(onset.get("value")),
                     "wake_time": catalog.clock(wake.get("value")),
                     "night_of": add_days(days[0], -1)}
        return _entry(metric, r["value"], coverage=r["coverage"], method=r["method"],
                      baseline=ctx.st.baseline(metric, days[0]) if metric in
                      rollups.BASELINE_METRICS else None, **extra), None
    # A multi-day period: the mean over the valid days, with its coverage.
    valid_kind = rollups.BASELINE_METRICS.get(metric, (grain, "any"))[1]
    used = []
    for d in days:
        r = rows.get(d)
        if r is None or r["value"] is None:
            continue
        c = cover.get(d) or {}
        if valid_kind == "night" and c.get("night_worn") != 1:
            continue
        used.append((d, r["value"]))
    n_exp = len([d for d in days if d <= ctx.today])
    if valid_kind == "night" or grain == "night":
        label = cov.nights_label(len(used), n_exp)
    elif valid_kind == "wear":
        valid = sum(1 for d in days
                    if ((cover.get(d) or {}).get("day_wear_h") or 0) >= cov.DAY_VALID_WEAR_H)
        label = cov.days_label(valid, n_exp)
    else:
        label = f"{len(used)} of {n_exp} days with data"
    coverage = cov.coverage(len(used), n_exp, label,
                            enroll.earliest_available(ctx.record))
    if not used:
        return _entry(metric, None, coverage=coverage, baseline=baseline), \
            {"metric": metric, "day": None, "reason": "not recorded in this period"}
    vals = [v for _, v in used]
    mean = sum(vals) / len(vals)
    extra = {"days_with_data": len(used),
             "lowest": {"day": min(used, key=lambda x: x[1])[0],
                        "display": catalog.display(metric, min(vals))},
             "highest": {"day": max(used, key=lambda x: x[1])[0],
                         "display": catalog.display(metric, max(vals))}}
    if m.stat == "sum":
        total = sum(vals)
        extra["total"] = _round(total)
        extra["total_display"] = catalog.display(metric, total)
    return _entry(metric, mean, coverage=coverage,
                  method=f"average per {'night' if grain == 'night' else 'day'} with data",
                  baseline=baseline, **extra), None


def _notable_sessions(ctx: Ctx, first: str, last: str) -> List[dict]:
    start = ctx.offsets.bounds(first)[0]
    end = ctx.offsets.bounds(last)[1]
    rows = ctx.st.sessions("exercise", start, end)
    rows.sort(key=lambda s: -(s["end_ts"] - s["start_ts"]))
    out = [_session_row(ctx, s, brief=True) for s in rows[:NOTABLE_SESSIONS]]
    return sorted(out, key=lambda s: s["start"])


@_erased_reads_as_not_enrolled("wellness_summary")
def summary(operator: str, period: str, categories: Optional[Iterable[str]] = None, *,
            start: Optional[str] = None, end: Optional[str] = None,
            now_ms: Optional[int] = None, delivery: Optional[Delivery] = None,
            budget_tokens: Optional[int] = None) -> dict:
    tool = "wellness_summary"
    try:
        ctx = _ctx(operator, now_ms, categories)
        label, first, last = _period(ctx, period, start, end)
    except ApiError as e:
        return error_envelope(tool, e.code, e.fix)
    days = day_range(first, last)
    single = first == last
    cover = ctx.st.coverage_days(first, last)
    env = _base(ctx, tool)
    s, e = ctx.offsets.bounds(first)[0], ctx.offsets.bounds(last)[1]
    if period == "last_night":
        s -= 12 * 3_600_000
    env.update({"period": {"name": period, "label": label, "start": ctx.iso(s),
                           "end": ctx.iso(min(e, ctx.now_ms)),
                           "tz": tz_label(ctx.offset), "days": len(days)},
                "sources": _sources(ctx, s, e), "metrics": [], "gaps": []})
    wanted = [c for c in ctx.categories]
    for cat in wanted:
        for metric in SUMMARY_METRICS[cat]:
            if period == "last_night" and metric not in NIGHT_GRAIN | {"sleep_stages",
                                                                       "resting_hr"}:
                continue
            entry, gap = _summary_metric(ctx, metric, days, cover, single)
            if entry is not None:
                env["metrics"].append(entry)
            if gap is not None:
                env["gaps"].append(gap)
    if "activity" in wanted and period != "last_night":
        env["sessions"] = _notable_sessions(ctx, first, last)
    if "sleep" in wanted and single:
        env["notes"].append(NOTE_STAGES)
    if "heart" in wanted:
        env["notes"].append(NOTE_SLEEPING_HR)
    if not any(m.get("value") is not None or m.get("parts") for m in env["metrics"]):
        env["no_data"] = True
    env["categories"] = list(wanted)
    return _finish(ctx, env, tool, {"period": period, "start": start, "end": end}, wanted,
                   delivery, budget_tokens)


# ------------------------------------------------------------------ series

def _window(ctx: Ctx, range_: Optional[str], start, end, max_days: int) -> Tuple[int, int, str]:
    if range_:
        if range_ not in RANGES:
            raise ApiError("invalid_range", "range is one of " + ", ".join(RANGES))
        if range_ == "last_24_hours":
            return ctx.now_ms - 86_400_000, ctx.now_ms, "the last 24 hours"
        n = RANGES[range_]
        return ctx.offsets.bounds(add_days(ctx.today, -(n - 1)))[0], ctx.now_ms, \
            f"the last {n} days"
    a, b = _dates(start, end)
    if (date.fromisoformat(b) - date.fromisoformat(a)).days + 1 > max_days:
        raise ApiError("invalid_range", f"At most {max_days} days per call.")
    return ctx.offsets.bounds(a)[0], min(ctx.offsets.bounds(b)[1], ctx.now_ms), f"{a} to {b}"


_STEP_MS = {"5min": 300_000, "hour": 3_600_000}


def _auto_granularity(metric: str, span_ms: int) -> str:
    hours = span_ms / 3_600_000
    if metric == "heart_rate" and hours <= 3:
        return "5min"
    if hours <= 48:
        return "hour"
    if hours <= 62 * 24:
        return "day"
    if hours <= 200 * 24:
        return "week"
    return "month"


def _agg(vals: List[float], stat: str, metric: str) -> Optional[float]:
    if not vals:
        return None
    if stat == "auto":
        stat = "sum" if catalog.METRICS[metric].stat == "sum" else "mean"
    if stat == "sum":
        return sum(vals)
    if stat == "median":
        return float(statistics.median(vals))
    if stat == "min":
        return min(vals)
    if stat == "max":
        return max(vals)
    if stat == "p10":
        return rollups.percentile(vals, 0.10)
    if stat == "latest":
        return vals[-1]
    return sum(vals) / len(vals)


def _period_key(day: str, gran: str) -> str:
    d = date.fromisoformat(day)
    if gran == "week":
        return (d - timedelta(days=d.weekday())).isoformat()
    if gran == "month":
        return d.replace(day=1).isoformat()
    return day


@_erased_reads_as_not_enrolled("wellness_series")
def series(operator: str, metric: str, range: Optional[str] = None, *,  # noqa: A002
           start: Optional[str] = None, end: Optional[str] = None, granularity: str = "auto",
           stat: str = "auto", categories: Optional[Iterable[str]] = None,
           now_ms: Optional[int] = None, delivery: Optional[Delivery] = None,
           budget_tokens: Optional[int] = None) -> dict:
    tool = "wellness_series"
    m = catalog.METRICS.get(metric)
    if m is None or metric == "sleep_stages":
        return error_envelope(tool, "unknown_metric", "metric is one of " +
                              ", ".join(k for k in catalog.METRICS if k != "sleep_stages"))
    if granularity not in GRANULARITIES or stat not in STATS:
        return error_envelope(tool, "invalid_argument",
                              "granularity: " + ", ".join(GRANULARITIES) + "; stat: " +
                              ", ".join(STATS))
    try:
        ctx = _ctx(operator, now_ms, categories)
        if m.category not in ctx.categories:
            raise ApiError("category_off", "Turn it on in Settings, Health, on your phone.")
        s, e, label = _window(ctx, range, start, end, MAX_SERIES_DAYS)
    except ApiError as err:
        return error_envelope(tool, err.code, err.fix)
    gran = _auto_granularity(metric, e - s) if granularity == "auto" else granularity
    if gran == "5min" and (metric != "heart_rate" or e - s > 3 * 3_600_000):
        gran = "hour"
    points = []
    if gran in _STEP_MS:
        step = _STEP_MS[gran]
        if metric == "heart_rate":
            col = {"min": "min", "p10": "min", "max": "max"}.get(stat, "avg")
            vals = [(r["ts"], r[col] if r[col] is not None else r["avg"])
                    for r in ctx.st.buckets("heart_rate", "5min", s, e)]
        elif metric in ("steps", "distance", "active_calories", "total_calories"):
            vals = [(r["ts"], r["sum"]) for r in ctx.st.buckets(metric, "1h", s, e)]
        else:
            wire = {"bp_systolic": "blood_pressure", "bp_diastolic": "blood_pressure"}.get(
                metric, metric)
            key = "v2" if metric == "bp_diastolic" else "v1"
            vals = [(r["ts"], r[key]) for r in ctx.st.readings(wire, s, e)]
        slots: Dict[int, List[float]] = {}
        for t, v in vals:
            if v is not None:
                slots.setdefault(s + (t - s) // step * step, []).append(v)
        t = s
        while t < e:
            got = slots.get(t, [])
            value = _agg(got, stat, metric)
            points.append({"t": ctx.iso(t), "value": _round(value),
                           "display": catalog.display(metric, value) if value is not None
                           else None,
                           "coverage_pct": 100 if got else 0})
            t += step
    else:
        grain = "night" if metric in NIGHT_GRAIN else "day"
        first, last = ctx.offsets.day_of(s), ctx.offsets.day_of(max(s, e - 1))
        rows = {r["bucket"]: r for r in ctx.st.rollups(grain, first, last, metric=metric)}
        groups: Dict[str, List[Tuple[str, float]]] = {}
        expected: Dict[str, int] = {}
        for d in day_range(first, last):
            key = _period_key(d, gran)
            expected[key] = expected.get(key, 0) + 1
            r = rows.get(d)
            if r is not None and r["value"] is not None:
                groups.setdefault(key, []).append((d, r["value"]))
        for key in sorted(expected):
            got = [v for _, v in groups.get(key, [])]
            if gran == "day":
                value = got[0] if got else None
            elif stat == "auto":
                # A week or month point is the mean of its days with data (for
                # steps: the daily average, not a total over a partial week).
                value = sum(got) / len(got) if got else None
            else:
                value = _agg(got, stat, metric)
            points.append({"t": key, "value": _round(value),
                           "display": catalog.display(metric, value) if value is not None
                           else None,
                           "coverage_pct": int(round(100 * len(got) / expected[key]))})
    while len(points) > MAX_POINTS:
        points = _merge_points(points, metric, stat)
        gran = gran + "+" if not gran.endswith("+") else gran
    env = _base(ctx, tool)
    env.update({"metric": metric, "unit": m.unit, "category": m.category, "window": label,
                "granularity_used": gran.rstrip("+"), "stat": stat, "points": points,
                "baseline": rollups.usual(metric, ctx.st.baseline(metric, ctx.today))[0]
                if metric in rollups.BASELINE_METRICS else None})
    if not any(p["value"] is not None for p in points):
        env["no_data"] = True
    return _finish(ctx, env, tool, {"metric": metric, "range": range, "start": start,
                                    "end": end, "granularity": granularity, "stat": stat},
                   [m.category], delivery, budget_tokens)


def _merge_points(points: List[dict], metric: str, stat: str) -> List[dict]:
    """Halve the resolution: each pair of points becomes one (never a cut)."""
    out = []
    for i in range(0, len(points), 2):
        pair = points[i:i + 2]
        vals = [p["value"] for p in pair if p["value"] is not None]
        if catalog.METRICS[metric].stat == "sum" and stat in ("auto", "sum"):
            value = sum(vals) if vals else None
        else:
            value = sum(vals) / len(vals) if vals else None
        out.append({"t": pair[0]["t"], "value": _round(value),
                    "display": catalog.display(metric, value) if value is not None else None,
                    "coverage_pct": int(round(sum(p["coverage_pct"] for p in pair) / len(pair)))})
    return out


# ------------------------------------------------------------------ sessions

def _session_ref(ctx: Ctx, s: dict) -> str:
    h = hashlib.sha256(f"{ctx.st.op_key}:{s['epoch']}:{s['hc_id']}".encode()).hexdigest()[:8]
    return f"s-{ctx.offsets.day_of(s['end_ts']).replace('-', '')}-{h}"


def _session_row(ctx: Ctx, s: dict, brief: bool = False) -> dict:
    origins = ctx.st.origins()
    hr = [b for b in ctx.st.buckets("heart_rate", "1min" if s["kind"] == "exercise" else "5min",
                                    s["start_ts"], s["end_ts"]) if b["avg"] is not None]
    if not hr and s["kind"] == "exercise":
        hr = [b for b in ctx.st.buckets("heart_rate", "5min", s["start_ts"], s["end_ts"])
              if b["avg"] is not None]
    minutes = (s["end_ts"] - s["start_ts"]) / 60_000
    attrs = s.get("attrs") or {}
    step = 60_000 if s["kind"] == "exercise" and hr and hr[0]["grain"] == "1min" else 300_000
    expected = max(1, int(minutes * 60_000 // step))
    avg_hr = attrs.get("avg_hr") or (sum(b["avg"] for b in hr) / len(hr) if hr else None)
    max_hr = attrs.get("max_hr") or (max((b["max"] or b["avg"]) for b in hr) if hr else None)
    o = origins.get(s["origin_id"]) or {}
    row = {"session_ref": _session_ref(ctx, s), "kind": s["kind"],
           "type": attrs.get("type") or ("sleep" if s["kind"] == "sleep" else "other"),
           "start": ctx.iso(s["start_ts"]), "end": ctx.iso(s["end_ts"]),
           "duration_min": _round(minutes), "duration": catalog.duration_display(minutes),
           "avg_hr": _round(avg_hr), "avg_hr_display": catalog.display("heart_rate", avg_hr)
           if avg_hr is not None else None,
           "max_hr": _round(max_hr), "hr_coverage_pct": min(100, int(round(100 * len(hr) /
                                                                         expected))),
           "source": f"{(o.get('device_type') or 'unknown').lower()} via "
                     f"{o.get('label') or o.get('package') or 'unknown'}"}
    if attrs.get("distance_m") is not None:
        row["distance_m"] = _round(attrs["distance_m"])
        row["distance"] = catalog.display("distance", attrs["distance_m"])
    kcal = attrs.get("active_kcal") or attrs.get("kcal") or attrs.get("total_kcal")
    if kcal is not None:
        row["calories"] = catalog.display("active_calories", kcal)
    if s["kind"] == "sleep" and not brief:
        mins: Dict[str, float] = {}
        for a, b, code in s.get("stages") or ():
            name = catalog.STAGE_NAMES.get(code)
            if name:
                mins[name] = mins.get(name, 0.0) + (b - a) / 60_000
        if mins:
            row["stages_min"] = {k: _round(v) for k, v in sorted(mins.items())}
    return row


@_erased_reads_as_not_enrolled("wellness_sessions")
def sessions(operator: str, kind: str, range: Optional[str] = None, *,  # noqa: A002
             start: Optional[str] = None, end: Optional[str] = None,
             exercise_type: Optional[str] = None, limit: int = 10,
             categories: Optional[Iterable[str]] = None, now_ms: Optional[int] = None,
             delivery: Optional[Delivery] = None, budget_tokens: Optional[int] = None) -> dict:
    tool = "wellness_sessions"
    if kind not in ("sleep", "exercise"):
        return error_envelope(tool, "invalid_argument", "kind is sleep or exercise")
    if exercise_type is not None and exercise_type not in catalog.EXERCISE_TYPE_NAMES:
        return error_envelope(tool, "invalid_argument",
                              "exercise_type is one of " + ", ".join(catalog.EXERCISE_TYPE_NAMES))
    limit = max(1, min(MAX_SESSIONS, int(limit or 10)))
    cat = "sleep" if kind == "sleep" else "activity"
    try:
        ctx = _ctx(operator, now_ms, categories)
        if cat not in ctx.categories:
            raise ApiError("category_off", "Turn it on in Settings, Health, on your phone.")
        s, e, label = _window(ctx, range or ("last_30_days" if not start else None), start, end,
                              MAX_SERIES_DAYS)
    except ApiError as err:
        return error_envelope(tool, err.code, err.fix)
    rows = ctx.st.sessions(kind, s, e)
    if exercise_type:
        rows = [r for r in rows if (r.get("attrs") or {}).get("type") == exercise_type]
    rows.sort(key=lambda r: -r["end_ts"])
    env = _base(ctx, tool)
    env.update({"kind": kind, "window": label, "total_in_window": len(rows),
                "sessions": [_session_row(ctx, r) for r in rows[:limit]]})
    if not rows:
        env["no_data"] = True
    return _finish(ctx, env, tool, {"kind": kind, "range": range, "start": start, "end": end,
                                    "exercise_type": exercise_type, "limit": limit},
                   [cat], delivery, budget_tokens)


# ------------------------------------------------------------------ history

@_erased_reads_as_not_enrolled("wellness_history")
def history(operator: str, start: str, end: str, *, categories: Optional[Iterable[str]] = None,
            now_ms: Optional[int] = None, delivery: Optional[Delivery] = None,
            budget_tokens: Optional[int] = None) -> dict:
    tool = "wellness_history"
    try:
        ctx = _ctx(operator, now_ms, categories)
        a, b = _dates(start, end)
        if (date.fromisoformat(b) - date.fromisoformat(a)).days + 1 > MAX_HISTORY_DAYS:
            raise ApiError("invalid_range", f"At most {MAX_HISTORY_DAYS} days per call.")
    except ApiError as err:
        return error_envelope(tool, err.code, err.fix)
    cols = [c for c in HISTORY_COLUMNS if c[2] in ctx.categories]
    data = {}
    for metric, grain, _cat, _unit in cols:
        data[metric] = {r["bucket"]: r["value"] for r in ctx.st.rollups(grain, a, b, metric=metric)}
    cover = ctx.st.coverage_days(a, b)
    columns = ["day"] + [c[0] for c in cols] + ["night_worn", "day_wear_h"]
    rows = []
    for d in day_range(a, b):
        row = [d]
        for metric, _grain, _cat, unit in cols:
            v = data[metric].get(d)
            row.append(catalog.clock(v) if unit == "clock" and v is not None else _round(v))
        c = cover.get(d) or {}
        row += [c.get("night_worn"), _round(c.get("day_wear_h"))]
        rows.append(row)
    env = _base(ctx, tool)
    env.update({"start": a, "end": b, "resolution": "day", "columns": columns,
                "units": {c[0]: c[3] for c in cols}, "rows": rows,
                "baselines": {c[0]: rollups.usual(c[0], ctx.st.baseline(c[0], b))[0]
                              for c in cols if c[0] in rollups.BASELINE_METRICS},
                "legend": "null = not recorded; night_worn 1/0; day_wear_h = hours of watch wear "
                          "07:00-23:00"})
    return _finish(ctx, env, tool, {"start": a, "end": b}, [c[2] for c in cols], delivery,
                   budget_tokens)


# ------------------------------------------------------------------ sources

@_erased_reads_as_not_enrolled("wellness_sources")
def sources(operator: str, *, now_ms: Optional[int] = None, delivery: Optional[Delivery] = None,
            budget_tokens: Optional[int] = None) -> dict:
    tool = "wellness_sources"
    try:
        ctx = _ctx(operator, now_ms, None)
    except ApiError as err:
        return error_envelope(tool, err.code, err.fix)
    report = ctx.st.meta_get("capabilities") or {}
    hc = report.get("health_connect") or {}
    granted = {g.split(".")[-1] for g in hc.get("granted") or [] if isinstance(g, str)}
    cover = ctx.st.coverage_days(add_days(ctx.today, -27), ctx.today)
    metrics = []
    consent = ctx.record.get("categories") or {}
    for origin in report.get("origins") or []:
        for mrow in origin.get("metrics") or []:
            metrics.append({"metric": mrow.get("metric"), "app": origin.get("label"),
                            "device_type": (origin.get("device_type") or "").lower() or None,
                            "model": origin.get("model"), "latest": mrow.get("last_sample"),
                            "count_14d": mrow.get("count_14d")})
    per_category = []
    for cat in catalog.CATEGORIES:
        days = 0
        for d in cover.values():
            if cat == "sleep" and d.get("night_worn") is not None:
                days += 1
            elif cat in ("heart", "activity") and (d.get("day_wear_h") or 0) > 0:
                days += 1
        per_category.append({"category": cat, "consent": consent.get(cat, "off"),
                             "local_only": bool((ctx.record.get("local_only") or {}).get(cat)),
                             "days_with_data_28": days if cat != "blood_pressure" else None})
    last7 = [cover.get(add_days(ctx.today, -k), {}).get("night_worn") for k in range(7)]
    env = _base(ctx, tool)
    env.update({
        "phone": {"label": ctx.record.get("source_label"),
                  "last_push": ctx.iso(cov.iso_to_ms(ctx.record.get("last_push_at")))},
        "platforms": {"health_connect": {"status": hc.get("sdk_status"),
                                         "background_access": catalog.PERMISSION_BACKGROUND
                                         in granted,
                                         "past_data_access": catalog.PERMISSION_HISTORY in granted,
                                         "earliest_available": enroll.earliest_available(
                                             ctx.record)},
                      "samsung_health": report.get("samsung_health")},
        "origins": metrics,
        "categories": per_category,
        "wear": {"hr_mode": (cover.get(ctx.today) or cover.get(add_days(ctx.today, -1)) or {}
                             ).get("hr_mode"),
                 "nights_worn_7": sum(1 for n in last7 if n == 1)},
        "sync_mode": "background" if catalog.PERMISSION_BACKGROUND in granted
        else "when BlackBox is open",
        "providers_allowed": ctx.record.get("providers_allowed") or [],
        "samsung_only": "Sleep score, Energy Score, stress and ECG stay in Samsung Health; "
                        "BlackBox can't read them yet."})
    return _finish(ctx, env, tool, {}, [], delivery, budget_tokens)


# ------------------------------------------------------------------ coarsening

def _coarsen_history(env: dict) -> bool:
    if env.get("resolution") == "month":
        return False
    to = "week" if env.get("resolution") == "day" else "month"
    cols = env["columns"]
    groups: Dict[str, List[list]] = {}
    for row in env["rows"]:
        groups.setdefault(_period_key(row[0], to), []).append(row)
    out = []
    for key in sorted(groups):
        rows = groups[key]
        merged = [key]
        for i in range(1, len(cols)):
            vals = [r[i] for r in rows if isinstance(r[i], (int, float))]
            if cols[i] in ("sleep_onset", "sleep_wake"):
                merged.append(None)
            elif cols[i] == "night_worn":
                merged.append(sum(vals))
            else:
                merged.append(_round(sum(vals) / len(vals)) if vals else None)
        out.append(merged)
    env["rows"] = out
    env["resolution"] = to
    env["legend"] = (f"one row per {to}: means of the days with data; night_worn = worn nights "
                     "in the period; null = not recorded")
    return True


def _drop_detail(env: dict) -> bool:
    changed = False
    for m in env.get("metrics") or []:
        for k in ("spoken", "method", "lowest", "highest", "usual_reason", "range_display"):
            if k in m:
                m.pop(k)
                changed = True
    for s in env.get("sessions") or []:
        for k in ("stages_min", "source", "avg_hr_display", "calories"):
            if k in s:
                s.pop(k)
                changed = True
    if env.get("sources"):
        env["sources"] = []
        changed = True
    return changed


def _compact_metrics(env: dict) -> bool:
    """A summary's metrics in their shortest whole form: the value and its
    display, the usual range, how it compares and the coverage (pct and
    label) stay; counts that the coverage already says, and labels the
    metric id already names, go."""
    changed = False
    for m in env.get("metrics") or []:
        if m.get("compact"):
            continue
        for k in ("label", "unit", "days_with_data", "total", "readings", "sessions",
                  "min", "max", "method"):
            m.pop(k, None)
        if not m.get("flags"):
            m.pop("flags", None)
        usual = m.get("usual")
        if isinstance(usual, dict):
            m["usual"] = {k: usual[k] for k in ("low", "high") if k in usual}
        vs = m.get("vs_usual")
        if isinstance(vs, dict):
            m["vs_usual"] = vs.get("label")
        c = m.get("coverage")
        if isinstance(c, dict):
            m["coverage"] = {k: c[k] for k in ("pct", "label") if k in c}
        m["compact"] = True
        changed = True
    if env.get("gaps"):
        env["gaps_count"] = len(env["gaps"])
        env["gaps"] = []
        changed = True
    return changed


def _drop_one_metric(env: dict) -> bool:
    """Leave out the next metric in DROP_ORDER, saying so (never silently)."""
    metrics = env.get("metrics") or []
    if len(metrics) <= 1:
        return False
    rank = {m: i for i, m in enumerate(DROP_ORDER)}
    victim = min(metrics, key=lambda m: rank.get(m.get("metric"), -1))
    metrics.remove(victim)
    env.setdefault("metrics_omitted", []).append(victim.get("metric"))
    if NOTE_OMITTED not in env.setdefault("notes", []):
        env["notes"].append(NOTE_OMITTED)
    return True


def coarsen(env: dict, token_budget: int) -> dict:
    """Fit `env` into `token_budget` by reducing resolution, never by cutting
    the JSON. Each step keeps the answer whole and says it was coarsened; a
    summary that still does not fit leaves out its least-asked metrics by
    name. If nothing more can go, the answer says over_budget."""
    if estimate_tokens(env) <= token_budget:
        return env
    env = json.loads(json.dumps(env))
    for _ in range(MAX_COARSEN_STEPS):
        if estimate_tokens(env) <= token_budget:
            break
        if env.get("points") and len(env["points"]) > 1:
            env["points"] = _merge_points(env["points"], env["metric"], env.get("stat", "auto"))
            env["granularity_used"] = "coarsened"
        elif env.get("rows") and _coarsen_history(env):
            pass
        elif _drop_detail(env):
            pass
        elif env.get("sessions") and len(env["sessions"]) > 1:
            env["sessions"] = env["sessions"][: max(1, len(env["sessions"]) // 2)]
            env["sessions_shown"] = len(env["sessions"])
        elif _compact_metrics(env):
            pass
        elif _drop_one_metric(env):
            pass
        else:
            break
        if NOTE_COARSENED not in env.setdefault("notes", []):
            env["notes"].append(NOTE_COARSENED)
        env["coarsened"] = True
    if estimate_tokens(env) > token_budget:
        env["over_budget"] = True
    return env
