"""The deterministic answers behind the future wellness_* tools: envelopes,
display/spoken strings, usual ranges, coverage, gaps as nulls, caps and
coarsening (never truncation), history <= 92 days, ref ids, delivered rows,
and categories narrowed by consent."""
from __future__ import annotations

import json
import re

import pytest

from Orchestrator.tests.wellness_support import OPERATOR, enrolled_fixture, fixtures
from Orchestrator.wellness import api

FX = fixtures()
DAY = FX.day_of
NOW = FX.now_ms()
DELIVERY = api.Delivery(turn_key="wt_test0001", surface="android", provider="anthropic",
                        model="test-model")
REF_RE = re.compile(r"w-\d{8}-[0-9a-f]{8}")


@pytest.fixture
def box(tmp_path, monkeypatch):
    rec, st, data = enrolled_fixture(tmp_path, monkeypatch)
    return st


def _metric(env, name):
    return next(m for m in env["metrics"] if m["metric"] == name)


def test_last_night_summary_envelope(box):
    env = api.summary(OPERATOR, "last_night", now_ms=NOW)
    assert env["ok"] is True and env["tool"] == "wellness_summary"
    assert REF_RE.fullmatch(env["ref"]) and env["ref"].startswith("w-20261028-")
    assert env["period"]["label"] == "last night" and env["period"]["tz"] == "UTC-04:00"
    assert env["stale"] is False and env["as_of"].startswith("2026-10-28T11:55")
    sleep = _metric(env, "sleep_duration")
    assert sleep["display"].endswith(" min") and " h " in sleep["display"]
    assert sleep["spoken"].startswith("about ") and "hour" in sleep["spoken"]
    assert sleep["usual"]["n"] >= 14 and sleep["vs_usual"]["label"].endswith("usual range")
    assert sleep["coverage"]["label"] in ("worn all night",) or "heart rate on" in \
        sleep["coverage"]["label"]
    assert sleep["bedtime"] and sleep["wake_time"] and sleep["night_of"] == DAY(-1)
    stages = _metric(env, "sleep_stages")
    assert set(stages["parts"]) == {"light", "deep", "rem", "awake"}
    assert {"sleeping_hr_median", "resting_hr"} <= {m["metric"] for m in env["metrics"]}
    assert not {"steps", "bp_systolic"} & {m["metric"] for m in env["metrics"]}
    assert api.NOTE_STAGES in env["notes"]
    assert env["sources"] and env["sources"][0]["label"] == "Watch via Samsung Health"


def test_sentinel_night_display(box):
    env = api.summary(OPERATOR, "custom", start=DAY(-5), end=DAY(-5), now_ms=NOW)
    hr = _metric(env, "sleeping_hr_median")
    assert hr["value"] == 47.3 and hr["display"] == "47 bpm"
    assert hr["spoken"] == "forty-seven beats per minute"
    assert hr["method"] == "BlackBox estimate from 5-minute averages"


def test_unworn_night_is_a_gap_with_a_reason(box):
    env = api.summary(OPERATOR, "custom", start=DAY(-6), end=DAY(-6), now_ms=NOW)
    sleep = _metric(env, "sleep_duration")
    assert sleep["value"] is None and sleep["display"] == "not recorded"
    gap = next(g for g in env["gaps"] if g["metric"] == "sleep_duration")
    assert gap["reason"] == "no sleep recorded"
    phone_night = api.summary(OPERATOR, "custom", start=DAY(-4), end=DAY(-4), now_ms=NOW)
    gap = next(g for g in phone_night["gaps"] if g["metric"] == "sleeping_hr_median")
    assert gap["reason"] == "watch probably not worn"


def test_week_summary_coverage_four_of_seven(box):
    env = api.summary(OPERATOR, "last_7_days", now_ms=NOW)
    sleep = _metric(env, "sleep_duration")
    assert sleep["coverage"]["label"] == "worn 4 of 7 nights"
    assert sleep["coverage"]["n_used"] == 4 and sleep["coverage"]["n_expected"] == 7
    steps = _metric(env, "steps")
    assert steps["total_display"].endswith(" steps") and steps["days_with_data"] == 7
    assert "days with 10 h of wear" in steps["coverage"]["label"]
    assert env["sessions"] and all("duration" in s for s in env["sessions"])
    assert any(s["type"] == "run" for s in env["sessions"])


