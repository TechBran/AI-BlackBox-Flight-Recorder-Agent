"""Retention (spec §3.6, decision D7): an optional `[wellness] detail_years`
(unset = keep forever) prunes buckets, sessions and readings older than N
years and keeps rollups and coverage; the ingest log rolls over at 90 days
and the delivered (disclosure) log at 400, whatever the setting. The
hourly maintenance pass applies it, logging counts only. Pruned days are
frozen: a late batch or a recompute never rebuilds them from the little
detail left. Time is injected throughout."""
from __future__ import annotations

import logging

import pytest

from Orchestrator.tests.wellness_support import (ENR, fixtures, fresh_store, ingest_batches,
                                                 ingest_fixture, rollup)
from Orchestrator.wellness import rollups, store as store_mod

FX = fixtures()
DAY_MS = 86_400_000
NOW = FX.now_ms()
# 2027-08-28, ten months after the fixture's day 0: with detail_years = 1 the
# horizon is 2026-08-28, so the fixture's first two months (from 2026-07-01)
# lose their detail and the last two keep it.
LATER = NOW + 304 * DAY_MS
# 2027-10-08: the horizon (2026-10-08) cuts into days_60 (from 2026-08-30).
LATEST = NOW + 345 * DAY_MS


def _later_day(ms):
    return store_mod.local_day(ms, FX.OFFSET_S)


def _count(st, table, where="1=1", args=()):
    with st.read() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]


def _summaries(st):
    with st.read() as conn:
        return {t: sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {t}"))
                for t in ("rollup", "coverage_day", "baseline", "digest")}


@pytest.fixture
def full(tmp_path, monkeypatch):
    st = fresh_store(tmp_path, monkeypatch)
    ingest_fixture(st, "continuous_120")
    return st


# ------------------------------------------------------------------ the setting

@pytest.mark.parametrize("value,years", [(None, None), ("", None), ("2", 2), (" 5 ", 5),
                                         ("1", 1), ("0", None), ("-3", None), ("two", None),
                                         ("1.5", None)])
def test_detail_years_is_read_fresh_and_never_misread(isolated_stores, value, years):
    from Orchestrator import config
    from Orchestrator.utils.config_ini import update_config_ini

    def edit(p):
        if not p.has_section("wellness"):
            p.add_section("wellness")
        if value is None:
            p.remove_option("wellness", "detail_years")
        else:
            p.set("wellness", "detail_years", value)
    update_config_ini(edit)
    assert config.wellness_detail_years() == years


def test_the_horizon_is_n_years_before_the_local_day():
    assert store_mod.detail_horizon_day("2027-03-05", 1) == "2026-03-05"
    assert store_mod.detail_horizon_day("2028-02-29", 1) == "2027-02-28"
    assert store_mod.detail_horizon_day("2028-02-29", 4) == "2024-02-29"


# ------------------------------------------------------------------ pruning

def test_detail_older_than_the_horizon_goes_and_summaries_stay(full):
    before = _summaries(full)
    n_buckets = _count(full, "bucket")
    counts = full.prune(LATER, detail_years=1)
    horizon = store_mod.detail_horizon_day(_later_day(LATER), 1)
    assert full.detail_horizon() == horizon == "2026-08-28"
    cutoff = full.offsets().bounds(store_mod.add_days(horizon, -1))[0]
    assert counts["buckets"] > 0 and counts["sessions"] > 0 and counts["readings"] > 0
    assert counts["buckets"] + _count(full, "bucket") == n_buckets
    assert _count(full, "bucket", "ts<?", (cutoff,)) == 0
    assert _count(full, "reading", "ts<?", (cutoff,)) == 0
    assert _count(full, "session", "end_ts<?", (cutoff,)) == 0
    assert _count(full, "stage", "end_ts<?", (cutoff - DAY_MS,)) == 0
    assert _count(full, "bucket", "ts>=?", (cutoff,)) > 0       # the last two months stay
    assert _summaries(full) == before                            # rollups, coverage, baselines
    assert rollup(full, "steps", "day", FX.day_of(-110)) is not None


def test_no_setting_keeps_every_detail_but_the_logs_still_roll(full):
    st = full
    st.record_delivery("w-old", "t-old", "android", "local", None, "wellness_summary", {},
                       ["sleep"], {"x": 1}, ts=LATER - 401 * DAY_MS)
    st.record_delivery("w-new", "t-new", "android", "local", None, "wellness_summary", {},
                       ["sleep"], {"x": 1}, ts=LATER - 399 * DAY_MS)
    with st.write() as conn:
        st.log_batch(conn, "fx-recent", LATER - 89 * DAY_MS, 1, 1, 0, "ok", None)
    n_buckets = _count(st, "bucket")
    n_logs = _count(st, "ingest_log")
    counts = st.prune(LATER, detail_years=None)
    assert counts["buckets"] == counts["sessions"] == counts["readings"] == 0
    assert _count(st, "bucket") == n_buckets and st.detail_horizon() is None
    # The fixture's batches arrived on day 0, over 90 days before LATER.
    assert counts["ingest_log"] == n_logs - 1 and _count(st, "ingest_log") == 1
    assert counts["delivered"] == 1
    assert st.get_delivery("w-old") is None and st.get_delivery("w-new") is not None


def test_pruning_is_idempotent_and_the_horizon_only_moves_forward(full):
    full.prune(LATER, detail_years=1)
    again = full.prune(LATER, detail_years=1)
    assert again["buckets"] == again["sessions"] == again["readings"] == 0
    horizon = full.detail_horizon()
    full.prune(LATER, detail_years=5)            # a longer setting cannot bring detail back
    assert full.detail_horizon() == horizon
    full.prune(LATER + 10 * DAY_MS, detail_years=1)
    assert full.detail_horizon() == store_mod.add_days(horizon, 10)


