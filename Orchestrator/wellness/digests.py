"""Digests: the words-only weekly note (spec §3.7, "Words-only checkpoint
summaries", opt-in from M7.4).

A note is written from a template, never by a model, and carries COUNTS of
nights and days and no physiological value, so it is safe to mint into the
ledger with the WELLNESS tag ("when did my sleep slip?" then finds it):

    "Week of Oct 19: sleep below your usual range on 4 of 6 worn nights;
     steps within your usual range on 5 of 7 days."

Stored in the digest table (kind "week_note", period = the Monday of the
week). Deterministic: `now_ms` is only the computed_at stamp.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional

from Orchestrator.wellness import coverage as cov
from Orchestrator.wellness import rollups
from Orchestrator.wellness.store import Store, add_days

KIND_WEEK_NOTE = "week_note"
_MONTHS = ("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec").split()


def week_start(day: str) -> str:
    d = date.fromisoformat(day)
    return (d - timedelta(days=d.weekday())).isoformat()


def week_label(monday: str) -> str:
    d = date.fromisoformat(monday)
    return f"Week of {_MONTHS[d.month - 1]} {d.day}"


def _bands(conn, store: Store, metric: str, days: List[str], valid) -> Dict[str, int]:
    counts = {"below": 0, "within": 0, "above": 0, "n": 0}
    grain = rollups.BASELINE_METRICS[metric][0]
    rows = {r["bucket"]: r for r in store.rollups(grain, days[0], days[-1], metric=metric,
                                                  conn=conn)}
    for d in days:
        r = rows.get(d)
        if r is None or r["value"] is None or not valid(d):
            continue
        cmp = rollups.vs_usual(metric, r["value"], store.baseline(metric, d, conn=conn))
        if cmp is None:
            continue
        counts["n"] += 1
        counts[cmp["label"].split()[0]] += 1
    return counts


def _phrase(what: str, counts: Dict[str, int], unit: str, expected: int) -> Optional[str]:
    if counts["n"] == 0:
        return None
    band, k = max(((b, counts[b]) for b in ("below", "within", "above")),
                  key=lambda x: (x[1], x[0] == "within"))
    where = {"below": "below your usual range", "within": "within your usual range",
             "above": "above your usual range"}[band]
    return f"{what} {where} on {k} of {counts['n']} {unit}"


def week_note(conn, store: Store, monday: str) -> Optional[str]:
    days = [add_days(monday, i) for i in range(7)]
    cover = store.coverage_days(days[0], days[-1], conn=conn)
    worn = lambda d: (cover.get(d) or {}).get("night_worn") == 1  # noqa: E731
    wear = lambda d: ((cover.get(d) or {}).get("day_wear_h") or 0) >= cov.DAY_VALID_WEAR_H  # noqa: E731
    parts = [
        _phrase("sleep", _bands(conn, store, "sleep_duration", days, worn), "worn nights", 7),
        _phrase("steps", _bands(conn, store, "steps", days, wear), "days", 7),
    ]
    parts = [p for p in parts if p]
    if not parts:
        return None
    return f"{week_label(monday)}: " + "; ".join(parts) + "."


def refresh(conn, store: Store, days: Iterable[str], *, now_ms: int) -> int:
    """Rewrite the notes of every week a recomputed day falls in."""
    n = 0
    for monday in sorted({week_start(d) for d in days}):
        note = week_note(conn, store, monday)
        if note is None:
            conn.execute("DELETE FROM digest WHERE kind=? AND period=?", (KIND_WEEK_NOTE, monday))
            continue
        store.put_digest(conn, KIND_WEEK_NOTE, monday, {"text": note}, now_ms)
        n += 1
    return n
