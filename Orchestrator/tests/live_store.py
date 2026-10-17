"""Helpers for the live_store tests: the ones that read this box's real ledger,
snapshot index and vector store (pytest.ini marker; excluded by default, run
with BLACKBOX_TEST_LIVE=1 pytest -m live_store).

"Recent" is measured from the corpus, not the calendar (audit
O21-python-tests-04): the recency checks used to pin a 'SNAP-202606' prefix,
which went red as soon as the ledger moved past June 2026.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

# A topic counts as having recent activity when a snapshot of it falls within
# this many days of the newest snapshot in the index.
RECENT_WINDOW_DAYS = 180


def require_live_store():
    """Skip (never fake-pass) when the active vector store or its embedding
    provider is unavailable or empty."""
    try:
        from Orchestrator.embeddings.search import get_active_store
        store = get_active_store()
    except Exception as e:  # noqa: BLE001 - provider/store unavailable in this env
        pytest.skip(f"active store/provider unavailable: {e}")
    if store.count == 0:
        pytest.skip("active store empty")
    return store


def require_query_embedder():
    """Skip when the active provider cannot embed a query on this box. The lean
    and golden checks then fail only on a real ranking result, never because
    the embedding call itself was refused (audit O21-python-tests-08)."""
    from Orchestrator.embeddings.search import generate_embedding_sync

    try:
        vector = generate_embedding_sync("probe", purpose="query")
    except Exception as e:  # noqa: BLE001 - provider unreachable in this env
        pytest.skip(f"query embedding unavailable: {e}")
    if not vector:
        pytest.skip("query embedding unavailable (the active provider returned nothing)")


def snap_date(snap_id: str) -> str:
    """'SNAP-20260606-6930' -> '20260606' ('' for an id of another shape)."""
    parts = snap_id.split("-")
    return parts[1] if len(parts) >= 3 and parts[1].isdigit() else ""


def recent_floor(window_days: int = RECENT_WINDOW_DAYS) -> str:
    """YYYYMMDD of the newest snapshot in the index minus window_days."""
    from Orchestrator.fossils import load_snapshot_index

    dates = [snap_date(sid) for sid in load_snapshot_index()]
    dates = [d for d in dates if len(d) == 8]
    if not dates:
        pytest.skip("no snapshot index on this box")
    newest = datetime.strptime(max(dates), "%Y%m%d")
    return (newest - timedelta(days=window_days)).strftime("%Y%m%d")


def assert_has_recent(snap_ids, context: str = "") -> None:
    floor = recent_floor()
    assert any(snap_date(sid) >= floor for sid in snap_ids), (
        f"expected a snapshot dated {floor} or later (the newest snapshot minus "
        f"{RECENT_WINDOW_DAYS} days) in the top-k{context}, got: {list(snap_ids)}"
    )
