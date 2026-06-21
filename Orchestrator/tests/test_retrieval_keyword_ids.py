"""Tests for the snap_id-returning keyword retrievers (Phase 2 retrieval hardening).

These retrievers return ranked snap_ids instead of decoded snapshot text, so the
hybrid fuser can fuse keyword + semantic results BY snap_id (no O(n^2) text-equality
reverse-map, no full snap_to_text rebuild). The text-returning wrappers must stay
behaviorally identical to before.
"""
from Orchestrator.fossils import (
    keyword_retrieve_ids,
    keyword_retrieve_ids_for_operator,
    keyword_retrieve,
    keyword_retrieve_for_operator,
    load_snapshot_index,
    read_volume_bytes,
    extract_snap_ids,
)
from Orchestrator.config import VOL_PATH


def _vol():
    return read_volume_bytes(VOL_PATH).decode("utf-8", "replace")


def test_keyword_retrieve_ids_returns_valid_snap_ids():
    idx = load_snapshot_index()
    vol = _vol()
    ids = keyword_retrieve_ids(vol, "embeddings model switch reembed", k=5)
    assert isinstance(ids, list) and len(ids) <= 5
    assert all(sid in idx for sid in ids)


def test_keyword_retrieve_ids_for_operator_returns_valid_snap_ids():
    idx = load_snapshot_index()
    vol = _vol()
    ids = keyword_retrieve_ids_for_operator(vol, "embeddings model switch reembed", 5, "system")
    assert isinstance(ids, list) and len(ids) <= 5
    assert all(sid in idx for sid in ids)


def test_text_wrapper_matches_id_version_order_nonoperator():
    """The text-returning keyword_retrieve must rank identically to the id version
    (it is now a thin wrapper that maps ids -> text)."""
    vol = _vol()
    q = "embeddings model switch reembed"
    ids = keyword_retrieve_ids(vol, q, k=8)
    texts = keyword_retrieve(vol, q, k=8)
    assert extract_snap_ids(texts) == ids


def test_text_wrapper_matches_id_version_order_operator():
    vol = _vol()
    q = "control phone on-device gemma"
    ids = keyword_retrieve_ids_for_operator(vol, q, 8, "system")
    texts = keyword_retrieve_for_operator(vol, q, 8, "system")
    assert extract_snap_ids(texts) == ids
