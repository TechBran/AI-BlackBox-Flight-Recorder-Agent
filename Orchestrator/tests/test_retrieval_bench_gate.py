"""eval/run_bench.py — M6f candidate arm + six-gate mode (runbook step 3).

Hermetic: tmp_path schema-2 stores, synthetic metrics straddling the gate
thresholds, and the COMMITTED sweep results file (no network, no live store,
retrieve() never called). Pins:

  * gate baselines are read programmatically from the sweep JSON (w=0.005
    row), never hardcoded;
  * candidate resolution requires an EXISTING schema-2 store (both path
    forms accepted; missing/v1 refused);
  * candidate cache keys carry the store's meta generation, so a refreshed
    candidate re-benches;
  * evaluate_gates: >= for gates 1-2 (no regression), STRICT > for gates 3-4
    (must improve), holdout hits@10 == n for gate 5, and the pytest gate 6
    (skipped = recorded, not failed);
  * the gate table/markdown render without error.
"""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from Orchestrator.embeddings.store import get_store

REPO = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "run_bench_under_test", REPO / "eval" / "run_bench.py")
RB = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(RB)

SLUG = "gemini-embedding-2"
DIMS = 3072


def _vec(x: float, y: float) -> np.ndarray:
    v = np.zeros(DIMS, dtype=np.float32)
    v[0], v[1] = x, y
    return v


def _mk_v2_store(base_dir):
    store = get_store(SLUG, base_dir=base_dir, schema=2)
    store.append_group("SNAP-A", [_vec(1, 0), _vec(0.9, 0.1)])
    store.append_group("SNAP-B", [_vec(0.8, 0.2)])
    return store


# ── baseline source (the committed sweep file, read programmatically) ─────────

def test_gate_baselines_read_from_committed_sweep():
    arms = RB.load_gate_baselines(RB.GATE_SWEEP_JSON, RB.GATE_WEIGHT)
    assert arms["gemini2-hybrid"]["recall@10"] == 0.4891
    assert arms["gemini2-hybrid"]["recall@10_gt10k"] == 0.6333
    assert arms["gemini2-semantic"]["recall@10"] == 0.497
    assert arms["gemini2-semantic"]["recall@10_gt10k"] == 0.42


def test_gate_baselines_missing_weight_fails_loud():
    with pytest.raises(SystemExit, match="no w=0.123"):
        RB.load_gate_baselines(RB.GATE_SWEEP_JSON, 0.123)


# ── candidate store resolution ────────────────────────────────────────────────

def test_resolve_candidate_accepts_base_dir_and_store_dir(tmp_path):
    made = _mk_v2_store(tmp_path)
    via_base = RB.resolve_candidate_store(str(tmp_path), SLUG)
    via_store_dir = RB.resolve_candidate_store(str(tmp_path / SLUG), SLUG)
    assert via_base is made and via_store_dir is made  # canonical instance
    assert via_base.schema == 2


def test_resolve_candidate_refuses_missing_store(tmp_path):
    missing = tmp_path / "not-built-yet"
    with pytest.raises(SystemExit, match="refusing to create"):
        RB.resolve_candidate_store(str(missing), SLUG)
    assert not missing.exists()


def test_resolve_candidate_refuses_v1_store(tmp_path):
    v1 = get_store(SLUG, base_dir=tmp_path)  # autodetect -> fresh v1
    v1.append("SNAP-V1", _vec(1, 0))
    with pytest.raises(ValueError, match="schema"):
        RB.resolve_candidate_store(str(tmp_path), SLUG)


# ── generation-keyed candidate cache ──────────────────────────────────────────

def test_candidate_cache_key_changes_when_store_refreshed(tmp_path):
    store = _mk_v2_store(tmp_path)
    gen1 = RB.store_generation(store)
    assert gen1 >= 1  # appends bump generation into meta
    row = {"query": "q", "operator": "system"}
    key1 = RB.row_key(RB.candidate_arms(SLUG, store, gen1)[0]["cache_name"], row)
    assert f"gen={gen1}" in key1

    store.append_group("SNAP-NEW", [_vec(0.5, 0.5)])  # re-diff refresh
    gen2 = RB.store_generation(store)
    assert gen2 > gen1
    key2 = RB.row_key(RB.candidate_arms(SLUG, store, gen2)[0]["cache_name"], row)
    assert key2 != key1, "refreshed candidate must re-bench, not reuse ranks"


def test_candidate_arms_are_hybrid_plus_semantic(tmp_path):
    store = _mk_v2_store(tmp_path)
    arms = RB.candidate_arms(SLUG, store, RB.store_generation(store))
    assert [a["include_keyword"] for a in arms] == [True, False]
    assert all(a["store"] is store and a["slug"] == SLUG for a in arms)


# ── six-gate evaluation (synthetic numbers straddling the thresholds) ─────────

SWEEP = {
    "gemini2-hybrid": {"recall@10": 0.4891, "recall@10_gt10k": 0.6333},
    "gemini2-semantic": {"recall@10": 0.497, "recall@10_gt10k": 0.42},
}


