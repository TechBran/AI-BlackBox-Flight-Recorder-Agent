"""Pure-logic unit tests for the G2 reranker-validity harness (eval/rerank_g2.py).

The harness itself runs on the GPU box (MS02) against a live llama-server; these
tests cover only its dependency-free scoring logic — degenerate-score detection,
relevant-vs-negative separation, and the pure-Python Spearman — so CI (no torch,
no GPU, no network) still guards the pass/fail math.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "eval"))
import rerank_g2  # noqa: E402


def test_is_degenerate_flags_1e28_scores():
    # the exact broken-GGUF signature: all scores ~1e-28
    assert rerank_g2.is_degenerate([1e-28, 2e-28, 1.5e-28]) is True
    assert rerank_g2.is_degenerate([]) is True
    assert rerank_g2.is_degenerate([0.5, 0.5, 0.5]) is True   # no spread
    assert rerank_g2.is_degenerate([0.9, 0.1, 0.5]) is False


def test_separation_ok_requires_min_relevant_over_max_negative():
    # documents = relevant(2) + hard_negative(2)
    assert rerank_g2.separation_ok([0.9, 0.8, 0.2, 0.1], 2) is True
    assert rerank_g2.separation_ok([0.9, 0.1, 0.8, 0.2], 2) is False  # a neg beats a rel
    # not enough info to judge → not a failure
    assert rerank_g2.separation_ok([0.5, 0.4], 0) is True
    assert rerank_g2.separation_ok([0.5, 0.4], 2) is True


def test_spearman_perfect_inverse_and_tie():
    assert rerank_g2.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert rerank_g2.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    # a constant series has zero variance → nan (guarded, never a ZeroDivision)
    import math
    assert math.isnan(rerank_g2.spearman([1, 1, 1], [1, 2, 3]))
