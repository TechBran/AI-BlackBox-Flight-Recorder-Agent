"""The v1 wellness catalog: categories, metrics, units, formatters, defaults
and the prompt texts (stdlib only).

Every number a model sees reaches it with a `display` string ("6 h 05 min")
and a `spoken` string ("about six hours") built here, so the model never has
to format, round or convert a health value itself (rule W6). The metric set is
the spec's §2.1/§3.2 v1 list; the question log can extend it before M7.4
without a migration (the store's `metric` column is open).
"""
from __future__ import annotations

import dataclasses
import math
import re
from typing import Dict, Optional

POLICY_VERSION = "2026-10-29"

# --------------------------------------------------------------- categories

CATEGORIES = ("sleep", "heart", "activity", "blood_pressure")
CONSENT_VALUES = ("off", "ask", "proactive")
CONSENTED = frozenset({"ask", "proactive"})

# Sleep, heart and activity are pre-selected at setup (as "ask"); blood
# pressure is off, and local-only once turned on (decision D9).
DEFAULT_CONSENT = {"sleep": "ask", "heart": "ask", "activity": "ask", "blood_pressure": "off"}
DEFAULT_LOCAL_ONLY = {"sleep": False, "heart": False, "activity": False, "blood_pressure": True}

CATEGORY_LABELS = {
    "sleep": "Sleep", "heart": "Heart", "activity": "Activity",
    "blood_pressure": "Blood pressure",
}

# Health Connect read permission per category: the first one is the one the
# category cannot work without (a missing one is `permission_revoked:<cat>`).
HC_PERMISSION_PREFIX = "android.permission.health."
CATEGORY_PERMISSIONS = {
    "sleep": ("READ_SLEEP",),
    "heart": ("READ_HEART_RATE", "READ_RESTING_HEART_RATE", "READ_HEART_RATE_VARIABILITY",
              "READ_OXYGEN_SATURATION", "READ_RESPIRATORY_RATE"),
    "activity": ("READ_STEPS", "READ_DISTANCE", "READ_ACTIVE_CALORIES_BURNED",
                 "READ_TOTAL_CALORIES_BURNED", "READ_EXERCISE"),
    "blood_pressure": ("READ_BLOOD_PRESSURE",),
}
PERMISSION_HISTORY = "READ_HEALTH_DATA_HISTORY"
PERMISSION_BACKGROUND = "READ_HEALTH_DATA_IN_BACKGROUND"

# ------------------------------------------------------------------ metrics


@dataclasses.dataclass(frozen=True)
class Metric:
    name: str
    category: str
    unit: str
    label: str
    grain: str            # night | day | series | session | reading
    fmt: str              # formatter key (see display()/spoken())
    floor: Optional[float] = None       # usual-range floor in the metric's unit
    floor_pct: Optional[float] = None   # ... or as a share of the median
    valid: str = "any"    # which days count for the baseline: night | wear | any
    stat: str = "mean"    # how a multi-day period combines daily values


METRICS: Dict[str, Metric] = {m.name: m for m in (
    Metric("sleep_duration", "sleep", "min", "Sleep", "night", "duration", floor=20.0, valid="night"),
    Metric("sleep_stages", "sleep", "min", "Sleep stages", "night", "duration"),
    Metric("sleeping_hr_median", "heart", "bpm", "Sleeping heart rate", "night", "bpm",
           floor=2.0, valid="night"),
    Metric("sleeping_hr_p10", "heart", "bpm", "Sleeping heart rate (low)", "night", "bpm",
           floor=2.0, valid="night"),
    Metric("resting_hr", "heart", "bpm", "Resting heart rate", "day", "bpm", floor=2.0),
    Metric("heart_rate", "heart", "bpm", "Heart rate", "series", "bpm", floor=2.0, valid="wear"),
    Metric("hrv", "heart", "ms", "Heart rate variability (RMSSD)", "reading", "ms", floor=3.0),
    Metric("spo2", "heart", "%", "Blood oxygen", "reading", "percent", floor=1.0),
    Metric("respiratory_rate", "heart", "breaths/min", "Breathing rate", "reading", "breaths",
           floor=1.0),
    Metric("steps", "activity", "steps", "Steps", "day", "steps", floor_pct=0.10, valid="wear",
           stat="sum"),
    Metric("distance", "activity", "m", "Distance", "day", "distance", floor_pct=0.10,
           valid="wear", stat="sum"),
    Metric("active_calories", "activity", "kcal", "Active calories", "day", "kcal",
           floor_pct=0.10, valid="wear", stat="sum"),
    Metric("total_calories", "activity", "kcal", "Total calories", "day", "kcal",
           floor_pct=0.10, valid="wear", stat="sum"),
    Metric("exercise", "activity", "min", "Workouts", "session", "duration", stat="sum"),
    Metric("bp_systolic", "blood_pressure", "mmHg", "Blood pressure (systolic)", "reading",
           "mmhg", floor=5.0),
    Metric("bp_diastolic", "blood_pressure", "mmHg", "Blood pressure (diastolic)", "reading",
           "mmhg", floor=4.0),
)}

