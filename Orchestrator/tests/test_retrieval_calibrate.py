"""scripts/calibrate_threshold.py — store-override + noise-band calibration (M6f step 2).

Hermetic: tmp_path schema-2 stores via the real VectorStore, fake query
embeddings (the provider layer is never touched). Pins:

  * --store-dir/--schema open the candidate store and the report carries BOTH
    bands (relevance + noise) plus the FLOOR GUIDANCE line;
  * scores flow through store.search, i.e. the v2 per-snapshot chunk-max
    COLLAPSE (the worst-relevant floor is the collapsed per-snapshot minimum,
    not a raw chunk-row score);
  * schema mismatches and missing stores fail loud (never auto-create — the
    script is read-only);
  * the no-args legacy path keeps today's output shape (no noise band).
"""
import importlib.util
import re
from pathlib import Path

import numpy as np
import pytest

from Orchestrator.embeddings.store import get_store

REPO = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "calibrate_threshold_under_test", REPO / "scripts" / "calibrate_threshold.py")
CAL = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CAL)

SLUG = "gemini-embedding-2"
DIMS = 3072


def _vec(x: float, y: float) -> np.ndarray:
    v = np.zeros(DIMS, dtype=np.float32)
    v[0], v[1] = x, y
    return v


# Query vectors the fake embedder hands out: relevance queries align with the
# corpus (x-axis), noise queries are near-orthogonal (y-axis).
REL_Q = _vec(1, 0).tolist()
NOISE_Q = _vec(0, 1).tolist()


def _mk_v2_store(base_dir):
    """3 snapshots, 6 chunk rows; SNAP-C's best chunk beats its own worst by a
    wide margin so a broken collapse is numerically distinguishable."""
    store = get_store(SLUG, base_dir=base_dir, schema=2)
    store.append_group("SNAP-A", [_vec(1, 0), _vec(0.95, 0.05)])
    store.append_group("SNAP-B", [_vec(1, 0.1)])
    store.append_group("SNAP-C", [_vec(1, 0.14), _vec(0.9, 0.05), _vec(1, 0.02)])
    return store


def _fake_embed(monkeypatch):
    def fake(slug, texts):
        assert slug == SLUG
        return [REL_Q if t in CAL.QUERIES else NOISE_Q for t in texts]
    monkeypatch.setattr(CAL, "_embed_override", fake)


def _grab(pattern: str, out: str) -> float:
    m = re.search(pattern, out)
    assert m, f"pattern {pattern!r} not found in output:\n{out}"
    return float(m.group(1))


def test_store_override_reports_both_bands_and_floor_guidance(
        tmp_path, monkeypatch, capsys):
    _mk_v2_store(tmp_path)
    _fake_embed(monkeypatch)
    CAL.main(["--store-dir", str(tmp_path), "--slug", SLUG, "--schema", "2"])
    out = capsys.readouterr().out

    assert f"store: slug={SLUG}  schema=2  rows=6  snapshots=3" in out
    assert "relevance band" in out
    assert "noise band (deliberately off-topic queries):" in out
    assert "FLOOR GUIDANCE" in out
    # One noise line per off-topic query.
    assert len(re.findall(r"top1=\d\.\d{4} top5=", out)) == len(CAL.NOISE_QUERIES)

    # Collapsed per-snapshot chunk-max floor: worst relevant = SNAP-B's single
    # chunk cos(1,0.1 ; 1,0) = 0.9950. WITHOUT collapse the raw-row minimum
    # would be SNAP-C's worst chunk (0.9903) — a broken collapse fails here.
    worst = _grab(r"worst relevant top-10 hit = (\d\.\d{4})", out)
    assert abs(worst - 0.9950) < 2e-3, worst

    # Noise ceiling = SNAP-C's best chunk vs the y-axis query: 0.14/|(1,.14)|.
    ceiling = _grab(r"noise ceiling \(max off-topic top-1\) = (\d\.\d{4})", out)
    assert abs(ceiling - 0.1386) < 2e-3, ceiling

    suggested = _grab(r"margin = (\d\.\d{4})", out)
    assert abs(suggested - (worst - CAL.MARGIN)) < 1e-3
    assert suggested > ceiling
    assert "WARNING" not in out


def test_store_dir_accepts_store_dir_itself(tmp_path, monkeypatch, capsys):
    _mk_v2_store(tmp_path)
    _fake_embed(monkeypatch)
    CAL.main(["--store-dir", str(tmp_path / SLUG), "--slug", SLUG, "--schema", "2"])
    out = capsys.readouterr().out
    assert f"store: slug={SLUG}  schema=2" in out


def test_overlapping_bands_print_warning(tmp_path, monkeypatch, capsys):
    """Noise scoring INSIDE the relevance band -> the guidance line warns."""
    store = get_store(SLUG, base_dir=tmp_path, schema=2)
    store.append_group("SNAP-X", [_vec(1, 1)])   # equidistant from both queries
    _fake_embed(monkeypatch)
    CAL.main(["--store-dir", str(tmp_path), "--slug", SLUG, "--schema", "2"])
    out = capsys.readouterr().out
    # worst relevant == noise ceiling == cos 45deg; margin crosses the ceiling.
    assert "WARNING: margin crosses the noise ceiling" in out


def test_schema_mismatch_fails_loud(tmp_path):
    v1 = get_store(SLUG, base_dir=tmp_path)          # autodetect -> fresh v1
    v1.append("SNAP-V1", _vec(1, 0))
    with pytest.raises(ValueError, match="schema"):
        CAL.main(["--store-dir", str(tmp_path), "--slug", SLUG, "--schema", "2"])


def test_missing_store_refused_not_created(tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(SystemExit, match="refusing to create"):
        CAL.main(["--store-dir", str(missing), "--slug", SLUG, "--schema", "2"])
    assert not missing.exists(), "calibrate must never create a store"


def test_legacy_no_args_output_shape_unchanged(tmp_path, monkeypatch, capsys):
    """No args -> today's report exactly: active store, relevance band +
    SUGGESTED threshold line, and NO noise band / guidance."""
    store = _mk_v2_store(tmp_path)
    monkeypatch.setattr(CAL, "get_active_slug", lambda: SLUG)
    monkeypatch.setattr(CAL.S, "get_active_store", lambda: store)
    monkeypatch.setattr(
        CAL.S, "generate_embedding_sync", lambda q, purpose="query": REL_Q)
    CAL.main([])
    out = capsys.readouterr().out
    assert out.startswith(f"active model: {SLUG}  store.count=3")
    assert "SUGGESTED threshold = worst_top10_min - 0.05 =" in out
    assert "noise band" not in out
    assert "FLOOR GUIDANCE" not in out
