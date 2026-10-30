"""Mask: health numbers never enter the immutable ledger (spec §3.7, W27, W28,
W30, W32; checks A2, A3b, A11b).

A turn is a wellness turn when at least one `delivered` row carries its turn
key (api.Delivery writes one whenever a wellness result reaches a model). The
store can re-supply any number on demand and nobody can un-mint one, so before
such a turn is recorded (the ledger, tasks.db, the cron rows) its assistant
text is masked:

  1. V, the value set, is built from the turn's delivered payloads: every
     number, its rounded and grouped forms, its words, the display and spoken
     strings, duration forms ("6 h 05", "6:05", "6.1 hours", "six hours") and
     clock times;
  2. every member of V, and every number (digits or words) within 3 tokens of
     a health unit or noun (bpm, beats, steps, mmHg, SpO2, %, kg, lb, kcal, ml,
     oz, °F/°C, HRV, ms, breaths, h/hours/min near sleep or exercise words,
     and a count of awakenings: "times" near a wake word, "wake-ups")
     becomes "[·]";
  3. the [REASONING] block becomes "[REASONING omitted: wellness turn]".

The user's own words are never passed in: they stay verbatim (they chose to
say them; the policy discloses it). Over-masking is the safe direction.

The durable carry (W28): the rows live in the operator's store, so a save that
arrives after a restart still finds them. A client that echoes the turn key
(stream_start's `wellness_turn`) is matched exactly; one that does not (or
whose key matches nothing) is masked against every delivery of the last 24
hours not yet masked, and those rows are stamped. A turn whose rows another
save stamped first (two chats at once, neither echoing its key) is caught by
the recall rule: a reply that quotes one of the operator's results of the last
72 hours in a distinctive form (a decimal, grouped thousands, a pair, a number
with a heart, blood-pressure or step unit) is masked and tagged. The recall
rule is a heuristic, so it only ever changes the ledger's copy: user-facing
text (an SMS or cron reply, result_data) stays as the model wrote it.

scrub_history() is the provider-switch rule (A3b): earlier assistant bubbles a
client sends back never carry health numbers to a provider the operator did
not allow.

speech_refused() is the speech rule (contract e, W26/W31/W32): the box never
reads a health reply aloud. For a caller who proves nothing it reads the
words alone (says_health_reading), never anyone's store, so its answer is no
way to read another operator's values back (W17); the operator's own signed
request is also held to their results of the last 24 hours (speech_recalls).
"""
from __future__ import annotations

import dataclasses
import math
import re
import sqlite3
import time
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from Orchestrator.wellness import catalog

MASK = "[·]"
REASONING_OMITTED = "[REASONING omitted: wellness turn]"
TOOLS_LINE = "- wellness: [numbers kept in the health store]"
UNMATCHED_WINDOW_MS = 24 * 3600 * 1000
HISTORY_WINDOW_MS = 72 * 3600 * 1000
UNIT_DISTANCE = 3
CONTEXT_DISTANCE = 8


def _now_ms() -> int:
    return int(time.time() * 1000)


def is_wellness_tool(name) -> bool:
    return isinstance(name, str) and name.strip().lower().startswith("wellness_")


# ------------------------------------------------------------------ the value set

# Keys whose numbers describe the answer, not the body: counts, coverage,
# shares, epochs. Their subtrees add nothing to V.
_STRUCTURAL = frozenset({"n", "n_used", "n_expected", "pct", "coverage_pct", "share", "days",
                         "total_in_window", "days_with_data_28", "nights_worn_7", "epoch",
                         "limit", "coverage", "day_wear_h", "night_worn", "flags", "readings",
                         "days_with_data"})
# Subtrees with no health values (ids, sync times, troubles, notes, sources).
_SKIP = frozenset({"ref", "session_ref", "tool", "tz", "as_of", "period", "troubles", "notes",
                   "sources", "phone", "platforms", "origins", "categories", "method", "label",
                   "metric", "category", "unit", "units", "columns", "kind", "type", "source",
                   "window", "granularity_used", "stat", "resolution", "night_of", "day",
                   "start_date", "end_date", "t", "usual_reason", "sync_mode", "providers_allowed",
                   "samsung_only", "wear", "error", "fix"})
_DISPLAY_KEYS = frozenset({"display", "spoken", "delta_display", "duration", "distance",
                           "calories", "bedtime", "wake_time"})
_CLOCK_KEYS = frozenset({"start", "end", "bedtime", "wake_time", "sleep_onset", "sleep_wake",
                         "onset", "wake", "time"})
_MIN_KEYS = frozenset({"parts", "stages_min"})

_ISO_CLOCK = re.compile(r"T(\d{2}):(\d{2})")
_BARE_CLOCK = re.compile(r"(?<![\d:+\-T])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
_NUM_IN_TEXT = re.compile(r"\d+(?:[.,:/]\d+)*")


def _words(n: int) -> Set[str]:
    try:
        w = catalog.number_words(int(n))
    except Exception:  # noqa: BLE001
        return set()
    return {w, w.replace("-", " ")}


_SCI = re.compile(r"\d[eE][+-]?\d")


def _number_forms(x: float, rounded: bool = True) -> Set[str]:
    """x as a reply writes it. `rounded` adds a big number's rounded and
    abbreviated forms ("12,300", "12,000", "12.3k" for 12,345)."""
    out: Set[str] = set()
    if not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x):
        return out
    x = float(x)
    integral = x.is_integer()
    if integral and abs(x) < 10:
        return out          # counts and flags; the unit rule covers small values
    r = int(round(x))
    out.add(str(r))
    out |= _words(r)
    if not integral:
        one, two = f"{x:.1f}", f"{x:.2f}"
        decimals = {one, two, two.rstrip("0").rstrip(".")}
        # The same number with a decimal comma ("47,3": a Spanish reply).
        out |= decimals | {d.replace(".", ",") for d in decimals}
        whole = int(abs(x))
        tenth = int(round((abs(x) - whole) * 10)) % 10
        out |= {f"{w} point {catalog.number_words(tenth)}" for w in _words(whole)}
    if abs(x) >= 1000:
        out |= _grouped(r)
        if not rounded:
            return {v for v in out if v and not _SCI.search(v)}
        for nd in (-1, -2, -3):
            rr = int(round(x, nd))
            out |= {str(rr)} | _grouped(rr)
            out |= _words(rr)
        k = x / 1000.0
        out |= {f"{k:.1f}k", f"{k:.1f} thousand", f"{k:g}k", f"{int(round(k))}k",
                f"{k:.1f}".replace(".", ",") + "k"}
    return {v for v in out if v and not _SCI.search(v)}