def _metrics(r10, gt10k, tail, hits=3, n=3):
    return {
        "overall": {"n": 503, "recall@10": r10, "mrr": 0.4},
        "strata": {
            "length_band": {">10k": {"n": 150, "recall@10": gt10k, "mrr": 0.4}},
            "position_third": {"tail": {"n": 103, "recall@10": tail, "mrr": 0.4}},
        },
        "holdout": {"n": n, "hits@10": hits, "detail": {}},
    }


BASE = {"hybrid": _metrics(0.4891, 0.6333, 0.30),
        "semantic": _metrics(0.497, 0.42, 0.32)}


def _verdicts(rows):
    return {r["gate"]: r["verdict"] for r in rows}


def test_all_gates_pass_above_thresholds():
    cand = {"hybrid": _metrics(0.50, 0.70, 0.35),
            "semantic": _metrics(0.51, 0.50, 0.40)}
    rows, ok = RB.evaluate_gates(SWEEP, cand, BASE, tests_gate=True)
    assert ok
    assert set(_verdicts(rows).values()) == {"PASS"}
    assert len(rows) == 9  # 1,2,3a,3b,4a,4b,5a,5b,6


def test_no_regression_gates_pass_on_equality_but_improve_gates_fail():
    """Gates 1-2 are >=; gates 3-4 are STRICT > (MUST IMPROVE)."""
    cand = {"hybrid": _metrics(0.4891, 0.6333, 0.30),   # all exactly at baseline
            "semantic": _metrics(0.497, 0.42, 0.32)}
    rows, ok = RB.evaluate_gates(SWEEP, cand, BASE, tests_gate=True)
    v = _verdicts(rows)
    assert v["1"] == "PASS" and v["2"] == "PASS"
    assert v["3a"] == "FAIL" and v["3b"] == "FAIL"
    assert v["4a"] == "FAIL" and v["4b"] == "FAIL"
    assert not ok


def test_overall_regression_fails_gate():
    cand = {"hybrid": _metrics(0.4890, 0.70, 0.35),     # 0.0001 under baseline
            "semantic": _metrics(0.51, 0.50, 0.40)}
    rows, ok = RB.evaluate_gates(SWEEP, cand, BASE, tests_gate=True)
    assert _verdicts(rows)["1"] == "FAIL"
    assert not ok


def test_holdout_miss_fails_gate():
    cand = {"hybrid": _metrics(0.50, 0.70, 0.35, hits=2),
            "semantic": _metrics(0.51, 0.50, 0.40)}
    rows, ok = RB.evaluate_gates(SWEEP, cand, BASE, tests_gate=True)
    v = _verdicts(rows)
    assert v["5a"] == "FAIL" and v["5b"] == "PASS"
    assert not ok


def test_tests_gate_failure_fails_and_skip_is_recorded_not_failed():
    cand = {"hybrid": _metrics(0.50, 0.70, 0.35),
            "semantic": _metrics(0.51, 0.50, 0.40)}
    rows, ok = RB.evaluate_gates(SWEEP, cand, BASE, tests_gate=False)
    assert _verdicts(rows)["6"] == "FAIL" and not ok
    rows, ok = RB.evaluate_gates(SWEEP, cand, BASE, tests_gate=None)
    assert _verdicts(rows)["6"] == "SKIPPED" and ok


def test_missing_stratum_fails_never_silently_passes():
    cand = {"hybrid": _metrics(0.50, 0.70, 0.35),
            "semantic": _metrics(0.51, 0.50, 0.40)}
    cand["hybrid"]["strata"]["length_band"][">10k"] = {"n": 0}  # empty bucket
    rows, ok = RB.evaluate_gates(SWEEP, cand, BASE, tests_gate=True)
    assert _verdicts(rows)["3a"] == "FAIL"
    assert not ok


# ── rendering + CLI wiring ────────────────────────────────────────────────────

def test_gate_table_and_markdown_render():
    cand = {"hybrid": _metrics(0.50, 0.70, 0.35),
            "semantic": _metrics(0.51, 0.50, 0.40)}
    rows, ok = RB.evaluate_gates(SWEEP, cand, BASE, tests_gate=True)
    table = RB.fmt_gate_table(rows)
    assert table.count("| PASS |") == 9

    report = {
        "date": "2026-07-02", "k": 10, "n_rows": 503,
        "candidate": {"slug": SLUG, "dir": "/x/_build/" + SLUG, "schema": 2,
                      "rows": 20000, "snapshots": 7600, "generation": 3},
        "baseline_source": str(RB.GATE_SWEEP_JSON), "baseline_weight": 0.005,
        "active_store_snapshots": 7600,
        "gates": rows, "all_pass": ok,
        "runs": {"baseline": BASE, "candidate": cand},
    }
    md = RB.fmt_gate_md(report)
    assert "M6f chunk-store gate" in md
    assert "ALL GATES PASS" in md
    assert "tail-third" in md
    assert "| baseline-hybrid |" in md and "| candidate-semantic |" in md


def test_gate_cli_requires_candidate_args():
    with pytest.raises(SystemExit):
        RB.main(["--gate"])