# ------------------------------------------------------------------ frozen days

def test_a_pruned_day_is_never_rebuilt(full):
    full.prune(LATER, detail_years=1)
    old = FX.day_of(-110)
    before = _summaries(full)
    rollups.recompute(full, [old, store_mod.add_days(old, 1)], now_ms=LATER)
    assert _summaries(full) == before
    # A late batch for pruned days is dropped and counted, and changes nothing.
    data = FX.load("continuous_120")
    late = dict(data["batches"][-1], batch_id="fx-late-old")
    res = ingest_batches(full, [late], now_ms=LATER)[0]
    assert res.accepted == 0 and res.dropped > 0
    with full.read() as conn:
        logged = conn.execute("SELECT dropped FROM ingest_log WHERE batch_id='fx-late-old'"
                              ).fetchone()[0]
    assert '"past_retention"' in logged
    assert _summaries(full) == before
    assert _count(full, "bucket", "ts<?", (full.offsets().bounds(
        store_mod.add_days(full.detail_horizon(), -1))[0],)) == 0


def test_days_after_the_horizon_still_recompute(full):
    full.prune(LATER, detail_years=1)
    day = store_mod.add_days(full.detail_horizon(), 3)
    with full.write() as conn:
        conn.execute("DELETE FROM rollup WHERE bucket=?", (day,))
    rollups.recompute(full, [day], now_ms=LATER)
    assert rollup(full, "steps", "day", day) is not None


def test_a_deletion_of_a_pruned_record_touches_nothing(full):
    full.prune(LATER, detail_years=1)
    before = _summaries(full)
    gone = {"schema": "bbx.wellness.ingest/1", "batch_id": "fx-del-old", "enrollment_id": ENR,
            "epoch": 1, "deleted_ids": [f"fx-sleep-{FX.day_of(-110)}"]}
    res = ingest_batches(full, [gone], now_ms=LATER)[0]
    assert res.dirty_days == [] and _summaries(full) == before


# ------------------------------------------------------------------ every store, hourly

def test_prune_all_covers_every_store_and_logs_counts_only(tmp_path, monkeypatch, caplog):
    a = fresh_store(tmp_path, monkeypatch, op_key="000000000000000a")
    ingest_fixture(a, "days_60")
    b = store_mod.open_store("000000000000000b")
    ingest_fixture(b, "days_10")
    (tmp_path / "data" / "wellness" / "enrollments.json").write_text("{}")
    with caplog.at_level(logging.INFO, logger="Orchestrator.wellness.store"):
        out = store_mod.prune_all(now_ms=LATEST, detail_years=1)
    assert out["stores"] == 2 and out["buckets"] > 0 and out["ingest_log"] > 0
    assert _count(a, "bucket", "ts<?", (a.offsets().bounds(
        store_mod.add_days(a.detail_horizon(), -1))[0],)) == 0
    lines = [r.getMessage() for r in caplog.records if "[WELLNESS] retention" in r.getMessage()]
    assert len(lines) == 1
    for sentinel in FX.SENTINEL_STRINGS:
        assert sentinel not in lines[0]
    assert "000000000000000a" not in lines[0]
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="Orchestrator.wellness.store"):
        quiet = store_mod.prune_all(now_ms=LATEST, detail_years=1)
    assert quiet["buckets"] == 0
    assert not [r for r in caplog.records if "[WELLNESS] retention" in r.getMessage()]


def test_prune_all_survives_a_store_erased_under_it(tmp_path, monkeypatch):
    st = fresh_store(tmp_path, monkeypatch, op_key="000000000000000c")
    ingest_fixture(st, "days_10")
    real = store_mod.Store.prune

    def erase_first(self, *a, **k):
        store_mod.delete_store_files(self.op_key)
        return real(self, *a, **k)

    monkeypatch.setattr(store_mod.Store, "prune", erase_first)
    out = store_mod.prune_all(now_ms=LATEST, detail_years=1)
    assert out["stores"] == 0 and not st.path.exists()


def test_the_maintenance_pass_applies_it(isolated_stores, monkeypatch):
    from Orchestrator import maintenance, volume
    from Orchestrator.utils.config_ini import update_config_ini
    from Orchestrator.wellness import paths
    paths.ensure_private_dir()
    st = store_mod.open_store("000000000000000d")
    ingest_fixture(st, "days_60")

    def edit(p):
        if not p.has_section("wellness"):
            p.add_section("wellness")
        p.set("wellness", "detail_years", "1")
    update_config_ini(edit)
    monkeypatch.setattr(volume, "scheduled_archive_copy", lambda: {"copied": False})
    monkeypatch.setattr(volume, "cleanup_old_archives", lambda **k: {"deleted_files": 0})
    monkeypatch.setattr(volume, "cleanup_old_artifacts", lambda: None)
    monkeypatch.setattr(volume, "ledger_fsck_due", lambda: False)
    monkeypatch.setattr(maintenance, "cleanup_old_session_files", lambda *a, **k: 0)
    monkeypatch.setattr(store_mod, "_now_ms", lambda: LATEST)
    report = maintenance.run_storage_maintenance()
    assert report["errors"] == []
    assert report["wellness"]["stores"] == 1 and report["wellness"]["buckets"] > 0
    assert st.detail_horizon() == store_mod.detail_horizon_day(_later_day(LATEST), 1)
