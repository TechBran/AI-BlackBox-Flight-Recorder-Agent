"""The wellness catalog: the v1 metric set, default consent, display and
spoken strings (the model quotes these, never its own formatting), and the
two prompt texts."""
from __future__ import annotations

import pytest

from Orchestrator.wellness import catalog


def test_v1_metric_set_and_categories():
    assert set(catalog.METRICS) == {
        "sleep_duration", "sleep_stages", "sleeping_hr_median", "sleeping_hr_p10", "resting_hr",
        "heart_rate", "hrv", "spo2", "respiratory_rate", "steps", "distance", "active_calories",
        "total_calories", "exercise", "bp_systolic", "bp_diastolic"}
    assert {m.category for m in catalog.METRICS.values()} == set(catalog.CATEGORIES)
    assert catalog.CATEGORIES == ("sleep", "heart", "activity", "blood_pressure")


def test_default_consent():
    assert catalog.DEFAULT_CONSENT == {"sleep": "ask", "heart": "ask", "activity": "ask",
                                       "blood_pressure": "off"}
    assert catalog.DEFAULT_LOCAL_ONLY["blood_pressure"] is True
    assert not any(v for k, v in catalog.DEFAULT_LOCAL_ONLY.items() if k != "blood_pressure")


@pytest.mark.parametrize("n,words", [(0, "zero"), (7, "seven"), (21, "twenty-one"),
                                     (100, "one hundred"), (118, "one hundred eighteen"),
                                     (12300, "twelve thousand three hundred"),
                                     (1_000_001, "one million one")])
def test_number_words(n, words):
    assert catalog.number_words(n) == words


@pytest.mark.parametrize("minutes,display,spoken", [
    (365, "6 h 05 min", "about six hours"), (390, "6 h 30 min", "about six and a half hours"),
    (45, "45 min", "about forty-five minutes"), (61, "1 h 01 min", "about one hour"),
    (0.4, "0 min", "less than five minutes"), (3, "3 min", "about five minutes"),
])
def test_durations(minutes, display, spoken):
    assert catalog.duration_display(minutes) == display
    assert catalog.duration_spoken(minutes) == spoken


@pytest.mark.parametrize("metric,value,display,spoken", [
    ("sleeping_hr_median", 47.3, "47 bpm", "forty-seven beats per minute"),
    ("steps", 12345, "12,345 steps", "about twelve thousand three hundred steps"),
    ("distance", 7210, "7.2 km", "about seven kilometres"),
    ("distance", 820, "820 m", "about eight hundred twenty metres"),
    ("spo2", 96, "96 %", "ninety-six percent"),
    ("hrv", 42, "42 ms", "forty-two milliseconds"),
    ("respiratory_rate", 14.6, "14.6 breaths/min", "about fifteen breaths a minute"),
    ("active_calories", 521, "521 kcal", "about five hundred twenty calories"),
    ("bp_systolic", 118, "118 mmHg", "one hundred eighteen"),
])
def test_display_and_spoken(metric, value, display, spoken):
    assert catalog.display(metric, value) == display
    assert catalog.spoken(metric, value) == spoken


def test_gaps_and_helpers():
    assert catalog.display("steps", None) is None and catalog.spoken("steps", None) is None
    assert catalog.bp_display(118, 79) == "118/79 mmHg"
    assert catalog.bp_spoken(118, 79) == "one hundred eighteen over seventy-nine"
    assert catalog.delta_display("sleep_duration", -65) == "about 1 h 05 min less"
    assert catalog.delta_display("steps", 2100) == "about 2,100 more steps"
    assert catalog.delta_display("resting_hr", 4.2) == "about 4 bpm higher"
    assert catalog.delta_display("sleeping_hr_median", -0.2) == "about the same as usual"
    assert catalog.delta_display("sleep_duration", 0.3) == "about the same as usual"
    assert catalog.delta_display("respiratory_rate", 0.04) == "about the same as usual"
    assert catalog.delta_display("respiratory_rate", 0.4) == "about 0.4 breaths/min higher"
    assert catalog.clock(-20) == "23:40" and catalog.clock(390) == "06:30"
    assert catalog.exercise_type_name(56) == "run" and catalog.exercise_type_name(999) == "other"
    assert catalog.exercise_type_name("swim") == "swim"


def test_prompt_texts():
    words = len(catalog.DEFAULT_INSTINCT_PROMPT.split())
    assert 100 <= words <= 150
    for topic in ("sleep", "energy", "recovery", "training", "stress", "travel", "unwell",
                  "planning the day", "coverage", "as of"):
        assert topic in catalog.DEFAULT_INSTINCT_PROMPT
    assert "never volunteer" in catalog.DEFAULT_INSTINCT_PROMPT
    assert catalog.WELLNESS_RULES.startswith("WELLNESS RULES: Wellness information only.")
    assert "emergency services" in catalog.WELLNESS_RULES
    assert len(catalog.DEFAULT_INSTINCT_PROMPT) <= catalog.INSTINCT_PROMPT_MAX