# Wire names the phone uses in a batch (bbx.wellness.ingest/1).
BUCKET_METRICS = {"heart_rate": "hr", "steps": "sum", "distance": "sum",
                  "active_calories": "sum", "total_calories": "sum"}
READING_METRICS = ("resting_hr", "hrv", "spo2", "respiratory_rate", "blood_pressure")
SESSION_KINDS = ("sleep", "exercise")

# Health Connect sleep stage codes (SleepSessionRecord.STAGE_TYPE_*).
STAGE_AWAKE, STAGE_SLEEPING, STAGE_OUT_OF_BED = 1, 2, 3
STAGE_LIGHT, STAGE_DEEP, STAGE_REM, STAGE_AWAKE_IN_BED = 4, 5, 6, 7
ASLEEP_STAGES = frozenset({STAGE_SLEEPING, STAGE_LIGHT, STAGE_DEEP, STAGE_REM})
STAGE_NAMES = {STAGE_LIGHT: "light", STAGE_DEEP: "deep", STAGE_REM: "rem",
               STAGE_SLEEPING: "asleep", STAGE_AWAKE: "awake", STAGE_AWAKE_IN_BED: "awake",
               STAGE_OUT_OF_BED: "out_of_bed"}

# Health Connect ExerciseSessionRecord.EXERCISE_TYPE_* codes the tools name.
EXERCISE_TYPES = {56: "run", 57: "run", 79: "walk", 37: "walk", 8: "cycle", 9: "cycle",
                  70: "strength", 81: "strength", 74: "swim", 73: "swim"}
EXERCISE_TYPE_NAMES = ("run", "walk", "cycle", "strength", "swim", "other")

# Health Connect device types that are worn (wear is inferred from their HR).
WEARABLE_DEVICE_TYPES = frozenset({"WATCH", "RING", "FITNESS_BAND", "CHEST_STRAP"})


def exercise_type_name(code) -> str:
    if isinstance(code, str):
        word = code.strip().lower()
        return word if word in EXERCISE_TYPE_NAMES else "other"
    if isinstance(code, int) and not isinstance(code, bool):
        return EXERCISE_TYPES.get(code, "other")
    return "other"


def category_of(metric: str) -> Optional[str]:
    m = METRICS.get(metric)
    return m.category if m else None


# --------------------------------------------------------------- formatting

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
         "fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")


