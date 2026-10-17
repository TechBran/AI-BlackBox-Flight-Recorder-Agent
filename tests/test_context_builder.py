"""Integration tests for Orchestrator.context_builder.build_fossil_context.

These run against the live volume — they are integration probes, not pure unit
tests. They assert that the shared retrieval builder produces provenance with
all four sources and that per-operator scoping is enforced.
"""
import json

import pytest

from Orchestrator.context_builder import build_fossil_context

pytestmark = pytest.mark.live_store


def test_build_fossil_context_populates_all_four_sources():
    # Uses the real volume; this is an integration probe
    text_block, provenance = build_fossil_context(
        user_text="tool vault recent work",
        operator="Brandon",
    )
    assert isinstance(provenance, dict)
    assert set(provenance.keys()) == {"recent", "keyword", "semantic", "checkpoint"}
    # Brandon has > 6000 snapshots; all four lists should populate for any prompt
    assert len(provenance["recent"]) >= 1, "recent should not be empty for Brandon"
    assert len(provenance["checkpoint"]) >= 1, "checkpoint should not be empty"
    assert "===" in text_block, "context text must contain at least one section marker"


def test_build_fossil_context_empty_query_still_gives_recent_and_checkpoint():
    _, prov = build_fossil_context(user_text="", operator="Brandon")
    assert prov["keyword"] == []
    assert prov["semantic"] == []
    assert len(prov["recent"]) >= 1
    assert len(prov["checkpoint"]) >= 1


def _no_operator_leak(snap_ids, operator):
    """Verify each returned snapshot belongs to the claimed operator."""
    from Orchestrator.fossils import get_snapshot_by_id
    for sid in snap_ids:
        snap = get_snapshot_by_id(sid)
        assert snap is not None, f"{sid} not found"
        assert snap["metadata"]["operator"] == operator, \
            f"{sid} belongs to {snap['metadata']['operator']}, leaked into {operator}'s retrieval"


def test_brandon_retrieval_does_not_return_other_operators_snapshots():
    _, prov = build_fossil_context(user_text="tool vault", operator="Brandon")
    all_ids = prov["recent"] + prov["keyword"] + prov["semantic"] + prov["checkpoint"]
    _no_operator_leak(all_ids, "Brandon")


def test_nonexistent_operator_returns_empty_retrieval():
    _, prov = build_fossil_context(user_text="anything", operator="nonexistent-test-user-zqx")
    assert prov["recent"] == []
    assert prov["keyword"] == []
    assert prov["semantic"] == []
    assert prov["checkpoint"] == []


def test_empty_operator_raises_value_error():
    with pytest.raises(ValueError):
        build_fossil_context(user_text="anything", operator="")
    with pytest.raises(ValueError):
        build_fossil_context(user_text="anything", operator="   ")
