"""The wellness fixture harness (spec M7.1): the committed payload files are
exactly what the deterministic generator builds, every batch of every variant
is a valid bbx.wellness.ingest/1 batch, and the variants hold what the other
suites rely on (sentinels, HR modes, unworn nights, the run, BP)."""
from __future__ import annotations

import gzip
import json

import pytest

from Orchestrator.tests.wellness_support import fixtures
from Orchestrator.wellness import schema

FX = fixtures()


@pytest.mark.parametrize("name", sorted(FX.BASE_VARIANTS))
def test_committed_files_equal_the_generator(name):
    path = FX.committed_path(name)
    assert path.is_file(), f"run python {FX.__file__} to write {path.name}"
    committed = json.loads(gzip.decompress(path.read_bytes()))
    assert committed == json.loads(json.dumps(FX.build(name)))


def test_fixture_files_stay_small():
    total = sum(FX.committed_path(n).stat().st_size for n in FX.BASE_VARIANTS)
    assert total < 512 * 1024


def test_generation_is_deterministic():
    assert FX.encode(FX.build("manual_21")) == FX.encode(FX.build("manual_21"))


@pytest.mark.parametrize("name", FX.VARIANTS)
def test_every_batch_is_a_valid_ingest_batch(name):
    data = FX.load(name)
    assert data["operator"] == "wellness-fixture" and data["batches"]
    for b in FX.for_enrollment(data, "en_0f1c0f1c", 1):
        schema.IngestBatch.model_validate(b)
    schema.CapabilityReport.model_validate(data["capabilities"])


def test_views_share_values_with_their_base():
    base = FX.load("continuous_120")["batches"][0]
    view = FX.load("days_10")["batches"][0]
    assert view["buckets"][0]["rows"][:5] == base["buckets"][0]["rows"][:5]
    assert FX.load("history_capped_30")["history"] is False
    granted = FX.load("history_capped_30")["capabilities"]["health_connect"]["granted"]
    assert not any(g.endswith("READ_HEALTH_DATA_HISTORY") for g in granted)


def test_the_fixture_holds_what_the_suites_need():
    data = FX.load("continuous_120")
    sessions = [s for b in data["batches"] for s in b["sessions"]]
    readings = [r for b in data["batches"] for r in b["readings"]]
    assert sum(1 for s in sessions if s["kind"] == "exercise" and s["exercise_type"] == 56) == 1
    bp = [r for r in readings if r["metric"] == "blood_pressure"]
    assert len(bp) == 4 * 21 - 2      # day 0 stops at noon: no evening readings yet
    first_sentinel = [r for r in bp if r["hc_id"] == f"fx-bp-{FX.day_of(-2)}-am0"][0]
    assert (first_sentinel["v"], first_sentinel["v2"]) == FX.SENTINEL_BP
    wake_days = {FX.day_of(i) for i in range(-6, 1)}
    watch_nights = {s["hc_id"][len("fx-sleep-"):] for s in sessions
                    if s["hc_id"].startswith("fx-sleep-2")}
    assert wake_days - watch_nights == {FX.day_of(-6), FX.day_of(-4), FX.day_of(-1)}