def number_words(n: int) -> str:
    """English words for a non-negative integer below one billion."""
    n = int(n)
    if n < 0:
        return "minus " + number_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + ("-" + _ONES[ones] if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return _ONES[hundreds] + " hundred" + (" " + number_words(rest) if rest else "")
    for size, word in ((1_000_000, "million"), (1000, "thousand")):
        if n >= size:
            head, rest = divmod(n, size)
            return number_words(head) + " " + word + (" " + number_words(rest) if rest else "")
    return str(n)  # unreachable below a billion


def _grouped(n: float) -> str:
    return f"{int(round(n)):,}"


def duration_display(minutes: float) -> str:
    total = int(round(minutes))
    hours, mins = divmod(abs(total), 60)
    if hours == 0:
        return f"{mins} min"
    return f"{hours} h {mins:02d} min"


def duration_spoken(minutes: float) -> str:
    total = abs(float(minutes))
    if total < 2.5:
        return "less than five minutes"
    if total < 60:
        return f"about {number_words(max(5, int(round(total / 5.0)) * 5))} minutes"
    halves = int(round(total / 30.0))
    whole, half = divmod(halves, 2)
    unit = "hour" if whole == 1 and not half else "hours"
    return f"about {number_words(whole)}{' and a half' if half else ''} {unit}"


def _round_spoken(n: float) -> int:
    n = abs(n)
    if n < 1000:
        return int(round(n / 10.0)) * 10 if n >= 100 else int(round(n))
    return int(round(n / 100.0)) * 100


def display(metric: str, value) -> Optional[str]:
    """The on-screen string for a value of `metric` (None for a gap)."""
    if value is None:
        return None
    fmt = METRICS[metric].fmt if metric in METRICS else "plain"
    v = float(value)
    if fmt == "duration":
        return duration_display(v)
    if fmt == "bpm":
        return f"{int(round(v))} bpm"
    if fmt == "ms":
        return f"{int(round(v))} ms"
    if fmt == "percent":
        return f"{int(round(v))} %"
    if fmt == "breaths":
        return f"{v:.1f} breaths/min"
    if fmt == "steps":
        return f"{_grouped(v)} steps"
    if fmt == "distance":
        return f"{v / 1000.0:.1f} km" if v >= 1000 else f"{int(round(v))} m"
    if fmt == "kcal":
        return f"{_grouped(v)} kcal"
    if fmt == "mmhg":
        return f"{int(round(v))} mmHg"
    return f"{v:g}"


def spoken(metric: str, value) -> Optional[str]:
    """The read-aloud string (at most one rounded number, in words)."""
    if value is None:
        return None
    fmt = METRICS[metric].fmt if metric in METRICS else "plain"
    v = float(value)
    if fmt == "duration":
        return duration_spoken(v)
    if fmt == "bpm":
        return f"{number_words(int(round(v)))} beats per minute"
    if fmt == "ms":
        return f"{number_words(int(round(v)))} milliseconds"
    if fmt == "percent":
        return f"{number_words(int(round(v)))} percent"
    if fmt == "breaths":
        return f"about {number_words(int(round(v)))} breaths a minute"
    if fmt == "steps":
        return f"about {number_words(_round_spoken(v))} steps"
    if fmt == "distance":
        if v >= 1000:
            km = round(v / 1000.0 * 2) / 2
            whole = int(km)
            half = " and a half" if km - whole else ""
            return f"about {number_words(whole)}{half} kilometres"
        return f"about {number_words(_round_spoken(v))} metres"
    if fmt == "kcal":
        return f"about {number_words(_round_spoken(v))} calories"
    if fmt == "mmhg":
        return f"{number_words(int(round(v)))}"
    return f"{v:g}"


def bp_display(systolic, diastolic) -> Optional[str]:
    if systolic is None or diastolic is None:
        return None
    return f"{int(round(systolic))}/{int(round(diastolic))} mmHg"


def bp_spoken(systolic, diastolic) -> Optional[str]:
    if systolic is None or diastolic is None:
        return None
    return f"{number_words(int(round(systolic)))} over {number_words(int(round(diastolic)))}"


def delta_display(metric: str, delta) -> Optional[str]:
    """"about 1 h 05 min less", "about 4 bpm higher", "about 2,100 fewer steps"."""
    if delta is None:
        return None
    d = float(delta)
    if not math.isfinite(d):
        return None
    fmt = METRICS[metric].fmt if metric in METRICS else "plain"
    up = d > 0
    mag = abs(d)
    shown = duration_display(mag) if fmt == "duration" else display(metric, mag)
    if re.match(r"0(\.0+)?(?![\d.])", shown or ""):
        return "about the same as usual"
    if fmt == "duration":
        return f"about {duration_display(mag)} {'more' if up else 'less'}"
    if fmt == "steps":
        return f"about {_grouped(mag)} {'more' if up else 'fewer'} steps"
    text = display(metric, mag)
    return f"about {text} {'higher' if up else 'lower'}"


def clock(minutes_after_midnight: Optional[float]) -> Optional[str]:
    """"23:40" for a local time given as minutes after (or before) midnight."""
    if minutes_after_midnight is None:
        return None
    m = int(round(minutes_after_midnight)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


# ------------------------------------------------------------- prompt texts

# Injected with the wellness tools only (wave 2), after the WELLNESS RULES.
# Each operator can replace it from Health settings (enroll.update_settings:
# 2,000 characters at most, control characters stripped); None = this text.
DEFAULT_INSTINCT_PROMPT = (
    "You have this person's own wearable data (sleep, heart, activity and, if they turned "
    "it on, blood pressure) through the wellness_* tools. When a question touches sleep, "
    "energy or tiredness, recovery, training or exercise, stress, travel or jet lag, "
    "feeling unwell, or planning the day, check the data first and bring in what is "
    "relevant: what was measured, how complete it is (its coverage), and \"as of\" when it "
    "is not fresh. Quote the display or spoken strings exactly as given and compare with "
    "the person's own usual range, not with population norms. If the data is missing or "
    "thin, say so plainly instead of guessing. Use it when it helps answer what they "
    "asked; never volunteer health remarks they did not ask for."
)
INSTINCT_PROMPT_MAX = 2000

# The rules block (spec §5), injected only when a wellness tool is offered.
WELLNESS_RULES = (
    "WELLNESS RULES: Wellness information only. Never diagnose, never name a condition "
    "from the data, never call a value abnormal or dangerous, never advise starting, "
    "stopping or changing medication or treatment. Use only numbers from this turn's "
    "wellness_* results, quoted through their display or spoken strings; never compute, "
    "estimate or recall health numbers. Say \"as of <time>\" when the data is stale and "
    "state coverage when it is incomplete. Missing data is \"not recorded\", never zero. "
    "Compare with the person's own usual range. Draw no conclusion from one night. If the "
    "user describes chest pain, fainting, severe breathlessness, stroke signs or thoughts "
    "of self-harm, the first sentence tells them to call emergency services. Don't raise "
    "health topics they didn't ask about. You can't change health sharing; point to "
    "Settings, Health on their phone. You can see only this operator's own data."
)