def _km_forms(metres: float) -> Set[str]:
    """A distance stored in metres as a reply writes it: "3.4", "3.41" (km),
    with the decimal comma, plus the catalog's own display of it. A usual
    range's low and high are plain metres in the payload, so without this
    "(3.4 to 7.3 km)" had only the unit rule to catch it."""
    if not isinstance(metres, (int, float)) or isinstance(metres, bool) \
            or not math.isfinite(metres) or abs(metres) < 100:
        return set()
    km = float(metres) / 1000.0
    forms = {f"{km:.1f}", f"{km:.2f}", f"{km:.2f}".rstrip("0").rstrip(".")}
    forms |= {f.replace(".", ",") for f in forms}
    forms.add(catalog.display("distance", metres))
    return {f for f in forms if f and not re.fullmatch(r"\d", f)}


def _grouped(n: int) -> Set[str]:
    """n with its thousands grouped by a comma, a space or a point."""
    g = f"{n:,}"
    return {g, g.replace(",", " "), g.replace(",", ".")}


def _duration_forms(minutes: float) -> Set[str]:
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) \
            or not math.isfinite(minutes) or minutes < 1:
        return set()
    total = int(round(minutes))
    h, m = divmod(total, 60)
    out = {catalog.duration_display(minutes), f"{m} min", f"{m} minutes", f"{total} min",
           f"{total} minutes"}
    spoken = catalog.duration_spoken(minutes)
    out |= {spoken, spoken.replace("about ", "")}
    if h:
        out |= {f"{h} h {m:02d}", f"{h} h {m}", f"{h}h{m:02d}", f"{h}h {m:02d}", f"{h}h{m:02d}m",
                f"{h}:{m:02d}", f"{h} h {m:02d} min", f"{h} hr {m} min", f"{h} hrs {m} min",
                f"{h} hours {m} minutes", f"{h} hours and {m} minutes",
                f"{h} hour {m} minutes", f"{h} hour and {m} minutes"}
        for nd in (1, 2):
            dec = f"{minutes / 60.0:.{nd}f}"
            out |= {f"{dec} hours", f"{dec} h", f"{dec} hrs", f"{dec} hr", dec,
                    dec.replace(".", ",")}
        for hw in _words(h):
            out |= {f"{hw} hours", f"{hw} hour"}
            for mw in _words(m) if m else ():
                out |= {f"{hw} hours and {mw} minutes", f"{hw} hours {mw} minutes",
                        f"{hw} hour and {mw} minutes"}
    return {s for s in out if s}


def _clock_forms(hh: int, mm: int) -> Set[str]:
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return set()
    h12 = hh % 12 or 12
    suf = "am" if hh < 12 else "pm"
    out = {f"{hh:02d}:{mm:02d}", f"{hh}:{mm:02d}", f"{h12}:{mm:02d}", f"{h12}:{mm:02d} {suf}",
           f"{h12}:{mm:02d}{suf}", f"{h12}:{mm:02d} {suf[0]}.m."}
    if mm == 0:
        out |= {f"{h12} {suf}", f"{h12}{suf}", f"{h12} {suf[0]}.m."}
    return out


def _clocks_in(text: str) -> Set[str]:
    out: Set[str] = set()
    for rx in (_ISO_CLOCK, _BARE_CLOCK):
        for m in rx.finditer(text):
            out |= _clock_forms(int(m.group(1)), int(m.group(2)))
        if rx is _ISO_CLOCK and _ISO_CLOCK.search(text):
            break
    return out


def _display_forms(text: str) -> Set[str]:
    s = " ".join(str(text).split())
    if not s or s == "not recorded":
        return set()
    out = {s}
    if s.lower().startswith("about "):
        out.add(s[6:])
    for piece in re.split(r",\s+|;\s+| · ", s):
        piece = piece.strip()
        if piece and piece != s:
            out.add(piece)
            if piece.lower().startswith("about "):
                out.add(piece[6:])
    for m in _NUM_IN_TEXT.finditer(s):
        tok = m.group(0)
        if not re.fullmatch(r"\d", tok):
            out.add(tok)
    out |= _clocks_in(s)
    return out


