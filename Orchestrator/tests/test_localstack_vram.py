"""Unit tests for the VRAM peak sampler's pure logic and its graceful
degradation when nvidia-smi is absent. All GPU calls (sample_used_mib) are
monkeypatched — no real nvidia-smi is invoked (this box has none)."""
import json

import pytest

from diagnostics.localstack import vram
from diagnostics.localstack.vram import summarize, BUDGET_MIB


def test_summarize_peak_delta_and_fits():
    out = summarize("g1", 0, baseline=10000, samples=[10000, 12000, 11000],
                    elapsed_s=1.0, rc=0)
    assert out["baseline_mib"] == 10000
    assert out["peak_mib"] == 12000
    assert out["delta_mib"] == 2000
    assert out["n_samples"] == 3
    assert out["headroom_mib"] == BUDGET_MIB - 12000
    assert out["fits_budget"] is True


def test_summarize_over_budget_does_not_fit():
    over = BUDGET_MIB + 500
    out = summarize("g5", 0, baseline=15000, samples=[over], elapsed_s=0.5, rc=0)
    assert out["peak_mib"] == over
    assert out["fits_budget"] is False
    assert out["headroom_mib"] == BUDGET_MIB - over  # negative


def test_summarize_baseline_none_falls_back_to_first_sample():
    # baseline read failed, but sampling recovered — first sample anchors delta.
    out = summarize("g3", 0, baseline=None, samples=[9000, 9500], elapsed_s=1.0, rc=0)
    assert out["baseline_mib"] == 9000
    assert out["peak_mib"] == 9500
    assert out["delta_mib"] == 500
    assert out["fits_budget"] is True


def test_summarize_no_baseline_no_samples_is_unmeasurable():
    # nvidia-smi entirely absent: everything None, fits_budget None (NOT a pass).
    out = summarize("x", 0, baseline=None, samples=[], elapsed_s=0.0, rc=0)
    assert out["baseline_mib"] is None
    assert out["peak_mib"] is None
    assert out["delta_mib"] is None
    assert out["headroom_mib"] is None
    assert out["fits_budget"] is None
    assert out["n_samples"] == 0


def test_main_happy_path_returns_zero(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(vram, "sample_used_mib", lambda gpu: 5000)
    out = tmp_path / "g.json"
    rc = vram.main(["--label", "t", "--duration", "0", "--out", str(out)])
    assert rc == 0
    saved = json.loads(out.read_text())
    assert saved["baseline_mib"] == 5000
    assert saved["peak_mib"] == 5000
    assert saved["fits_budget"] is True


def test_main_graceful_when_nvidia_smi_absent(monkeypatch, capsys):
    def _boom(gpu):
        raise FileNotFoundError("No such file or directory: 'nvidia-smi'")
    monkeypatch.setattr(vram, "sample_used_mib", _boom)
    # Must NOT raise; must exit 3 (unmeasurable) with a remediation message.
    rc = vram.main(["--label", "t", "--duration", "0"])
    assert rc == 3
    err = capsys.readouterr().err
    assert "nvidia-smi" in err
