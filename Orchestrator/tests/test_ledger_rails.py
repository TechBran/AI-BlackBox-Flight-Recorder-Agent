"""Ledger safety rails: a domain error instead of the unimported
HTTPException, a dry run that is really dry, an archive directory bounded by
a schedule and a free-space floor (audit O05-ledger-persistence-01/-02/-10/
-24, X06-ops-deploy-04)."""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import namedtuple
from pathlib import Path

import pytest

from Orchestrator import volume

GB = 1024 ** 3
DiskUsage = namedtuple("DiskUsage", "total used free")


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    vol = tmp_path / "Volumes" / "SNAPSHOT_VOLUME.txt"
    vol.parent.mkdir()
    arc = tmp_path / "Archive"
    arc.mkdir()
    manifest = tmp_path / "Manifest" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"archive": []}))
    gm = tmp_path / "GoldMaster.txt"
    gm.write_bytes(b"gold master")
    gm_hash = tmp_path / "GoldMaster.sha256"
    gm_hash.write_text(hashlib.sha256(b"gold master").hexdigest())
    for name, value in (("VOL_PATH", vol), ("ARC_DIR", arc), ("MANIFEST", manifest),
                        ("GM_PATH", gm), ("GM_HASH", gm_hash)):
        monkeypatch.setattr(volume, name, value)
    return {"vol": vol, "arc": arc, "manifest": manifest, "gm": gm, "gm_hash": gm_hash}


def _archives(arc_dir: Path, sizes_mb):
    """Archive copies oldest first, mtimes one hour apart, the newest an hour
    ago."""
    start = time.time() - (len(sizes_mb) + 1) * 3600
    paths = []
    for i, size in enumerate(sizes_mb):
        p = arc_dir / f"Volume_2026-01-{i + 1:02d}T00-00-00Z.txt"
        p.write_bytes(b"x" * int(size * 1024 * 1024))
        os.utime(p, (start + i * 3600, start + i * 3600))
        paths.append(p)
    return paths


# ── a domain error, not a NameError ─────────────────────────────────────────

def test_torn_tail_raises_a_ledger_error():
    torn = "=== END SNAPSHOT — SNAP-20260101-001 — UTC 2026-01-01T00:00:00Z ===\n"
    with pytest.raises(volume.LedgerError) as exc:
        volume.parse_tail(torn)
    assert exc.value.detail == "PARTIAL-SNAPSHOT"


def test_goldmaster_checks_raise_a_ledger_error(ledger):
    ledger["gm_hash"].write_text("0" * 64)
    with pytest.raises(volume.LedgerError) as exc:
        volume.verify_gm_or_halt()
    assert exc.value.detail.startswith("GM-HASH-MISMATCH")
    ledger["gm"].unlink()
    with pytest.raises(volume.LedgerError, match="GM-NOT-FOUND"):
        volume.verify_gm_or_halt()


def test_health_reports_gm_fail_instead_of_500(ledger, app_client, monkeypatch):
    from Orchestrator.routes import admin_routes
    monkeypatch.setattr(admin_routes, "MANIFEST", ledger["manifest"])
    ledger["gm_hash"].write_text("0" * 64)
    r = app_client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "GM FAIL"
    assert "GM-HASH-MISMATCH" in r.json()["detail"]


def test_ledger_error_from_a_route_is_a_500_with_its_detail(ledger, app_client, monkeypatch):
    from Orchestrator.routes import admin_routes
    monkeypatch.setattr(admin_routes, "read_text_safe", lambda _p: "=== END SNAPSHOT — SNAP-20260101-001 — UTC 2026-01-01T00:00:00Z ===\n")
    monkeypatch.setattr(admin_routes, "parse_tail", volume.parse_tail)
    r = app_client.get("/tail")
    assert r.status_code == 500
    assert r.json()["detail"] == "PARTIAL-SNAPSHOT"


# ── cleanup: dry run, free-space floor, temp litter ─────────────────────────

def test_dry_run_deletes_nothing(ledger):
    paths = _archives(ledger["arc"], [1, 1, 1])
    stats = volume.cleanup_old_archives(days_to_keep=0, max_size_gb=5.0, max_files=200, dry_run=True)
    assert all(p.exists() for p in paths)
    assert stats["dry_run"] is True
    assert sorted(stats["would_delete"]) == sorted(p.name for p in paths)
    assert stats["deleted_files"] == 0


def test_dry_run_route_deletes_nothing(ledger, app_client):
    paths = _archives(ledger["arc"], [1, 1])
    r = app_client.post("/cleanup/archives", params={"days_to_keep": 0, "dry_run": "true"})
    assert r.status_code == 200
    assert r.json()["dry_run"] is True
    assert all(p.exists() for p in paths)