def value_set(payloads: Iterable, *, rounded: bool = True) -> frozenset:
    """V for a turn: every value-bearing string the delivered payloads hold.
    With rounded=False, a big number's rounded and abbreviated forms stay
    out: the exact forms the recall rule draws its distinctive quotes from
    (quotes_results)."""
    values: Set[str] = set()

    def unit_of(key, unit):
        m = catalog.METRICS.get(key) if isinstance(key, str) else None
        if m is not None:
            return m.unit
        if isinstance(key, str) and (key.endswith("_min") or key in _MIN_KEYS):
            return "min"
        return unit

    def walk(node, key=None, unit=None):
        if key in _SKIP or key in _STRUCTURAL or (
                isinstance(key, str) and key.endswith(("_pct", "_count", "_days"))):
            return
        if isinstance(node, dict):
            if isinstance(node.get("columns"), list) and isinstance(node.get("rows"), list):
                cols = node["columns"]
                units = node.get("units") if isinstance(node.get("units"), dict) else {}
                for row in node["rows"]:
                    if not isinstance(row, list):
                        continue
                    for col, cell in zip(cols, row):
                        walk(cell, col, units.get(col) or unit_of(col, None))
            u = node.get("unit") if isinstance(node.get("unit"), str) else unit
            for k, v in node.items():
                if k in ("rows",) and "columns" in node:
                    continue
                walk(v, k, unit_of(k, u))
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                walk(item, key, unit)
            return
        if isinstance(node, bool) or node is None:
            return
        if isinstance(node, (int, float)):
            values.update(_number_forms(node, rounded))
            if unit == "min":
                values.update(_duration_forms(node))
            elif unit == "m":
                values.update(_km_forms(node))
            return
        if isinstance(node, str):
            if key in _DISPLAY_KEYS or (isinstance(key, str) and key.endswith("_display")):
                values.update(_display_forms(node))
            elif key in _CLOCK_KEYS or unit == "clock":
                values.update(_clocks_in(node))

    for payload in payloads:
        walk(payload)
    return frozenset(v for v in values if v and not re.fullmatch(r"\d", v))


# ------------------------------------------------------------------ masking text

# English and Spanish (the box's second chat language): the unit rule is the
# net under V, so a reply in either names its units in words the rule knows.
_UNIT_WORDS = frozenset("""
bpm beat beats heartbeat heartbeats pulse heart hr rhr hrv rmssd step steps mmhg hg spo2 spo₂
oxygen saturation percent percentage kg kgs kilo kilos kilogram kilograms lb lbs pound pounds
kcal cal cals calorie calories ml oz ounce ounces °f °c degree degrees ms millisecond
milliseconds breath breaths breathing respiratory systolic diastolic bp pressure glucose weight
temperature km kms kilometre kilometres kilometer kilometers mile miles mi metre metres meter
meters vo2 vo2max resting
lpm ppm latido latidos pulsaciones pulso cardíaca cardiaca cardíaco cardiaco frecuencia paso
pasos presión presion arterial sistólica sistolica diastólica diastolica oxígeno oxigeno
saturación saturacion peso caloría calorías caloria calorias respiración respiracion
respiraciones temperatura grado grados kilómetro kilómetros kilometro kilometros metros
reposo glucosa
wake-up wake-ups wakeup wakeups awakening awakenings wakings despertares
""".split())
# A count of awakenings has no unit word ("you woke up 3 times", "te
# despertaste 3 veces"): "times" / "veces" is one only within reach of a wake
# word (a Spanish one is any form of despertar). A tally elsewhere ("I called
# 3 times") stays readable.
_TALLY_WORDS = frozenset({"times", "veces"})
_WAKE_WORDS = frozenset({"woke", "woken", "wake", "wakes", "waking", "awake", "awoke",
                         "awoken", "awakened"})
_TIME_WORDS = frozenset("""
h hr hrs hour hours min mins minute minutes am pm hora horas minuto minutos
""".split())
_CONTEXT_WORDS = frozenset("""
sleep sleeps slept sleeping asleep nap naps napped bed bedtime woke wake waking awake night
nights overnight tonight rem deep run ran running runs walk walked walking walks workout
workouts exercise exercised exercising active activity cycling cycled ride rode swim swam
swimming training trained jog jogged jogging hike hiked
sueño dormir dormiste dormías dormias dormido dormida durmiendo durmió durmio siesta cama
noche noches anoche despertaste despertar profundo caminaste caminata caminar caminando
corriste correr carrera ejercicio entrenamiento entrenaste actividad nadaste bici
""".split())
_NUMBER_WORDS = frozenset("""
zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen
sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety
hundred thousand million half dozen
cero uno una dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince
dieciséis dieciseis diecisiete dieciocho diecinueve veinte veintiuno veintidós veintidos
veintitrés veintitres veinticuatro veinticinco veintiséis veintiseis veintisiete veintiocho
veintinueve treinta cuarenta cincuenta sesenta setenta ochenta noventa cien ciento
doscientos trescientos cuatrocientos quinientos seiscientos setecientos ochocientos
novecientos mil millón millon medio media
""".split())
_JOINERS = frozenset({"and", "point", "a", "y", "coma"})

# Words are any letters (an accented "cardíaca" is one token, not three). A
# value already masked ("[·]") is ONE number token: in "3.4 to [·] km" the
# unit stays within reach of the first number of the range, and the two
# ends stay one phrase apart, as they were before the value pass ran.
_TOKEN_RE = re.compile(
    r"(?P<num>" + re.escape(MASK) + r"|\d+(?:[.,:/]\d+)*(?:k(?![^\W\d_]))?)"
    r"|(?P<word>(?i:spo2|spo₂|vo2max|vo2)|(?:[^\W\d_]|[°₂])+(?:['’-][^\W\d_]+)*)"
    r"|(?P<pct>%)"
    r"|(?P<other>[^\s\w])")


def _is_number_word(word: str) -> bool:
    parts = word.lower().replace("’", "'").split("-")
    return all(p in _NUMBER_WORDS for p in parts) and bool(parts)


def _is_wake_word(low: str) -> bool:
    return low in _WAKE_WORDS or low.startswith("despert")


