"""Tests for the snap_id-returning keyword retrievers (Phase 2 retrieval hardening).

These retrievers return ranked snap_ids instead of decoded snapshot text, so the
hybrid fuser can fuse keyword + semantic results BY snap_id (no O(n^2) text-equality
reverse-map, no full snap_to_text rebuild). The text-returning wrappers must stay
behaviorally identical to before.

They run over the fixed synthetic corpus of test_keyword_scorer_parity, not the
box's ledger: over an empty ledger (a fresh tree, CI) every assertion here held
for an empty result, and on a box they scanned its whole volume.
"""
import pytest

from Orchestrator.fossils import (
    keyword_retrieve_ids,
    keyword_retrieve_ids_for_operator,
    keyword_retrieve,
    keyword_retrieve_for_operator,
    extract_snap_ids,
)
from Orchestrator.tests.test_keyword_scorer_parity import (
    SYNTH_QUERY,
    _build_synthetic_corpus,
    serve_corpus_to_operator_scorer,
)

OFF_TOPIC_QUERY = "control phone on-device gemma"


@pytest.fixture
def corpus(monkeypatch, tmp_path):
    """The synthetic volume text, with the operator scorer's index and volume
    reads pointed at the same corpus."""
    vol_txt, vol_bytes, index = _build_synthetic_corpus()
    serve_corpus_to_operator_scorer(monkeypatch, tmp_path, vol_bytes, index)
    return vol_txt, index


def test_keyword_retrieve_ids_returns_valid_snap_ids(corpus):
    vol, idx = corpus
    ids = keyword_retrieve_ids(vol, SYNTH_QUERY, k=3)
    assert ids and len(ids) <= 3
    assert all(sid in idx for sid in ids)


def test_keyword_retrieve_ids_for_operator_returns_valid_snap_ids(corpus):
    vol, idx = corpus
    ids = keyword_retrieve_ids_for_operator(vol, SYNTH_QUERY, 3, "system")
    assert ids and len(ids) <= 3
    assert all(sid in idx for sid in ids)


def test_text_wrapper_matches_id_version_order_nonoperator(corpus):
    """The text-returning keyword_retrieve must rank identically to the id version
    (it is now a thin wrapper that maps ids -> text)."""
    vol, _idx = corpus
    ids = keyword_retrieve_ids(vol, SYNTH_QUERY, k=8)
    texts = keyword_retrieve(vol, SYNTH_QUERY, k=8)
    assert len(ids) >= 2
    assert extract_snap_ids(texts) == ids


@pytest.mark.parametrize("query,op", [(SYNTH_QUERY, "system"), (OFF_TOPIC_QUERY, "Brandon")])
def test_text_wrapper_matches_id_version_order_operator(corpus, query, op):
    vol, _idx = corpus
    ids = keyword_retrieve_ids_for_operator(vol, query, 8, op)
    texts = keyword_retrieve_for_operator(vol, query, 8, op)
    assert ids
    assert extract_snap_ids(texts) == ids