def test_free_space_floor_deletes_oldest_but_keeps_the_newest(ledger, monkeypatch):
    paths = _archives(ledger["arc"], [2, 2, 2, 2])          # newest last
    # 1 MB free, floor 5 MB: deleting the two oldest (4 MB) reaches 5 MB.
    monkeypatch.setattr(volume.shutil, "disk_usage",
                        lambda _p: DiskUsage(100 * GB, 100 * GB - 1024 * 1024, 1024 * 1024))
    stats = volume.cleanup_old_archives(days_to_keep=3650, max_size_gb=50, max_files=200,
                                        min_free_gb=5 * 1024 * 1024 / GB)
    assert [p.exists() for p in paths] == [False, False, True, True]
    assert "free_space" in stats["reason"]
    # Even an unreachable floor never deletes the newest copy.
    monkeypatch.setattr(volume.shutil, "disk_usage", lambda _p: DiskUsage(100 * GB, 100 * GB, 0))
    volume.cleanup_old_archives(days_to_keep=3650, max_size_gb=50, max_files=200, min_free_gb=1000)
    assert [p.exists() for p in paths] == [False, False, False, True]


def test_cleanup_sweeps_stale_temp_litter_only(ledger):
    arc = ledger["arc"]
    stale = [arc / "Volume_2026-01-01T00-00-00Z.txt.part", arc / ".Volume_2026-01-02T00-00-00Z.txt.ab12.tmp"]
    for p in stale:
        p.write_text("partial")
        os.utime(p, (time.time() - 7200, time.time() - 7200))
    fresh = arc / ".Volume_2026-01-03T00-00-00Z.txt.cd34.tmp"
    fresh.write_text("a write in flight")
    volume.cleanup_old_archives(dry_run=True)
    assert all(p.exists() for p in stale)
    volume.cleanup_old_archives()
    assert not any(p.exists() for p in stale)
    assert fresh.exists()


# ── archive copy: durable, one read, skipped near a full disk ───────────────

def test_archive_copy_records_the_manifest_and_leaves_no_part_file(ledger, monkeypatch):
    monkeypatch.setattr(volume, "archive_min_free_gb", lambda: 0.0)
    ledger["vol"].write_bytes(b"ledger bytes")
    arc, digest, _utc = volume.archive_volume()
    assert Path(arc).read_bytes() == b"ledger bytes"
    assert digest == hashlib.sha256(b"ledger bytes").hexdigest()
    man = json.loads(ledger["manifest"].read_text())
    assert man["latest_sha256"] == digest and len(man["archive"]) == 1
    assert [p.name for p in ledger["arc"].iterdir()] == [Path(arc).name]


def test_archive_copy_is_skipped_below_the_free_space_floor(ledger, monkeypatch):
    ledger["vol"].write_bytes(b"x" * 1024)
    monkeypatch.setattr(volume.shutil, "disk_usage", lambda _p: DiskUsage(100 * GB, 100 * GB - 512, 512))
    with pytest.raises(volume.LedgerError, match="ARCHIVE-SKIPPED-LOW-DISK"):
        volume.archive_volume()
    assert list(ledger["arc"].iterdir()) == []


# ── the schedule ─────────────────────────────────────────────────────────────

def test_storage_maintenance_bounds_archives_and_upload_sessions(ledger, tmp_path, monkeypatch):
    from Orchestrator import maintenance
    sessions = tmp_path / "uploads" / "sessions"
    old, new = sessions / "session-old", sessions / "session-new"
    for d in (old, new):
        d.mkdir(parents=True)
        (d / "photo.jpg").write_bytes(b"jpg")
    os.utime(old, (time.time() - 5 * 86400, time.time() - 5 * 86400))
    monkeypatch.setattr(maintenance, "session_uploads_dir", lambda: sessions)
    archives = _archives(ledger["arc"], [1] * 5)
    monkeypatch.setattr(maintenance, "ARCHIVE_RETENTION",
                        {"days_to_keep": 3650, "max_size_gb": 50.0, "max_files": 2})
    monkeypatch.setattr(volume, "archive_min_free_gb", lambda: 0.0)

    report = maintenance.run_storage_maintenance()

    assert [p.exists() for p in archives] == [False, False, False, True, True]
    assert not old.exists() and new.exists()
    assert report["archives"]["deleted_files"] == 3
    assert report["upload_sessions_removed"] == 1


def test_storage_maintenance_is_scheduled_by_a_gated_boot_hook():
    from Orchestrator import startup
    from Orchestrator.utils.testing import BOOT_HOOK_ATTR
    hook = getattr(startup, "startup_storage_maintenance", None)
    assert hook is not None, "nothing schedules archive and upload cleanup"
    registered = [h for h in startup.app.router.on_startup
                  if getattr(h, "__wrapped__", None) is hook]
    assert registered and getattr(registered[0], BOOT_HOOK_ATTR, False)