def _unit_rule_spans(text: str) -> List[Tuple[int, int]]:
    toks = []
    wakes = []
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup
        low = m.group(0).lower()
        if kind == "num":
            cls = "num"
        elif kind == "pct":
            cls = "unit"
        elif kind == "word":
            if _is_wake_word(low):
                wakes.append(len(toks))
            if _is_number_word(low):
                cls = "num"
            elif low in _UNIT_WORDS:
                cls = "unit"
            elif low in _TIME_WORDS:
                cls = "time"
            elif low in _TALLY_WORDS:
                cls = "tally"
            elif low in _CONTEXT_WORDS:
                cls = "context"
            elif low in _JOINERS:
                cls = "join"
            else:
                cls = "word"
        else:
            cls = "other"
        toks.append((cls, m.start(), m.end()))
    if not toks:
        return []
    contexts = [i for i, t in enumerate(toks) if t[0] == "context"]
    units = [i for i, t in enumerate(toks) if t[0] == "unit" or (
        t[0] == "time" and any(abs(i - c) <= CONTEXT_DISTANCE for c in contexts)) or (
        t[0] == "tally" and any(abs(i - w) <= CONTEXT_DISTANCE for w in wakes))]
    if not units:
        return []
    # Runs of number tokens ("twelve thousand three hundred and forty-five",
    # "forty seven point three") are one phrase.
    phrases = []
    i = 0
    while i < len(toks):
        if toks[i][0] != "num":
            i += 1
            continue
        j = i
        while True:
            if j + 1 < len(toks) and toks[j + 1][0] == "num":
                j += 1
            elif j + 2 < len(toks) and toks[j + 1][0] == "join" and toks[j + 2][0] == "num":
                j += 2
            else:
                break
        phrases.append((i, j))
        i = j + 1
    spans = []
    for a, b in phrases:
        if any((a - u if u < a else u - b) <= UNIT_DISTANCE for u in units):
            spans.append((toks[a][1], toks[b][2]))
    return spans


def _value_regex(values: frozenset, *, tails: bool = True):
    if not values:
        return None
    alts = sorted(values, key=len, reverse=True)
    body = "|".join(re.escape(v) for v in alts)
    # Not inside a longer number ("147", "12,345") or a clock time (clocks
    # are members of their own), but a part of a pair is a hit on its own:
    # "118/79" masks as "[·]/[·]". A member with a decimal tail is the same
    # reading written another way ("47.30", "47,3", "52.1" through "52"), so
    # with `tails` the tail goes with it.
    tail = r"(?:[.,]\d+)?" if tails else ""
    return re.compile(r"(?<![\w.,:])(?:" + body + r")" + tail + r"(?![\w]|[.,:]\d)", re.I)


def mask_values(text: str, values: frozenset) -> str:
    """`text` with every member of `values` masked (no unit rule)."""
    rx = _value_regex(values) if text else None
    return rx.sub(MASK, text) if rx is not None else text


def mask_text(text: str, values: frozenset = frozenset()) -> str:
    """`text` with every member of `values` and every health-unit number masked."""
    if not text:
        return text
    out = mask_values(text, values)
    spans = _unit_rule_spans(out)
    if not spans:
        return out
    pieces, last = [], 0
    for a, b in spans:
        pieces.append(out[last:a])
        pieces.append(MASK)
        last = b
    pieces.append(out[last:])
    return "".join(pieces)


_REASONING_RE = re.compile(r"\[REASONING\]\n.*?(?=\n\n\[RESPONSE\]\n|\Z)", re.S)


def drop_reasoning(text: str) -> str:
    """The [REASONING] block (/chat/save puts it first, the worker last) as
    one omitted line."""
    if not text or "[REASONING]" not in text:
        return text
    return _REASONING_RE.sub(REASONING_OMITTED, text)


def mask(text: str, deliveries: Iterable[dict] = ()) -> Tuple[str, List[str], List[str]]:
    """(masked text, categories, refs) of `text` against the delivered rows."""
    rows = [r for r in deliveries if isinstance(r, dict)]
    values = value_set(r.get("payload") for r in rows)
    cats = sorted({c for r in rows for c in (r.get("categories") or [])})
    refs = [r.get("ref") for r in rows if r.get("ref")]
    return mask_text(drop_reasoning(text), values), cats, refs


# ------------------------------------------------------------------ the gauge

def period_label(env) -> str:
    """A period label for the WELLNESS gauge (no values: dates and words)."""
    if not isinstance(env, dict):
        return ""
    as_of = str(env.get("as_of") or "")[:10]
    p = env.get("period")
    if isinstance(p, dict):
        nights = sorted({m.get("night_of") for m in env.get("metrics") or []
                         if isinstance(m, dict) and isinstance(m.get("night_of"), str)})
        if p.get("name") == "last_night" and nights:
            return f"night of {nights[-1]}"
        label = str(p.get("label") or p.get("name") or "")
        halves = label.split(" to ")
        if len(halves) == 2 and halves[0] == halves[1]:
            label = halves[0]
        if label and not re.search(r"\d", label) and as_of:
            label = f"{label} to {as_of}"
        return label
    if env.get("start") and env.get("end"):
        a, b = str(env["start"])[:10], str(env["end"])[:10]
        return a if a == b else f"{a} to {b}"
    if env.get("window"):
        return f"{env['window']} to {as_of}" if as_of else str(env["window"])
    tool = str(env.get("tool") or "")
    return "sources" if tool == "wellness_sources" else (as_of or "")


# ------------------------------------------------------------------ the carry

