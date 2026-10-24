"""GET /health stays small as the ledger grows.

Audit O05-ledger-persistence-09 / rank 45: /health parsed the whole snapshot
index (17 MB on the dev box) to count it and embedded the whole manifest
(1.7 MB, one entry per archived volume) in every response - and the Portal
polls it every 60 s per open tab, Android on every chat screen and the
updater while it waits for a restart. Every client reads `status`,
`snapshot_count`, `operator_turns`, `users` and `pairing`; the Portal's
Manifest dialog shows `latest_manifest`. That key now carries the manifest's
latest-volume fields plus an archive count; ?full=1 still returns all of it.
"""
from __future__ import annotations

import json

import pytest

from Orchestrator import config, fossils

ARCHIVES = 4000


def _write_manifest(n=ARCHIVES, sha="ab" * 32):
    manifest = {
        "latest_path": "Volumes/SNAPSHOT_VOLUME.txt",
        "latest_sha256": sha,
        "latest_utc": "2026-10-23T20:00:00Z",
        "archive": [{"utc": f"2026-01-01T00:00:{i % 60:02d}Z",
                     "path": f"Archive/Volume_{i}.txt", "sha256": "cd" * 32}
                    for i in range(n)],
    }
    config.MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    config.MANIFEST.write_text(json.dumps(manifest, indent=2))
    return manifest


def _write_goldmaster():
    import hashlib
    gm = b"gold master"
    config.GM_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.GM_PATH.write_bytes(gm)
    config.GM_HASH.write_text(hashlib.sha256(gm).hexdigest())


@pytest.fixture
def box(isolated_stores):
    _write_goldmaster()
    manifest = _write_manifest()
    fossils.save_snapshot_index({f"SNAP-20261001-{i}": {
        "byte_start": 0, "byte_end": 10, "operator": "Ana",
        "timestamp": "2026-10-01T00:00:00Z", "type": "normal"} for i in range(1, 38)})
    return manifest


def test_health_returns_a_compact_manifest_and_the_index_count(box, app_client):
    r = app_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["latest_manifest"] == {
        "latest_path": "Volumes/SNAPSHOT_VOLUME.txt",
        "latest_sha256": "ab" * 32,
        "latest_utc": "2026-10-23T20:00:00Z",
        "archive_count": ARCHIVES,
    }
    assert body["snapshot_count"] == 37
    assert len(r.content) < 16_000, f"/health is {len(r.content):,} bytes"
    # What the Portal, Android, cron and telephony panels read is unchanged.
    for key in ("status", "volume_bytes", "operator_turns", "users", "pairing",
                "checkpoint_config", "queue_size", "worker_running"):
        assert key in body


def test_health_full_still_returns_the_whole_manifest(box, app_client):
    body = app_client.get("/health", params={"full": 1}).json()
    assert body["latest_manifest"] == box
    assert body["snapshot_count"] == 37


def test_health_follows_a_rewritten_manifest(box, app_client):
    assert app_client.get("/health").json()["latest_manifest"]["archive_count"] == ARCHIVES
    import os
    _write_manifest(n=3, sha="ef" * 32)
    st = config.MANIFEST.stat()
    os.utime(config.MANIFEST, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
    summary = app_client.get("/health").json()["latest_manifest"]
    assert summary["archive_count"] == 3 and summary["latest_sha256"] == "ef" * 32


def test_health_on_a_box_without_a_manifest_or_index(isolated_stores, app_client):
    _write_goldmaster()
    config.MANIFEST.unlink(missing_ok=True)
    r = app_client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["latest_manifest"] == {
        "latest_path": "", "latest_sha256": "", "latest_utc": "", "archive_count": 0}
    assert r.json()["snapshot_count"] == 0


def test_health_reports_a_missing_goldmaster_instead_of_failing(isolated_stores, app_client):
    config.GM_PATH.unlink(missing_ok=True)
    r = app_client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "GM FAIL"
