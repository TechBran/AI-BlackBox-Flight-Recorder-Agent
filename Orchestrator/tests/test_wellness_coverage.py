"""Wear, heart-rate mode, coverage objects and trouble states (spec §3.5):
the three HR modes, worn and unworn nights ("4 of 7"), day wear in 15- and
30-minute bins, the history-capped variant, and the phone-side troubles."""
from __future__ import annotations

import pytest

from Orchestrator.tests.wellness_support import fixtures, fresh_store, ingest_fixture
from Orchestrator.wellness import coverage as cov

FX = fixtures()
DAY = FX.day_of


@pytest.fixture(scope="module")
def stores(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    out = {}
    for name in ("continuous_120", "every_10_min_21", "manual_21"):
        out[name] = fresh_store(tmp_path_factory.mktemp(name), mp,
                                op_key={"continuous_120": "c" * 16, "every_10_min_21": "e" * 16,
                                        "manual_21": "a" * 16}[name])
        ingest_fixture(out[name], name)
    yield out
    mp.undo()


@pytest.mark.parametrize("name,mode", [("continuous_120", cov.MODE_CONTINUOUS),
                                       ("every_10_min_21", cov.MODE_TEN_MIN),
                                       ("manual_21", cov.MODE_MANUAL)])
def test_three_hr_modes(stores, name, mode):
    days = stores[name].coverage_days(DAY(-3), DAY(0))
    assert {d["hr_mode"] for d in days.values()} == {mode}


def test_manual_mode_is_a_trouble_and_has_no_sleeping_hr(stores):
    st = stores["manual_21"]
    day = st.coverage_days(DAY(-2), DAY(-2))[DAY(-2)]
    assert "hr_mode_manual" in day["troubles"] and day["night_worn"] == 0
    assert st.rollups("night", DAY(-20), DAY(0), metric="sleeping_hr_median") == []
    assert st.rollups("night", DAY(-20), DAY(0), metric="sleep_duration")      # still recorded


def test_worn_and_unworn_nights_last_seven(stores):
    days = stores["continuous_120"].coverage_days(DAY(-6), DAY(0))
    worn = [days[DAY(i)]["night_worn"] for i in range(-6, 1)]
    assert worn == [None, 1, 0, 1, 1, None, 1]
    n = sum(1 for w in worn if w == 1)
    assert cov.nights_label(n, 7) == "worn 4 of 7 nights"


def test_ten_minute_mode_counts_a_night_as_worn(stores):
    """At one sample every 10 minutes a 5-minute bin is half empty by
    construction; the night uses 10-minute bins in that mode."""
    days = stores["every_10_min_21"].coverage_days(DAY(-3), DAY(-2))
    assert days[DAY(-3)]["night_worn"] == 1 and days[DAY(-2)]["night_worn"] == 1


def test_day_wear_hours(stores):
    days = stores["continuous_120"].coverage_days(DAY(-9), DAY(-8))
    assert days[DAY(-9)]["day_wear_h"] < cov.DAY_VALID_WEAR_H       # off 12:00-19:30
    assert days[DAY(-8)]["day_wear_h"] == pytest.approx(16.0, abs=0.01)
    ten = stores["every_10_min_21"].coverage_days(DAY(-8), DAY(-8))[DAY(-8)]
    assert ten["day_wear_h"] == pytest.approx(16.0, abs=0.01)         # 30-minute bins
    manual = stores["manual_21"].coverage_days(DAY(-8), DAY(-8))[DAY(-8)]
    assert manual["day_wear_h"] < 2


def test_today_is_a_partial_day(stores):
    st = stores["continuous_120"]
    today = st.rollups("day", DAY(0), DAY(0), metric="steps")[0]
    assert today["coverage"]["label"].endswith("so far today")
    assert today["coverage"]["n_expected"] == (FX.PUSH_HOUR - cov.DAY_WEAR_START_H) * 4


def test_coverage_object_shape():
    c = cov.coverage(4, 7, "worn 4 of 7 nights", "2026-09-01")
    assert c == {"n_used": 4, "n_expected": 7, "pct": 57, "label": "worn 4 of 7 nights",
                 "earliest_available": "2026-09-01"}
    assert cov.coverage(None, None, "no heart rate recorded")["pct"] is None


def test_labels():
    assert cov.nights_label(7, 7) == "worn all 7 nights"
    assert cov.nights_label(1, 1) == "worn all night"
    assert cov.nights_label(0, 1) == "not worn"
    assert cov.night_coverage_label(72.4) == "heart rate on 72% of the night"
    assert cov.night_coverage_label(None) == "no sleep recorded"


def test_history_capped_variant_carries_earliest_available(tmp_path, monkeypatch):
    st = fresh_store(tmp_path, monkeypatch)
    earliest = DAY(-29)
    ingest_fixture(st, "history_capped_30", earliest_available=earliest)
    rows = st.rollups("day", DAY(-5), DAY(-5), metric="steps")
    assert rows[0]["coverage"]["earliest_available"] == earliest
    span = st.data_span()
    assert span[0] == DAY(-30)       # the evening before the first day
    capped = st.coverage_days(DAY(-30), DAY(-30))
    assert capped[DAY(-30)]["history_capped"] == 1


def test_phone_troubles_from_the_capability_report():
    report = FX.load("continuous_120")["capabilities"]
    consent = {"sleep": "ask", "heart": "ask", "activity": "ask", "blood_pressure": "ask"}
    assert cov.phone_troubles(report, consent) == []
    capped = FX.load("history_capped_30")["capabilities"]
    assert cov.phone_troubles(capped, consent) == ["history_window_capped"]
    revoked = {**report, "health_connect": {
        **report["health_connect"],
        "granted": [g for g in report["health_connect"]["granted"]
                    if not g.endswith(("READ_SLEEP", "READ_HEALTH_DATA_IN_BACKGROUND"))]}}
    assert cov.phone_troubles(revoked, consent) == ["permission_revoked:sleep", "foreground_only"]
    # A category that is off cannot be "revoked".
    assert cov.phone_troubles(revoked, {**consent, "sleep": "off"}) == ["foreground_only"]
    assert cov.phone_troubles(None, consent) == []


def test_every_trouble_has_fixed_wording():
    for code in list(cov.TROUBLE_TEXT) + ["permission_revoked:blood_pressure"]:
        t = cov.trouble(code, last_sync="06:10")
        assert t["title"] and t["body"] and t["fix"] and t["code"] == code
        assert "{" not in t["body"]
    assert "blood pressure" in cov.trouble("permission_revoked:blood_pressure")["body"]
    for t in cov.TROUBLE_TEXT.values():
        for word in ("dangerous", "alarming", "critical", "warning", "abnormal"):
            assert word not in " ".join(t).lower()


def test_stale_and_not_worn_rules():
    now = 1_800_000_000_000
    assert cov.is_stale(None, now)
    assert not cov.is_stale("2027-01-15T07:00:00Z", 1_800_000_000_000)
    assert cov.not_worn_likely([None, 0, 1], worn_before=True)
    assert not cov.not_worn_likely([1, 0, 1], worn_before=True)
    assert not cov.not_worn_likely([None, None, None], worn_before=False)