@dataclasses.dataclass(frozen=True)
class Carry:
    """One turn's wellness results: what to mask its text against, and its tag.

    `exact` is a turn a delivered row names (its own key, or the keyless
    fallback): every copy of its text is masked, user-facing ones included.
    A heuristic carry (exact False: the recall rule, or a health store that
    could not be read) masks and tags the ledger's copy only, and stamps no
    row."""
    operator: str
    rows: Tuple[dict, ...]
    values: frozenset
    extra_categories: Tuple[str, ...] = ()
    op_key: Optional[str] = None
    exact: bool = True
    note: Optional[str] = None

    @property
    def refs(self) -> List[str]:
        return [r.get("ref") for r in self.rows if r.get("ref")]

    @property
    def categories(self) -> List[str]:
        cats = {c for r in self.rows for c in (r.get("categories") or [])}
        cats |= set(self.extra_categories)
        return sorted(cats) or list(catalog.CATEGORIES)

    @property
    def labels(self) -> List[str]:
        out = [period_label(r.get("payload")) for r in self.rows]
        return [x for x in dict.fromkeys(out) if x] or (
            [self.note] if self.note else (["on-device"] if self.extra_categories else []))

    def mask(self, text: str) -> str:
        return mask_text(drop_reasoning(text), self.values)

    def snap_text(self, masked_response: str, reasoning: Optional[str] = None) -> str:
        """/chat/save's snapshot body for the turn: the reasoning never goes in."""
        if reasoning:
            return f"{REASONING_OMITTED}\n\n[RESPONSE]\n{masked_response}"
        return masked_response

    def gauge(self) -> dict:
        return {"categories": self.categories, "labels": self.labels, "refs": self.refs}

    def _store(self):
        from Orchestrator.wellness import enroll
        return enroll.store_for(enroll.get_enrollment(self.operator), create=False)

    def mark(self, now_ms: Optional[int] = None) -> None:
        """Stamp masked_at on the turn's rows (never raises: the mask already
        happened; an unstamped row only widens a later fallback). A heuristic
        carry's rows belong to earlier turns: never stamped here."""
        if not self.exact or not self.refs:
            return
        try:
            st = self._store()
            if st is not None:
                st.mark_masked(self.refs, _now_ms() if now_ms is None else now_ms)
        except Exception as e:  # noqa: BLE001
            print(f"[WELLNESS] mask stamp failed: {type(e).__name__}")

    def note_task(self, task_id: str, now_ms: Optional[int] = None) -> None:
        """Record a tasks.db row of this turn in the store's scrub list (erase)."""
        try:
            st = self._store()
            if st is not None and task_id:
                st.add_task_ref(task_id, _now_ms() if now_ms is None else now_ms)
        except Exception as e:  # noqa: BLE001
            print(f"[WELLNESS] task ref failed: {type(e).__name__}")


def merge_gauge(existing: Optional[dict], new: Optional[dict]) -> Optional[dict]:
    """Two turns' gauges in one snapshot (a mint covers every turn since the last)."""
    if not new:
        return existing
    if not existing:
        return {k: list(v) for k, v in new.items()}
    return {k: list(dict.fromkeys(list(existing.get(k) or []) + list(new.get(k) or [])))
            for k in ("categories", "labels", "refs")}


def gauge_text(gauge: Optional[dict]) -> str:
    """The rendered WELLNESS gauge value for perform_mint ("" for none)."""
    if not gauge:
        return ""
    from Orchestrator.wellness import redact
    return redact.format_gauge(gauge.get("categories") or [], gauge.get("labels") or [],
                               gauge.get("refs") or [])


def items_gauge(items: Iterable[dict]) -> Optional[dict]:
    """The WELLNESS gauge of the conversation items a mint covers: each
    turn's items carry their own tag (turn_persistence), so a mint that lands
    between two appends never separates a turn from its tag."""
    gauge = None
    for it in items:
        if isinstance(it, dict) and it.get("wellness"):
            gauge = merge_gauge(gauge, it["wellness"])
    return gauge


def _unmatched_rows(st, now_ms: int) -> List[dict]:
    """Deliveries of the last 24 h not yet masked (the keyless fallback)."""
    return st.unmatched_deliveries(now_ms - UNMATCHED_WINDOW_MS)


# The recall rule's units: a reading's own units, never a duration, a percent,
# a weight or a distance (ordinary in any work chat: "30 minutes", "95%").
_RECALL_UNITS = frozenset("""
bpm beat beats heartbeat heartbeats pulse mmhg spo2 spo₂ hrv rmssd step steps systolic diastolic
breath breaths lpm ppm latido latidos pulsaciones pulso paso pasos sistólica sistolica
diastólica diastolica respiraciones
""".split())
# A step count is in the thousands: "step 12 of the plan" quotes nothing.
_RECALL_MIN = {"step": 1000, "steps": 1000, "paso": 1000, "pasos": 1000}
_DISTINCTIVE = re.compile(r"\d{2,}[.,]\d+|\d{1,3}(?:[,. ]\d{3})+|\d{2,3}/\d{2,3}")
_ROUND_THOUSANDS = re.compile(r"\d{1,3}(?:[,. ]000)+")


def _distinctive(values: frozenset) -> frozenset:
    """The members of V specific enough to say a text quotes a health result
    on their own: a decimal, grouped thousands or a pair ("47.3", "12,345",
    "118/79"). A bare "47", a duration, a clock time or a round thousand
    ("12,000": every other invoice) is not."""
    return frozenset(v for v in values
                     if _DISTINCTIVE.fullmatch(v) and not _ROUND_THOUSANDS.fullmatch(v))


def quotes_results(text: str, values: frozenset, *, exact: Optional[frozenset] = None) -> bool:
    """Does `text` repeat a delivered health result? A distinctive member of
    `exact` (V without its rounded forms, value_set(rounded=False); V itself
    when not given), two of V's numbers as a pair ("118/79"), or a number of
    V right by one of a reading's own units ("47 bpm", "12345 steps", "about
    12,300 steps"); never a duration, a percent or a clock time on its own.
    A rounded "12,300" or "1,200" is no quote without its unit: an ordinary
    "1,200 euros" stays an ordinary turn."""
    if not text or not values:
        return False
    rx = _value_regex(_distinctive(values if exact is None else exact), tails=False)
    if rx is not None and rx.search(text):
        return True
    toks = [(m.lastgroup, m.group(0).lower()) for m in _TOKEN_RE.finditer(text)]
    # Two of V's numbers written as a pair ("118/79": a reading's two halves).
    if any(kind == "num" and _DISTINCTIVE.fullmatch(w) and "/" in w
           and all(p in values for p in w.split("/")) for kind, w in toks):
        return True
    units = [(i, w) for i, (_k, w) in enumerate(toks) if w in _RECALL_UNITS]
    if not units:
        return False

    def reading(kind, w) -> bool:
        # Digits, or a compound number word ("forty-seven"); "seven" alone is
        # every other sentence.
        return w in values and (kind == "num" or ("-" in w and _is_number_word(w)))

    def big_enough(w, unit) -> bool:
        digits = re.sub(r"\D", "", w)
        return unit not in _RECALL_MIN or (digits.isdigit() and int(digits) >= _RECALL_MIN[unit])

    return any(reading(kind, w) and any(abs(i - u) <= UNIT_DISTANCE and big_enough(w, uw)
                                        for u, uw in units)
               for i, (kind, w) in enumerate(toks))