def test_candidate_dir_requires_slug():
    with pytest.raises(SystemExit):
        RB.main(["--candidate-dir", "/tmp/x"])


# ── run_gate end-to-end (hermetic: fake retrieve/active store, tmp artifacts) ─

GATE_ROWS = [
    {"query": "alpha", "operator": "system", "gold_snap_id": "SNAP-A",
     "length_band": ">10k", "position_third": "tail", "source": "generated"},
    {"query": "beta", "operator": "system", "gold_snap_id": "SNAP-B",
     "length_band": "<6k", "position_third": "head", "source": "generated"},
    {"query": "gamma", "operator": "system", "gold_snap_id": "SNAP-A",
     "length_band": None, "position_third": None, "source": "holdout"},
]


class _FakeActiveStore:
    """Minimal v1 active-store stand-in for the fresh-baseline arms."""
    count = 2
    schema = 1

    def ids(self):
        return {"SNAP-A", "SNAP-B"}


class _FakeCfg:
    @staticmethod
    def getfloat(section, option, fallback=None):
        return 0.005


def _wire_gate(monkeypatch, tmp_path, baseline_hits_tail: bool):
    """Patch every external seam of run_gate; candidate always ranks gold #1,
    the fresh baseline misses the tail/>10k gold unless baseline_hits_tail."""
    cand = _mk_v2_store(tmp_path)
    fake_active = _FakeActiveStore()
    real_get_store = RB.get_store

    def fake_get_store(slug, dims=None, base_dir=None, schema=None):
        if base_dir is not None:  # candidate resolution -> real tmp store
            return real_get_store(slug, dims=dims, base_dir=base_dir, schema=schema)
        return fake_active

    def fake_retrieve(query, operator=None, k=10, include_keyword=True,
                      store=None, query_vector=None):
        gold = {"alpha": "SNAP-A", "beta": "SNAP-B", "gamma": "SNAP-A"}[query]
        if store is fake_active and query == "alpha" and not baseline_hits_tail:
            return [("SNAP-B", 0.9)]  # baseline buries the tail/>10k gold
        other = "SNAP-B" if gold == "SNAP-A" else "SNAP-A"
        return [(gold, 0.9), (other, 0.5)]

    monkeypatch.setattr(RB, "get_store", fake_get_store)
    monkeypatch.setattr(RB, "get_active_slug", lambda: SLUG)
    monkeypatch.setattr(RB, "embed_queries",
                        lambda slug, texts: [[1.0, 0.0]] * len(texts))
    monkeypatch.setattr(RB, "retrieve", fake_retrieve)
    monkeypatch.setattr(RB, "CACHE_PATH", tmp_path / "bench_cache.json")
    monkeypatch.setattr(RB, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr("Orchestrator.config.CFG", _FakeCfg())
    import argparse
    return argparse.Namespace(
        out_date="2099-01-01", candidate_dir=str(tmp_path), candidate_slug=SLUG,
        gate_sweep_json=str(RB.GATE_SWEEP_JSON), gate_weight=0.005,
        skip_tests_gate=True, mmr_lambda=None, candidate_n=None)


def test_run_gate_all_pass_writes_artifacts_and_exits_zero(
        tmp_path, monkeypatch, capsys):
    args = _wire_gate(monkeypatch, tmp_path, baseline_hits_tail=False)
    RB.run_gate(list(GATE_ROWS), args)  # no SystemExit == exit 0
    out = capsys.readouterr().out
    assert "ALL GATES PASS" in out
    md = (tmp_path / "results" / "2099-01-01-chunk-gate.md").read_text()
    assert "| 6 |" in md and "skipped" in md  # gate 6 recorded as SKIPPED
    assert "GATE FAILED" not in md
    js = json.loads((tmp_path / "results" / "2099-01-01-chunk-gate.json").read_text())
    assert js["all_pass"] is True
    assert js["candidate"]["schema"] == 2 and js["candidate"]["generation"] >= 1
    # Candidate ranks were cached under generation-scoped keys.
    cache = json.loads((tmp_path / "bench_cache.json").read_text())
    gen = js["candidate"]["generation"]
    assert any(f"gate-cand|gen={gen}" in k for k in cache)


def test_run_gate_failure_exits_nonzero_and_records_fail(
        tmp_path, monkeypatch, capsys):
    # Baseline hits the tail gold too -> candidate tail-third does NOT improve
    # (equal), so gates 4a/4b FAIL and the gate exits 1 (STOP, no cutover).
    args = _wire_gate(monkeypatch, tmp_path, baseline_hits_tail=True)
    with pytest.raises(SystemExit) as exc:
        RB.run_gate(list(GATE_ROWS), args)
    assert exc.value.code == 1
    assert "GATE FAILED — STOP, no cutover" in capsys.readouterr().out
    js = json.loads((tmp_path / "results" / "2099-01-01-chunk-gate.json").read_text())
    assert js["all_pass"] is False
    verdicts = {r["gate"]: r["verdict"] for r in js["gates"]}
    assert verdicts["4a"] == "FAIL" and verdicts["4b"] == "FAIL"