def test_blood_pressure_only_when_consented(tmp_path, monkeypatch):
    rec, st, data = enrolled_fixture(tmp_path, monkeypatch)
    env = api.summary(OPERATOR, "yesterday", now_ms=NOW)
    assert "blood_pressure" not in env["categories"]          # off by default
    assert not [m for m in env["metrics"] if m["category"] == "blood_pressure"]
    from Orchestrator.tests.wellness_support import write_record
    write_record(categories={"sleep": "ask", "heart": "ask", "activity": "ask",
                             "blood_pressure": "ask"},
                 last_push_at=rec["last_push_at"])
    env = api.summary(OPERATOR, "custom", start=DAY(-2), end=DAY(-2), now_ms=NOW)
    sys_ = _metric(env, "bp_systolic")
    assert sys_["readings"] == 4 and sys_["unit"] == "mmHg"


def test_categories_argument_narrows(box):
    env = api.summary(OPERATOR, "today", categories=["activity"], now_ms=NOW)
    assert env["categories"] == ["activity"]
    assert {m["category"] for m in env["metrics"]} == {"activity"}
    today_steps = _metric(env, "steps")
    assert today_steps["coverage"]["label"].endswith("so far today")


def test_not_enrolled_and_bad_arguments(tmp_path, monkeypatch):
    from Orchestrator.tests.wellness_support import sandbox
    sandbox(tmp_path, monkeypatch)
    env = api.summary(OPERATOR, "today", now_ms=NOW)
    assert env == {"ok": False, "tool": "wellness_summary", "error": "not_enrolled",
                   "fix": env["fix"]} and "Settings, Health" in env["fix"]


def test_bad_period_and_range(box):
    assert api.summary(OPERATOR, "forever", now_ms=NOW)["error"] == "invalid_period"
    long = api.summary(OPERATOR, "custom", start=DAY(-100), end=DAY(0), now_ms=NOW)
    assert long["error"] == "invalid_range"
    assert api.series(OPERATOR, "nonsense", "last_7_days", now_ms=NOW)["error"] == \
        "unknown_metric"
    assert api.history(OPERATOR, DAY(-100), DAY(0), now_ms=NOW)["error"] == "invalid_range"


def test_series_day_points_with_nulls(box):
    env = api.series(OPERATOR, "sleeping_hr_median", "last_30_days", now_ms=NOW)
    assert env["granularity_used"] == "day" and len(env["points"]) == 30
    by_day = {p["t"]: p for p in env["points"]}
    assert by_day[DAY(-6)]["value"] is None and by_day[DAY(-6)]["coverage_pct"] == 0
    assert by_day[DAY(-5)]["value"] == 47.3 and by_day[DAY(-5)]["display"] == "47 bpm"
    assert env["baseline"]["n"] >= 14


def test_series_auto_granularity_and_point_cap(box):
    hours = api.series(OPERATOR, "heart_rate", "last_24_hours", now_ms=NOW)
    assert hours["granularity_used"] == "hour" and len(hours["points"]) == 24
    year = api.series(OPERATOR, "steps", "last_365_days", now_ms=NOW)
    assert len(year["points"]) <= api.MAX_POINTS
    assert year["granularity_used"] in ("month", "week")
    fine = api.series(OPERATOR, "heart_rate", start=DAY(-2), end=DAY(-2), granularity="5min",
                      now_ms=NOW)
    assert fine["granularity_used"] == "hour"     # 5min only for windows of 3 h or less


def test_sessions_newest_first_with_types(box):
    env = api.sessions(OPERATOR, "exercise", "last_7_days", now_ms=NOW)
    assert env["sessions"] and env["sessions"][0]["start"] >= env["sessions"][-1]["start"]
    run = api.sessions(OPERATOR, "exercise", "last_7_days", exercise_type="run", now_ms=NOW)
    assert len(run["sessions"]) == 1
    r = run["sessions"][0]
    assert r["type"] == "run" and r["duration"] == "45 min"
    assert r["distance"] == "7.2 km" and r["hr_coverage_pct"] == 100
    assert r["session_ref"].startswith("s-")
    sleep = api.sessions(OPERATOR, "sleep", "last_7_days", limit=3, now_ms=NOW)
    assert len(sleep["sessions"]) == 3 and "stages_min" in sleep["sessions"][0]
    assert sleep["total_in_window"] >= 5
    assert api.sessions(OPERATOR, "exercise", "last_7_days", exercise_type="ski",
                        now_ms=NOW)["error"] == "invalid_argument"