def carry_for(operator: Optional[str], *, turn_key=None, fallback: bool = False,
              extra_categories: Sequence[str] = (), recall_text: Optional[str] = None,
              recall_window_ms: int = HISTORY_WINDOW_MS,
              now_ms: Optional[int] = None) -> Optional[Carry]:
    """The turn's carry, or None for a turn no wellness result reached.

    `turn_key` (a string; anything else is no key) matches the turn's own
    rows; with `fallback`, a key that matches nothing (none echoed, a stale
    one) takes every unmasked delivery of the last 24 hours instead (the
    spec's rule for clients that do not echo the key). `extra_categories`
    marks a turn as wellness even with no rows (an on-device wellness call).

    `recall_text` (the assistant text) closes the recall gap: a later turn of
    the same chat sends the earlier, unmasked bubbles back to the model as
    history, and two chats at once can have one turn's save stamp the other's
    rows, so a reply can repeat a result with no row of its own. When it
    quotes one of the operator's results of the last 72 hours
    (`recall_window_ms`; quotes_results), the turn's ledger copy is masked
    and tagged against all of them: a heuristic carry (exact False).

    A health store that cannot be read is no reason to lose the turn: it is
    recorded, its ledger copy masked by the unit rule and tagged."""
    if not operator:
        return None
    from Orchestrator.wellness import enroll, store as store_mod
    now_ms = _now_ms() if now_ms is None else now_ms
    key = turn_key if isinstance(turn_key, str) and turn_key else None
    rec = enroll.get_enrollment(operator)
    rows: List[dict] = []
    values = frozenset()
    exact = True
    try:
        st = enroll.store_for(rec, create=False) if rec else None
        if st is not None:
            if key:
                rows = st.deliveries_for_turn(key)
            if not rows and fallback:
                rows = _unmatched_rows(st, now_ms)
            if rows:
                values = value_set(r.get("payload") for r in rows)
            elif recall_text:
                recent = _recent_history_rows(st, now_ms, recall_window_ms)
                recent_values = value_set(r.get("payload") for r in recent)
                exact_values = value_set((r.get("payload") for r in recent), rounded=False)
                if quotes_results(recall_text, recent_values, exact=exact_values):
                    rows, values, exact = recent, recent_values, False
    except store_mod.StoreGone:
        rows = []
    except (sqlite3.Error, OSError) as e:
        # Count-only: never a value, never the store's path.
        print(f"[WELLNESS] health store unreadable ({type(e).__name__}); "
              "recording the turn masked by the unit rule")
        return Carry(operator, (), frozenset(), tuple(catalog.CATEGORIES),
                     (rec or {}).get("op_key"), exact=False, note="health data")
    if not rows and not extra_categories:
        return None
    return Carry(operator, tuple(rows), values, tuple(extra_categories), (rec or {}).get("op_key"),
                 exact=exact)


def stream_turn_key(operator: Optional[str]) -> Optional[str]:
    """The turn key /chat/stream announces in stream_start: the stamped turn's,
    for an operator who has a health store (else None, so every other turn's
    stream_start is byte-identical)."""
    from Orchestrator.wellness import enroll, turn as turn_mod
    t = turn_mod.current()
    if t is None or not t.turn_key or not operator or t.operator != operator:
        return None
    try:
        rec = enroll.get_enrollment(operator)
    except Exception:  # noqa: BLE001
        return None
    return t.turn_key if rec and rec.get("op_key") else None


# ------------------------------------------------------------------ the speech rule (contract e)

# A reply that used health data is never read aloud in v1 (W26, W31, W32);
# routes/tts_routes.py asks these two rules before any voice runs:
#
#   * says_health_reading(text): the words alone. No store, no operator, no
#     clock, so the answer is the same whoever asks and whoever a body names
#     (W17: an answer that turned on the stored values of an operator an
#     unsigned body named let any caller read those values back one guess
#     at a time, and tell whether that operator was enrolled);
#   * speech_recalls(operator, text): the recall rule over the operator's
#     own deliveries of the last 24 hours, for a request their own phone
#     signed (they may learn their own values).
SPEECH_WINDOW_MS = 24 * 3600 * 1000

# Words that name a health reading on their own: a number within
# UNIT_DISTANCE of one is a reading wherever it stands ("47 bpm", "SpO2 96",
# "4 awakenings"). Durations, percents, weights and distances are left out
# ("30 minutes", "95%", "3 kg", "5 km" are every other reply), and so is "hr"
# (an hour).
_READING_WORDS = frozenset("""
bpm mmhg spo2 spo₂ hrv rmssd systolic diastolic pulse heartbeat heartbeats beats breaths rhr bp
vo2 vo2max oxygen lpm ppm latido latidos pulsaciones pulso sistólica sistolica diastólica
diastolica respiraciones cardíaca cardiaca cardíaco cardiaco arterial oxígeno oxigeno reposo
wake-up wake-ups wakeup wakeups awakening awakenings wakings despertares
""".split())
# Ordinary words that name a reading only after their partner ("blood
# pressure", "heart rate"): "under pressure", "the rate" stay plain.
_READING_PAIRS = {"pressure": frozenset({"blood"}),
                  "rate": frozenset({"heart", "pulse", "respiratory", "breathing"})}
