"""Wear, heart-rate mode, the coverage object and the trouble states (spec §3.5).

Health Connect has no wear record, so wear is INFERRED from heart-rate
density, never observed: a covered bin is a bin that holds heart rate. Every
threshold is a named constant here and is provisional until the M7.1 data.
The literature is consistent that coverage moves step totals a lot and median
HR very little, so sums need stricter coverage than medians.

Nothing here reads the clock: `now_ms` is always passed in.

A coverage object, carried by every derived number:

    {"n_used": 4, "n_expected": 7, "pct": 57, "label": "worn 4 of 7 nights",
     "earliest_available": "2026-09-01"}

Gaps are null, never 0. Trouble states are never alarms and never alert
triggers; each has fixed wording and a fix.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from Orchestrator.wellness import catalog

# Heart-rate mode, from sample density over 14 days (samples per covered hour).
HR_MODE_WINDOW_DAYS = 14
HR_CONTINUOUS_MIN_PER_H = 30.0     # ~5 samples per 5-minute bucket (one a minute) = 60/h
HR_TEN_MIN_MIN_PER_H = 2.0         # ~0.5 per bucket (one every 10 minutes) = 6/h; below: manual
MODE_CONTINUOUS, MODE_TEN_MIN, MODE_MANUAL = "continuous", "every_10_min", "manual"

# Day wear: 07:00-23:00 local, in 15-minute bins (30 in 10-minute mode).
DAY_WEAR_START_H = 7
DAY_WEAR_END_H = 23
DAY_BIN_MIN = 15
DAY_BIN_MIN_TEN_MODE = 30
DAY_VALID_WEAR_H = 10.0            # a day is valid for sums at 10 h of wear or more

# Night worn: the main sleep session, ending 03:00-14:00 local, at least 3 h,
# with 60 % or more of its bins holding heart rate.
NIGHT_END_EARLIEST_H = 3
NIGHT_END_LATEST_H = 14
NIGHT_MIN_H = 3.0
NIGHT_WORN_PCT = 60.0
NIGHT_BIN_MIN = 5
NIGHT_BIN_MIN_TEN_MODE = 10
WORN_ALL_NIGHT_PCT = 95.0

# Enrollment-level troubles.
STALE_SYNC_H = 6.0                 # no push for 6 h while active
NOT_WORN_LOOKBACK_NIGHTS = 3       # of the last 3 nights ...
NOT_WORN_MISSING_NIGHTS = 2        # ... 2 or more not worn while worn before

TROUBLE_TEXT = {
    "stale_sync": ("Health data hasn't synced recently",
                   "Your phone hasn't sent health data since {last_sync}.",
                   "Open BlackBox on your phone"),
    "not_worn_likely": ("Watch probably not worn",
                        "There is no heart rate for most of the recent nights, so the watch "
                        "was probably not worn.",
                        "Wear your watch to bed"),
    "hr_mode_manual": ("Heart rate is measured manually",
                       "Sleeping heart rate needs continuous or 10-minute measurement on "
                       "your watch.",
                       "Open Samsung Health, then heart rate settings"),
    "history_window_capped": ("Only recent history",
                              "BlackBox can see only the last 30 days of Health Connect data.",
                              "Allow past data in Health Connect"),
    "permission_revoked": ("Health Connect access turned off",
                           "BlackBox can no longer read {category} in Health Connect.",
                           "Open Health Connect permissions"),
    "foreground_only": ("Syncs when you open BlackBox",
                        "Background access is off, so health data syncs only while BlackBox "
                        "is open.",
                        "Allow background access in Health Connect"),
    "battery_restricted": ("Battery optimisation may delay syncing",
                           "Android may hold back BlackBox's hourly sync.",
                           "Open battery settings"),
    "source_unreachable": ("Phone not reachable",
                           "The box could not reach your phone over Tailscale.",
                           "Check that Tailscale is on, on your phone"),
    "ownership_changed": ("Phone assigned to someone else",
                          "This phone was assigned to another person in Devices, so health "
                          "collection stopped.",
                          "Set up Health again from your own phone"),
}


def trouble(code: str, **fmt) -> dict:
    """A trouble state with its fixed wording: {code, title, body, fix}."""
    base = code.split(":", 1)[0]
    title, body, fix = TROUBLE_TEXT[base]
    if base == "permission_revoked" and "category" not in fmt:
        cat = code.split(":", 1)[1] if ":" in code else ""
        fmt["category"] = catalog.CATEGORY_LABELS.get(cat, cat).lower()
    try:
        body = body.format(**fmt)
    except (KeyError, IndexError):
        body = body.replace("{last_sync}", "a while").replace("{category}", "this data")
    return {"code": code, "title": title, "body": body, "fix": fix}


# ------------------------------------------------------------------ coverage objects

def coverage(n_used: Optional[int], n_expected: Optional[int], label: str,
             earliest_available: Optional[str] = None) -> dict:
    pct = None
    if n_used is not None and n_expected:
        pct = int(round(100.0 * n_used / n_expected))
    return {"n_used": n_used, "n_expected": n_expected, "pct": pct, "label": label,
            "earliest_available": earliest_available}


def nights_label(worn: int, expected: int) -> str:
    if expected == 1:
        return "worn all night" if worn else "not worn"
    if worn == expected:
        return f"worn all {expected} nights"
    return f"worn {worn} of {expected} nights"


def days_label(valid: int, expected: int) -> str:
    if expected == 1:
        return "full day of wear" if valid else "watch worn less than 10 h"
    return f"{valid} of {expected} days with 10 h of wear"


def night_coverage_label(pct: Optional[float]) -> str:
    if pct is None:
        return "no sleep recorded"
    if pct >= WORN_ALL_NIGHT_PCT:
        return "worn all night"
    if pct >= NIGHT_WORN_PCT:
        return f"heart rate on {int(round(pct))}% of the night"
    if pct <= 0:
        return "not worn"
    return f"heart rate on only {int(round(pct))}% of the night"


def day_wear_label(hours: Optional[float], today: bool = False) -> str:
    if hours is None:
        return "no heart rate recorded"
    h = int(round(hours))
    return f"watch worn about {h} h so far today" if today else f"watch worn about {h} h"


# ------------------------------------------------------------------ HR mode and wear

def hr_mode_from_rate(samples_per_hour: Optional[float]) -> Optional[str]:
    if samples_per_hour is None:
        return None
    if samples_per_hour >= HR_CONTINUOUS_MIN_PER_H:
        return MODE_CONTINUOUS
    if samples_per_hour >= HR_TEN_MIN_MIN_PER_H:
        return MODE_TEN_MIN
    return MODE_MANUAL


def day_bin_minutes(mode: Optional[str]) -> int:
    return DAY_BIN_MIN_TEN_MODE if mode == MODE_TEN_MIN else DAY_BIN_MIN


def night_bin_minutes(mode: Optional[str]) -> int:
    return NIGHT_BIN_MIN_TEN_MODE if mode == MODE_TEN_MIN else NIGHT_BIN_MIN


def covered_bins(hr_ts: Iterable[int], start_ms: int, end_ms: int, bin_min: int) -> Tuple[int, int]:
    """(bins holding heart rate, bins) over [start, end)."""
    width = bin_min * 60_000
    total = max(0, (end_ms - start_ms + width - 1) // width)
    hit = set()
    for ts in hr_ts:
        if start_ms <= ts < end_ms:
            hit.add((ts - start_ms) // width)
    return len(hit), int(total)


def day_wear_hours(hr_ts: Iterable[int], day_start_ms: int, mode: Optional[str],
                   upto_ms: Optional[int] = None) -> Tuple[Optional[float], int, int]:
    """(wear hours, covered bins, expected bins) over 07:00-23:00 local (or up
    to `upto_ms` for today)."""
    start = day_start_ms + DAY_WEAR_START_H * 3_600_000
    end = day_start_ms + DAY_WEAR_END_H * 3_600_000
    if upto_ms is not None:
        end = max(start, min(end, upto_ms))
    bin_min = day_bin_minutes(mode)
    hit, total = covered_bins(hr_ts, start, end, bin_min)
    if total == 0:
        return None, 0, 0
    return hit * bin_min / 60.0, hit, total


def is_wearable(device_type: Optional[str]) -> bool:
    """Wearable origin, or one Health Connect did not label (HR coverage still
    has to prove wear). A phone is never a wearable."""
    if not device_type or device_type.upper() in ("UNKNOWN", ""):
        return True
    return device_type.upper() in catalog.WEARABLE_DEVICE_TYPES


# ------------------------------------------------------------------ troubles

def phone_troubles(report: Optional[dict], categories: Dict[str, str]) -> List[str]:
    """Trouble codes the latest capability report implies (counts only)."""
    if not report:
        return []
    hc = report.get("health_connect") or {}
    granted_full = hc.get("granted")
    out: List[str] = []
    if isinstance(granted_full, list):
        granted = {g[len(catalog.HC_PERMISSION_PREFIX):] if g.startswith(
            catalog.HC_PERMISSION_PREFIX) else g for g in granted_full if isinstance(g, str)}
        for cat in catalog.CATEGORIES:
            if categories.get(cat) in catalog.CONSENTED and \
                    catalog.CATEGORY_PERMISSIONS[cat][0] not in granted:
                out.append(f"permission_revoked:{cat}")
        if catalog.PERMISSION_HISTORY not in granted:
            out.append("history_window_capped")
        if catalog.PERMISSION_BACKGROUND not in granted:
            out.append("foreground_only")
    if (report.get("phone") or {}).get("battery_restricted") is True:
        out.append("battery_restricted")
    return out


def iso_to_ms(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def is_stale(last_push_at: Optional[str], now_ms: int) -> bool:
    last = iso_to_ms(last_push_at)
    return last is None or (now_ms - last) > STALE_SYNC_H * 3_600_000


def not_worn_likely(recent_nights: List[Optional[int]], worn_before: bool) -> bool:
    """`recent_nights`: night_worn for the last nights, newest first (None =
    no record). Not worn on most of them while the person normally wears it."""
    window = recent_nights[:NOT_WORN_LOOKBACK_NIGHTS]
    missing = sum(1 for n in window if not n)
    return worn_before and len(window) == NOT_WORN_LOOKBACK_NIGHTS and \
        missing >= NOT_WORN_MISSING_NIGHTS
