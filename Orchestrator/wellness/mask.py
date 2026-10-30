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
     oz, °F/°C, HRV, ms, breaths, and h/hours/min near sleep or exercise words)
     becomes "[·]";
  3. the [REASONING] block becomes "[REASONING omitted: wellness turn]".

The user's own words are never passed in: they stay verbatim (they chose to
say them; the policy discloses it). Over-masking is the safe direction.

The durable carry (W28): the rows live in the operator's store, so a save that
arrives after a restart still finds them. A client that echoes the turn key
(stream_start's `wellness_turn`) is matched exactly; one that does not is
masked against every delivery of the last 24 hours not yet masked (plus those
masked in the last half hour, for a concurrent turn whose save lands second).

scrub_history() is the provider-switch rule (A3b): earlier assistant bubbles a
client sends back never carry health numbers to a provider the operator did
not allow.
"""
from __future__ import annotations

import dataclasses
import math
import re
import time
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from Orchestrator.wellness import catalog

MASK = "[·]"
REASONING_OMITTED = "[REASONING omitted: wellness turn]"
TOOLS_LINE = "- wellness: [numbers kept in the health store]"
UNMATCHED_WINDOW_MS = 24 * 3600 * 1000
CONCURRENT_GRACE_MS = 30 * 60 * 1000
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


def _number_forms(x: float) -> Set[str]:
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
        out.add(f"{x:.1f}")
        out.add(f"{x:.2f}".rstrip("0").rstrip("."))
        whole = int(abs(x))
        tenth = int(round((abs(x) - whole) * 10)) % 10
        out |= {f"{w} point {catalog.number_words(tenth)}" for w in _words(whole)}
    if abs(x) >= 1000:
        out |= {f"{r:,}", f"{r:,}".replace(",", " "), f"{r:,}".replace(",", ".")}
        for nd in (-1, -2):
            rr = int(round(x, nd))
            out |= {str(rr), f"{rr:,}"}
            out |= _words(rr)
        k = x / 1000.0
        out |= {f"{k:.1f}k", f"{k:.1f} thousand", f"{k:g}k"}
    return {v for v in out if v and not _SCI.search(v)}


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
            out |= {f"{dec} hours", f"{dec} h", f"{dec} hrs", f"{dec} hr", dec}
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


def value_set(payloads: Iterable) -> frozenset:
    """V for a turn: every value-bearing string the delivered payloads hold."""
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
            values.update(_number_forms(node))
            if unit == "min":
                values.update(_duration_forms(node))
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

_UNIT_WORDS = frozenset("""
bpm beat beats heartbeat heartbeats pulse heart hr rhr hrv rmssd step steps mmhg hg spo2 spo₂
oxygen saturation percent percentage kg kgs kilo kilos kilogram kilograms lb lbs pound pounds
kcal cal cals calorie calories ml oz ounce ounces °f °c degree degrees ms millisecond
milliseconds breath breaths breathing respiratory systolic diastolic bp pressure glucose weight
temperature km kms kilometre kilometres kilometer kilometers mile miles mi metre metres meter
meters vo2 vo2max resting
""".split())
_TIME_WORDS = frozenset("h hr hrs hour hours min mins minute minutes am pm".split())
_CONTEXT_WORDS = frozenset("""
sleep sleeps slept sleeping asleep nap naps napped bed bedtime woke wake waking awake night
nights overnight tonight rem deep run ran running runs walk walked walking walks workout
workouts exercise exercised exercising active activity cycling cycled ride rode swim swam
swimming training trained jog jogged jogging hike hiked
""".split())
_NUMBER_WORDS = frozenset("""
zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen
sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety
hundred thousand million half dozen
""".split())
_JOINERS = frozenset({"and", "point", "a"})

_TOKEN_RE = re.compile(
    r"(?P<num>\d+(?:[.,:/]\d+)*(?:k(?![A-Za-z]))?)"
    r"|(?P<word>(?i:spo2|spo₂|vo2max|vo2)|[A-Za-z°₂]+(?:['’-][A-Za-z]+)*)"
    r"|(?P<pct>%)"
    r"|(?P<other>[^\sA-Za-z\d])")


def _is_number_word(word: str) -> bool:
    parts = word.lower().replace("’", "'").split("-")
    return all(p in _NUMBER_WORDS for p in parts) and bool(parts)


def _unit_rule_spans(text: str) -> List[Tuple[int, int]]:
    toks = []
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup
        low = m.group(0).lower()
        if kind == "num":
            cls = "num"
        elif kind == "pct":
            cls = "unit"
        elif kind == "word":
            if _is_number_word(low):
                cls = "num"
            elif low in _UNIT_WORDS:
                cls = "unit"
            elif low in _TIME_WORDS:
                cls = "time"
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
        t[0] == "time" and any(abs(i - c) <= CONTEXT_DISTANCE for c in contexts))]
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


def _value_regex(values: frozenset):
    if not values:
        return None
    alts = sorted(values, key=len, reverse=True)
    body = "|".join(re.escape(v) for v in alts)
    # Not inside a longer number ("147", "47.35", "12,345") or a clock time
    # (clocks are members of their own), but a part of a pair is a hit on its
    # own: "118/79" masks as "[·]/[·]".
    return re.compile(r"(?<![\w.,:])(?:" + body + r")(?![\w]|[.,:]\d)", re.I)


def mask_text(text: str, values: frozenset = frozenset()) -> str:
    """`text` with every member of `values` and every health-unit number masked."""
    if not text:
        return text
    rx = _value_regex(values)
    out = rx.sub(MASK, text) if rx is not None else text
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
    """One turn's wellness results: what to mask its text against, and its tag."""
    operator: str
    rows: Tuple[dict, ...]
    values: frozenset
    extra_categories: Tuple[str, ...] = ()
    op_key: Optional[str] = None

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
        return [x for x in dict.fromkeys(out) if x] or (["on-device"] if self.extra_categories else [])

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
        happened; an unstamped row only widens a later fallback)."""
        if not self.refs:
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


def _recent_rows(st, now_ms: int) -> List[dict]:
    """Deliveries of the last 24 h not yet masked, plus those masked in the
    last CONCURRENT_GRACE_MS (a concurrent turn whose save lands second)."""
    with st.read() as conn:
        rows = conn.execute(
            "SELECT * FROM delivered WHERE ts>=? AND (masked_at IS NULL OR masked_at>=?) "
            "ORDER BY ts", (now_ms - UNMATCHED_WINDOW_MS, now_ms - CONCURRENT_GRACE_MS)).fetchall()
    return [st._delivery(r) for r in rows]


def _distinctive(values: frozenset) -> List[str]:
    """The members of V specific enough to say a text quotes a health result:
    a decimal, grouped thousands or a pair ("47.3", "12,345", "118/79"), or a
    number with its unit ("47 bpm", "6 h 47 min", "forty-seven beats per
    minute"). A bare "47" or a clock time ("12:30") alone is not."""
    out = []
    for v in values:
        if re.search(r"\d[.,/]\d", v):
            out.append(v)
            continue
        words = [t.lower() for t in re.findall(r"[A-Za-z%]+|\d+", v)]
        if len(words) > 1 and any(w in _UNIT_WORDS or w in _TIME_WORDS or w == "%" for w in words) \
                and any(w.isdigit() or _is_number_word(w) for w in words):
            out.append(v)
    return out


def quotes_results(text: str, values: frozenset) -> bool:
    """Does `text` repeat a delivered health result (a distinctive member of V)?"""
    if not text or not values:
        return False
    rx = _value_regex(frozenset(_distinctive(values)))
    return bool(rx is not None and rx.search(text))


def carry_for(operator: Optional[str], *, turn_key: Optional[str] = None, fallback: bool = False,
              extra_categories: Sequence[str] = (), recall_text: Optional[str] = None,
              now_ms: Optional[int] = None) -> Optional[Carry]:
    """The turn's carry, or None for a turn no wellness result reached.

    `turn_key` matches the turn's own rows; `fallback` (a client that echoes no
    key) takes every recent unmasked delivery instead. `extra_categories` marks
    a turn as wellness even with no rows (an on-device wellness tool call).

    `recall_text` (the assistant text) closes the recall gap: a later turn of
    the same chat sends the earlier, unmasked bubbles back to the model as
    history, so a reply can repeat a result no tool delivered this turn. When
    it quotes one of the operator's results of the last 72 hours, the turn is
    masked and tagged against all of them (over-masking is the safe way)."""
    if not operator:
        return None
    from Orchestrator.wellness import enroll, store as store_mod
    now_ms = _now_ms() if now_ms is None else now_ms
    rec = enroll.get_enrollment(operator)
    st = enroll.store_for(rec, create=False) if rec else None
    rows: List[dict] = []
    values = frozenset()
    if st is not None:
        try:
            if turn_key:
                rows = st.deliveries_for_turn(turn_key)
            if not rows and fallback:
                rows = _recent_rows(st, now_ms)
            if rows:
                values = value_set(r.get("payload") for r in rows)
            elif recall_text:
                recent = _recent_history_rows(st, now_ms)
                recent_values = value_set(r.get("payload") for r in recent)
                if quotes_results(recall_text, recent_values):
                    rows, values = recent, recent_values
        except store_mod.StoreGone:
            rows = []
    if not rows and not extra_categories:
        return None
    return Carry(operator, tuple(rows), values, tuple(extra_categories), (rec or {}).get("op_key"))


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


# ------------------------------------------------------------------ history scrub (A3b)

def _message_text_masked(message: dict, values: frozenset) -> dict:
    content = message.get("content")
    if isinstance(content, str):
        return {**message, "content": mask_text(content, values)}
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append({**part, "text": mask_text(part["text"], values)})
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

    An earlier assistant message tagged `wellness_ref` (clients echo it from
    the wellness_used frame) is masked against its stored payload when the
    provider is not allowed for that ref's categories. With no ref, every
    client-supplied assistant message is masked (the unit rule, plus the
    operator's recent deliveries) whenever an enrolled operator is on a
    provider not allowed for one of their consented categories. User messages
    stay verbatim. The `wellness_ref` key never reaches the provider. A list
    with nothing to change is returned as the SAME object."""
    if not isinstance(messages, list):
        return messages
    has_refs = any(isinstance(m, dict) and "wellness_ref" in m for m in messages)
    try:
        from Orchestrator.wellness import destinations, enroll, gate, store as store_mod
        rec = enroll.get_enrollment(operator) if operator else None
        live = bool(rec) and enroll.effective_state(rec) in gate.GATE_STATES
        if not live and not has_refs:
            return messages
        st = enroll.store_for(rec, create=False) if rec else None
        now_ms = _now_ms() if now_ms is None else now_ms

        def allowed(cats) -> bool:
            return bool(rec) and all(destinations.allowed(provider, c, rec, model=model)[0]
                                     for c in cats)

        consented = [c for c in catalog.CATEGORIES
                     if ((rec or {}).get("categories") or {}).get(c) in catalog.CONSENTED]
        blanket = live and not allowed(consented or catalog.CATEGORIES)
        recent_values = frozenset()
        if blanket and st is not None:
            try:
                rows = [r for r in _recent_history_rows(st, now_ms)
                        if not allowed(r.get("categories") or catalog.CATEGORIES)]
                recent_values = value_set(r.get("payload") for r in rows)
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
                    new = _message_text_masked(m, values | recent_values)
            elif blanket:
                new = _message_text_masked(m, recent_values)
            new = _without_ref(new)
            changed = changed or new != m
            out.append(new)
        return out if changed else messages
    except Exception as e:  # noqa: BLE001 - a failed scrub must not send the numbers
        print(f"[WELLNESS] history scrub failed ({type(e).__name__}); masking every assistant turn")
        return [_without_ref(_message_text_masked(m, frozenset()))
                if isinstance(m, dict) and m.get("role") == "assistant" else _without_ref(m)
                for m in messages]


def _recent_history_rows(st, now_ms: int) -> List[dict]:
    with st.read() as conn:
        rows = conn.execute("SELECT * FROM delivered WHERE ts>=? ORDER BY ts",
                            (now_ms - HISTORY_WINDOW_MS,)).fetchall()
    return [st._delivery(r) for r in rows]