# A step count is in the thousands: "step 12 of the plan" reads nothing.
_STEP_WORDS = frozenset({"step", "steps", "paso", "pasos"})
_BIG_NUMBER_WORDS = frozenset({"thousand", "million", "mil", "millón", "millon"})
# A duration is a reading near a sleep word ("you slept 6 h 47 min", "deep
# sleep 1 h 12 min"); exercise words are left out here ("walk through it in
# 30 minutes", "run the tests for 20 min").
_SLEEP_WORDS = frozenset("""
sleep sleeps slept sleeping asleep nap naps napped bedtime rem
sueño dormir dormiste dormías dormias dormido dormida durmiendo durmió durmio siesta
""".split())
_DURATION_WORDS = frozenset("""
h hr hrs hour hours min mins minute minutes hora horas minuto minutos
""".split())
_PAIR_WORDS = frozenset({"over", "sobre"})
_BP_TOKEN = re.compile(r"(\d{2,3})/(\d{2,3})")
_GROUPED_TOKEN = re.compile(r"\d{1,3}(?:[,.]\d{3})+")


def _blood_pressure(high: str, low: str) -> bool:
    """A systolic over a diastolic: 70-250 over 30-150, the first higher."""
    if not (high.isdigit() and low.isdigit()):
        return False
    s, d = int(high), int(low)
    return 70 <= s <= 250 and 30 <= d <= 150 and s > d


def _thousands(phrase: List[Tuple[str, str]]) -> bool:
    """Is a number phrase 1,000 or more ("12,345", "12 345", "12345", "12.3k",
    "twelve thousand ...")?"""
    digits = [w for cls, w in phrase if cls == "num" and w.isdigit()]
    if len(digits) > 1 and len(digits[0]) <= 3 and all(len(d) == 3 for d in digits[1:]):
        return True                             # thousands grouped by a space
    for cls, w in phrase:
        if cls != "num":
            continue
        if w in _BIG_NUMBER_WORDS or w.endswith("k") or _GROUPED_TOKEN.fullmatch(w):
            return True
        head = re.split(r"[.,]", w)[0]
        if head.isdigit() and int(head) >= 1000:
            return True
    return False