def test_history_table(box):
    env = api.history(OPERATOR, DAY(-91), DAY(0), now_ms=NOW)
    assert env["ok"] and env["resolution"] == "day" and len(env["rows"]) == 92
    cols = env["columns"]
    assert cols[0] == "day" and "steps" in cols and "bp_systolic" not in cols
    row = dict(zip(cols, env["rows"][-4]))
    assert row["day"] == DAY(-3) and row["steps"] == 12345
    unworn = dict(zip(cols, env["rows"][-7]))
    assert unworn["sleep_duration"] is None and unworn["night_worn"] is None


def test_sources_has_no_values(box):
    env = api.sources(OPERATOR, now_ms=NOW)
    assert env["ok"] and env["wear"] == {"hr_mode": "continuous", "nights_worn_7": 4}
    text = json.dumps(env)
    for sentinel in FX.SENTINEL_STRINGS:
        assert sentinel not in text
    assert {"category": "blood_pressure", "consent": "off", "local_only": True,
            "days_with_data_28": None} in env["categories"]


def test_coarsening_never_truncates(box):
    env = api.history(OPERATOR, DAY(-91), DAY(0), now_ms=NOW, budget_tokens=900)
    assert env["coarsened"] is True and env["resolution"] in ("week", "month")
    assert api.estimate_tokens(env) <= 900
    json.loads(json.dumps(env))
    assert api.NOTE_COARSENED in env["notes"]
    big = api.series(OPERATOR, "heart_rate", "last_7_days", granularity="hour", now_ms=NOW,
                     budget_tokens=400)
    assert big["coarsened"] and len(big["points"]) < 120 and api.estimate_tokens(big) <= 400


def test_a_summary_fits_the_on_device_cap(box):
    """A 30-day summary of every category is over 1,500 tokens whole; it is
    compacted (still whole JSON, every metric with its coverage), and at a
    tiny budget it names what it left out instead of being cut."""
    full = api.summary(OPERATOR, "last_30_days", now_ms=NOW, budget_tokens=100_000)
    assert api.estimate_tokens(full) > api.CAP_TOKENS["on_device"]
    env = api.summary(OPERATOR, "last_30_days", now_ms=NOW,
                      budget_tokens=api.CAP_TOKENS["on_device"])
    assert api.estimate_tokens(env) <= api.CAP_TOKENS["on_device"]
    assert env["coarsened"] is True and "over_budget" not in env
    assert {m["metric"] for m in env["metrics"]} == {m["metric"] for m in full["metrics"]}
    for m in env["metrics"]:
        assert m["display"] and isinstance(m["coverage"], dict) and m["coverage"]["label"]
    tiny = api.summary(OPERATOR, "last_30_days", now_ms=NOW, budget_tokens=300)
    json.loads(json.dumps(tiny))
    assert tiny["metrics_omitted"] and api.NOTE_OMITTED in tiny["notes"]
    kept = [m["metric"] for m in tiny["metrics"]]
    assert "sleep_duration" in kept and "total_calories" not in kept
    assert api.estimate_tokens(tiny) <= 300 or tiny["over_budget"] is True


def test_an_answer_that_cannot_fit_says_so(box):
    env = api.history(OPERATOR, DAY(-91), DAY(0), now_ms=NOW, budget_tokens=50)
    json.loads(json.dumps(env))
    assert env["over_budget"] is True and env["rows"]


def test_caps_by_destination():
    assert api.budget_for("anthropic") == 8000
    assert api.budget_for("local") == 1500


def test_every_call_with_a_turn_writes_one_delivered_row(box):
    env = api.summary(OPERATOR, "last_night", now_ms=NOW, delivery=DELIVERY)
    rows = box.deliveries_for_turn("wt_test0001")
    assert len(rows) == 1 and rows[0]["ref"] == env["ref"]
    assert rows[0]["provider"] == "anthropic" and rows[0]["tool"] == "wellness_summary"
    assert rows[0]["categories"] == ["activity", "heart", "sleep"]
    assert rows[0]["payload"]["ref"] == env["ref"]
    api.series(OPERATOR, "steps", "last_7_days", now_ms=NOW, delivery=DELIVERY)
    assert len(box.deliveries_for_turn("wt_test0001")) == 2
    api.sources(OPERATOR, now_ms=NOW)                       # no turn: nothing recorded
    assert box.disclosures_count() == 2


