"""The snapshot list + detail routes the Timelines read are cheap and pageable.

Audit P04-media-tasks-fx-web-06 / rank 45: the Portal Timeline fetched /assert
(the whole 58 MB volume, hashed, inside a JSON string) on every open and every
SNAP-id click. It now pages GET /api/snapshots/recent and fetches one snapshot
at a time from GET /fossil/snapshot/{id}, so both must be cheap: the list reads
at most 4 KB of each listed snapshot for its preview (Android polls it every
10 s), and the detail route reads one snapshot's bytes.
"""
from __future__ import annotations

import pytest

from Orchestrator import fossils
from Orchestrator.config import VOL_PATH
from Orchestrator.tests.test_ledger_reads_per_turn import (  # noqa: F401 (fixtures)
    _block,
    ledger,
    volume_reads,
)


def _newest_first(index, operator=""):
    ids = [s for s, m in index.items() if not operator or m["operator"] == operator]
    return sorted(ids, key=lambda s: int(s.rsplit("-", 1)[1]), reverse=True)


def test_recent_list_reads_only_its_page(ledger, volume_reads):
    from Orchestrator.routes import task_routes as tr

    page = tr.api_snapshots_recent(limit=5)

    ids = [r["snap_id"] for r in page["results"]]
    assert ids == _newest_first(ledger)[:5]
    assert page["count"] == 5 and page["has_more"] is True
    assert volume_reads["bytes"] <= 5 * 4000
    assert volume_reads["bytes"] < VOL_PATH.stat().st_size // 4
    first = page["results"][0]
    assert first["operator"] == ledger[first["snap_id"]]["operator"]
    assert first["timestamp"] == ledger[first["snap_id"]]["timestamp"]
    assert first["type"] == "normal"
    assert first["snippet"].startswith("turn 120 for Ana — reranker latency")
    assert len(first["snippet"]) == 200


def test_recent_list_pages_back_with_before(ledger):
    from Orchestrator.routes import task_routes as tr

    seen, before, pages = [], "", 0
    while True:
        page = tr.api_snapshots_recent(limit=25, operator="Ben", before=before)
        seen += [r["snap_id"] for r in page["results"]]
        pages += 1
        if not page["has_more"]:
            break
        before = page["results"][-1]["snap_id"]
    assert seen == _newest_first(ledger, "Ben") and pages == 2
    assert all(ledger[s]["operator"] == "Ben" for s in seen)


def test_recent_list_names_every_operator_in_the_index(ledger):
    from Orchestrator.routes import task_routes as tr
    assert tr.api_snapshots_recent(limit=1)["operators"] == ["Ana", "Ben", "Cy"]


def test_recent_list_on_a_fresh_box(isolated_stores):
    from Orchestrator.routes import task_routes as tr
    assert tr.api_snapshots_recent() == {"results": [], "count": 0, "has_more": False,
                                         "operators": []}


def test_recent_list_caps_a_huge_limit(ledger, volume_reads):
    from Orchestrator.routes import task_routes as tr
    page = tr.api_snapshots_recent(limit=10_000_000)
    assert page["count"] == 120 and page["has_more"] is False


def test_recent_list_over_http_keeps_the_android_shape(ledger, app_client):
    """Android decodes {results:[{snap_id, operator, timestamp, type, snippet}],
    count} with ignoreUnknownKeys; the new keys are additive."""
    r = app_client.get("/api/snapshots/recent",
                       params={"limit": 3, "operator": "Cy", "before": "SNAP-20261001-100"})
    assert r.status_code == 200
    body = r.json()
    assert [x["snap_id"] for x in body["results"]] == [
        "SNAP-20261001-98", "SNAP-20261001-95", "SNAP-20261001-92"]
    assert set(body["results"][0]) == {"snap_id", "operator", "timestamp", "type", "snippet"}
    assert body["count"] == 3


def test_snapshot_detail_route_reads_one_snapshot(ledger, volume_reads, app_client):
    meta = ledger["SNAP-20261001-42"]
    r = app_client.get("/fossil/snapshot/SNAP-20261001-42")
    assert r.status_code == 200
    assert r.json()["content"].startswith("=== START SNAPSHOT — UTC 2026-10-01T00:42:00Z — SNAP-20261001-42")
    assert volume_reads["bytes"] == meta["byte_end"] - meta["byte_start"]
    assert app_client.get("/fossil/snapshot/SNAP-19990101-1").status_code == 404


def test_hybrid_search_route_reads_only_its_results(ledger, volume_reads, monkeypatch):
    from Orchestrator.routes import task_routes as tr
    ids = _newest_first(ledger, "Ana")[:3]
    monkeypatch.setattr(tr, "retrieve", lambda q, operator="", k=10: [(s, 0.5) for s in ids])

    resp = tr.fossil_hybrid_search(q="reranker", operator="Ana", limit=3)

    assert [r["snap_id"] for r in resp["results"]] == ids
    assert volume_reads["bytes"] == sum(ledger[s]["byte_end"] - ledger[s]["byte_start"] for s in ids)


def test_snapshot_detail_resolves_a_short_id_to_the_newest_match(ledger, app_client, monkeypatch):
    """Models write SNAP-<n> for SNAP-<date>-<n>; the Portal used to resolve it
    by scanning the whole ledger in the browser. The route resolves it now,
    newest match first; an exact id always wins."""
    r = app_client.get("/fossil/snapshot/SNAP-42")
    assert r.status_code == 200 and r.json()["snap_id"] == "SNAP-20261001-42"

    twin = dict(ledger)
    twin["SNAP-20261002-42"] = dict(ledger["SNAP-20261001-42"])
    monkeypatch.setattr(fossils, "load_snapshot_index", lambda: twin)
    assert app_client.get("/fossil/snapshot/SNAP-42").json()["snap_id"] == "SNAP-20261002-42"
    assert app_client.get("/fossil/snapshot/SNAP-20261001-42").json()["snap_id"] == "SNAP-20261001-42"
    assert app_client.get("/fossil/snapshot/SNAP-4242").status_code == 404
    assert app_client.get("/fossil/snapshot/42").status_code == 404