def says_health_reading(text) -> bool:
    """Does `text` read out a health reading, whoever's it is? A number (digits
    or words) within 3 tokens of a reading's own unit or noun (bpm, mmHg,
    SpO2, HRV, pulse, breaths, "heart rate", "blood pressure", awakenings,
    and their Spanish words), a step count in the thousands, a blood-pressure
    pair ("118/79", "120 over 77"), a duration near a sleep word, or a count
    of awakenings ("woke up 3 times"). The mask glyph is no number: a reply
    already masked passes. A bare value ("It was 47.3.") is no reading here;
    only the operator's own signed request can tell it is theirs
    (speech_recalls)."""
    if not isinstance(text, str) or not text.strip():
        return False
    toks: List[Tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(text):
        kind, low = m.lastgroup, m.group(0).lower()
        if kind == "num":
            toks.append(("glyph" if low == MASK.lower() else "num", low))
        elif kind == "word":
            toks.append(("num" if _is_number_word(low) else "word", low))
        else:
            toks.append(("other", low))
    for i, (cls, w) in enumerate(toks):
        if cls != "num":
            continue
        pair = _BP_TOKEN.fullmatch(w)
        if pair and _blood_pressure(pair.group(1), pair.group(2)):
            return True
        if i + 2 < len(toks) and toks[i + 1][1] in _PAIR_WORDS and toks[i + 2][0] == "num" \
                and _blood_pressure(w, toks[i + 2][1]):
            return True
    sleeps = [i for i, (c, w) in enumerate(toks) if c == "word" and w in _SLEEP_WORDS]
    wakes = [i for i, (c, w) in enumerate(toks) if c == "word" and _is_wake_word(w)]
    units: List[Tuple[int, bool]] = []          # (index, needs a count in the thousands)
    for i, (cls, w) in enumerate(toks):
        if cls != "word":
            continue
        if w in _READING_WORDS:
            units.append((i, False))
        elif w in _STEP_WORDS:
            units.append((i, True))
        elif w in _READING_PAIRS and i > 0 and toks[i - 1][1] in _READING_PAIRS[w]:
            units.append((i, False))
        elif w in _DURATION_WORDS and any(abs(i - s) <= CONTEXT_DISTANCE for s in sleeps):
            units.append((i, False))
        elif w in _TALLY_WORDS and any(abs(i - k) <= CONTEXT_DISTANCE for k in wakes):
            units.append((i, False))
    if not units:
        return False
    # Runs of number tokens are one phrase, as in the unit rule.
    i = 0
    while i < len(toks):
        if toks[i][0] != "num":
            i += 1
            continue
        j = i
        while True:
            if j + 1 < len(toks) and toks[j + 1][0] == "num":
                j += 1
            elif j + 2 < len(toks) and toks[j + 1][1] in _JOINERS and toks[j + 2][0] == "num":
                j += 2
            else:
                break
        phrase = toks[i:j + 1]
        for u, big in units:
            if (i - u if u < i else u - j) <= UNIT_DISTANCE and (not big or _thousands(phrase)):
                return True
        i = j + 1
    return False


def speech_recalls(operator: Optional[str], text, *, now_ms: Optional[int] = None) -> bool:
    """Does `text` repeat one of `operator`'s health results of the last 24
    hours (SPEECH_WINDOW_MS)? The recall rule (carry_for, quotes_results):
    an exact distinctive quote ("47.3", "12,345", "118/79") or a value by a
    reading's unit. Ask it only for a request the operator's own phone
    signed. A health store that cannot be read is a yes (any doubt is a no
    to speaking)."""
    if not operator or not isinstance(text, str) or not text.strip():
        return False
    try:
        return carry_for(operator, recall_text=text, recall_window_ms=SPEECH_WINDOW_MS,
                         now_ms=now_ms) is not None
    except Exception:  # noqa: BLE001 - a guard that cannot decide refuses
        return True


def speech_refused(text, *, proven_operator: Optional[str] = None,
                   now_ms: Optional[int] = None) -> bool:
    """The box's speech rule: refuse `text` when it reads out a health
    reading (says_health_reading, for everyone) or, for a request
    `proven_operator`'s own phone signed, when it repeats one of their
    results (speech_recalls)."""
    if not isinstance(text, str) or not text.strip():
        return False
    if says_health_reading(text):
        return True
    return bool(proven_operator) and speech_recalls(proven_operator, text, now_ms=now_ms)


# ------------------------------------------------------------------ history scrub (A3b)

def _message_text_masked(message: dict, values: frozenset, unit_rule: bool = True) -> dict:
    fn = mask_text if unit_rule else mask_values
    content = message.get("content")
    if isinstance(content, str):
        return {**message, "content": fn(content, values)}
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append({**part, "text": fn(part["text"], values)})
            else:
                parts.append(part)
        return {**message, "content": parts}
    return message


def _without_ref(message):
    if isinstance(message, dict) and "wellness_ref" in message:
        return {k: v for k, v in message.items() if k != "wellness_ref"}
    return message


def scrub_history(messages: list, operator: Optional[str], provider: Optional[str],
                  model: Optional[str] = None, *, now_ms: Optional[int] = None) -> list:
    """`messages` as `provider` may receive them (A3b).

    `provider` is the one the turn is dispatched to (routes/chat/stream.py
    resolves an alias the dispatcher does not know to the provider that
    really runs). For an operator with health data on this box (a live
    enrollment, or a store kept by a disconnect: an operator who is not live
    has no allowed destination at all), every earlier health number the
    provider may not receive is masked in the client-supplied assistant
    messages:

      * a message tagged `wellness_ref` (clients echo it from the
        wellness_used frame) whose ref's categories the provider may not
        receive is masked against its stored payload and by the unit rule;
      * every message, tagged or not, is masked against the operator's
        results of the last 72 hours the provider may not receive (a bubble
        can quote more than its own ref: an on-device reply covering sleep
        and blood pressure);
      * an untagged message is also masked by the unit rule whenever the
        provider is not allowed for one of the operator's consented
        categories.

    User messages stay verbatim. The `wellness_ref` key never reaches the
    provider. A list with nothing to change is returned as the SAME object."""
    if not isinstance(messages, list):
        return messages
    has_refs = any(isinstance(m, dict) and "wellness_ref" in m for m in messages)
    try:
        from Orchestrator.wellness import destinations, enroll, gate, store as store_mod
        rec = enroll.get_enrollment(operator) if operator else None
        live = bool(rec) and enroll.effective_state(rec) in gate.GATE_STATES
        st = enroll.store_for(rec, create=False) if rec else None
        # Health data on this box: a live enrollment, or a store kept after a
        # disconnect. Nobody else's history is touched.
        holds = live or st is not None
        if not holds and not has_refs:
            return messages
        now_ms = _now_ms() if now_ms is None else now_ms

        def allowed(cats) -> bool:
            return live and all(destinations.allowed(provider, c, rec, model=model)[0]
                                for c in cats)

        consented = [c for c in catalog.CATEGORIES
                     if ((rec or {}).get("categories") or {}).get(c) in catalog.CONSENTED]
        blanket = holds and not allowed(consented or catalog.CATEGORIES)
        withheld = frozenset()
        if st is not None:
            try:
                rows = [r for r in _recent_history_rows(st, now_ms)
                        if not allowed(r.get("categories") or catalog.CATEGORIES)]
                withheld = value_set(r.get("payload") for r in rows)
            except store_mod.StoreGone:
                pass
        out, changed = [], has_refs
        for m in messages:
            if not isinstance(m, dict) or m.get("role") != "assistant":
                out.append(_without_ref(m))
                continue
            ref = m.get("wellness_ref")
            new = m
            if ref:
                row = None
                if st is not None and isinstance(ref, str):
                    try:
                        row = st.get_delivery(ref)
                    except store_mod.StoreGone:
                        row = None
                cats = (row or {}).get("categories") or catalog.CATEGORIES
                if not allowed(cats):
                    values = value_set([(row or {}).get("payload")]) if row else frozenset()
                    new = _message_text_masked(m, values | withheld)
                elif withheld:
                    new = _message_text_masked(m, withheld, unit_rule=False)
            elif blanket:
                new = _message_text_masked(m, withheld)
            elif withheld:
                new = _message_text_masked(m, withheld, unit_rule=False)
            new = _without_ref(new)
            changed = changed or new != m
            out.append(new)
        return out if changed else messages
    except Exception as e:  # noqa: BLE001 - a failed scrub must not send the numbers
        print(f"[WELLNESS] history scrub failed ({type(e).__name__}); masking every assistant turn")
        return [_without_ref(_message_text_masked(m, frozenset()))
                if isinstance(m, dict) and m.get("role") == "assistant" else _without_ref(m)
                for m in messages]


def _recent_history_rows(st, now_ms: int, window_ms: int = HISTORY_WINDOW_MS) -> List[dict]:
    with st.read() as conn:
        rows = conn.execute("SELECT * FROM delivered WHERE ts>=? ORDER BY ts",
                            (now_ms - window_ms,)).fetchall()
    return [st._delivery(r) for r in rows]