def test_refs_are_unique(box):
    refs = {api.summary(OPERATOR, "today", now_ms=NOW, delivery=DELIVERY)["ref"]
            for _ in range(15)}
    assert len(refs) == 15


def test_stale_and_paused_are_stated(tmp_path, monkeypatch):
    rec, st, data = enrolled_fixture(tmp_path, monkeypatch, state="paused")
    env = api.summary(OPERATOR, "today", now_ms=NOW + 9 * 3_600_000)
    assert env["paused"] is True and api.NOTE_PAUSED in env["notes"]
    assert env["stale"] is True
    assert any(t["code"] == "stale_sync" for t in env["troubles"]) is False  # paused, not stale


def test_a_store_erased_mid_answer_reads_as_not_set_up(box, monkeypatch):
    def gone(*a, **k):
        raise api.store_mod.StoreGone("x")
    monkeypatch.setattr(api.store_mod.Store, "coverage_days", gone)
    env = api.summary(OPERATOR, "last_7_days", now_ms=NOW)
    assert env == {"ok": False, "tool": "wellness_summary", "error": "not_enrolled",
                   "fix": api.NOT_ENROLLED_FIX}


# ------------------------------------------------------------------ local-time grids and gaps

def test_last_24_hours_points_sit_on_local_hours(box):
    """Asked at 12:37, the hourly points start on the hour (the hour that
    holds 12:37 yesterday, partly outside the window), and each holds the
    bucket of the hour it names."""
    now = NOW + 37 * 60_000
    env = api.series(OPERATOR, "steps", "last_24_hours", now_ms=now)
    labels = [p["t"] for p in env["points"]]
    assert all(t.endswith(":00-04:00") for t in labels), labels[:3]
    assert labels[0] == "2026-10-27T12:00-04:00" and labels[-1] == "2026-10-28T12:00-04:00"
    want = {}
    for b in FX.load("continuous_120")["batches"]:
        for g in b["buckets"]:
            if g["metric"] == "steps":
                want.update({r[0]: r[1] for r in g["rows"]})
    for p in env["points"]:
        day = -1 if p["t"].startswith("2026-10-27") else 0
        assert p["value"] == want.get(FX.ms(day, int(p["t"][11:13]))), p
    hr = api.series(OPERATOR, "heart_rate", "last_24_hours", now_ms=now)
    assert hr["points"][0]["t"] == "2026-10-27T12:00-04:00"


def test_a_gap_after_the_last_push_reads_not_synced_yet(box):
    """The last push came at 21:00 local (01:00 the next day in UTC); a day
    after it has not been synced, whatever the UTC date says."""
    from Orchestrator.wellness import enroll
    with enroll.editing() as data:
        data[OPERATOR]["last_push_at"] = enroll.iso(FX.ms(0, 21))
    env = api.summary(OPERATOR, "today", now_ms=FX.ms(1, 9))
    reasons = {g["reason"] for g in env["gaps"]}
    assert env["gaps"] and reasons == {"not synced yet"}, env["gaps"]


def test_troubles_read_local_nights_and_local_clock_times(box):
    """At 21:00 local the UTC date is tomorrow: the last 3 nights are still
    tonight's three (worn, not worn, worn: no "not worn" trouble), and the
    stale-sync wording names the local time of the last push."""
    from Orchestrator.wellness import enroll
    rec = enroll.get_enrollment(OPERATOR)
    details = enroll.troubles_for(rec, FX.ms(0, 21))
    codes = [t["code"] for t in details]
    assert "not_worn_likely" not in codes and "stale_sync" in codes
    stale = next(t for t in details if t["code"] == "stale_sync")
    assert "since 11:55." in stale["body"], stale["body"]
    env = api.summary(OPERATOR, "today", now_ms=FX.ms(0, 21))
    assert "not_worn_likely" not in [t["code"] for t in env["troubles"]]
