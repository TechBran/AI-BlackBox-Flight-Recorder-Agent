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
    monkeypatch.setattr(volume, "_latest_copy", None)
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
    monkeypatch.setattr(admin_routes, "read_tail_text", lambda _p: "=== END SNAPSHOT — SNAP-20260101-001 — UTC 2026-01-01T00:00:00Z ===\n")
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
    assert sorted(stats["would_delete"]) == sorted(p.name for p in paths[:-1])   # never the newest
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


@pytest.mark.parametrize("rule", [
    {"days_to_keep": 0, "max_size_gb": 50.0, "max_files": 200},       # age
    {"days_to_keep": 3650, "max_size_gb": 0.0, "max_files": 200},     # size
    {"days_to_keep": 3650, "max_size_gb": 50.0, "max_files": 0},      # count
])
def test_retention_never_deletes_the_newest_copy(ledger, rule):
    """The age rule now runs hourly: an idle box (or one whose every archive
    was skipped for low disk) must still keep its newest ledger backup."""
    paths = _archives(ledger["arc"], [1, 1, 1])
    for p in paths:
        os.utime(p, (time.time() - 40 * 86400, p.stat().st_mtime - 40 * 86400))
    volume.cleanup_old_archives(**rule)
    assert [p.exists() for p in paths] == [False, False, True]


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



def _snap(n: int) -> bytes:
    sid = f"SNAP-20260101-{n:03d}"
    return (f"=== START SNAPSHOT — UTC 2026-01-01T00:00:00Z — {sid} (7.1.0) ===\n"
            f"body {n}\n"
            f"=== END SNAPSHOT — {sid} — UTC 2026-01-01T00:00:00Z ===\n").encode()


def _manifest(ledger):
    return json.loads(ledger["manifest"].read_text())


def test_an_archive_copy_is_the_committed_prefix_never_a_concurrent_append(ledger, monkeypatch):
    """Mints archive after mint_lock is released, so another mint may be half
    way through its append: the copy is the size this mint committed."""
    monkeypatch.setattr(volume, "archive_min_free_gb", lambda: 0.0)
    a = _snap(1)
    ledger["vol"].write_bytes(a + _snap(2)[:40])          # mint 2 is mid-append
    arc, digest, _utc = volume.archive_volume(upto=len(a))
    assert Path(arc).read_bytes() == a
    assert digest == hashlib.sha256(a).hexdigest() == _manifest(ledger)["latest_sha256"]
    volume.parse_tail(Path(arc).read_text())               # a whole ledger, not torn


def test_an_archive_without_a_size_measures_it_under_mint_lock(ledger, monkeypatch):
    import threading
    from Orchestrator import state
    monkeypatch.setattr(volume, "archive_min_free_gb", lambda: 0.0)
    a, b = _snap(1), _snap(2)
    ledger["vol"].write_bytes(a)
    out = {}
    with state.mint_lock:                                  # a mint is appending
        with open(ledger["vol"], "ab") as f:
            f.write(b[:40])
        worker = threading.Thread(target=lambda: out.setdefault("arc", volume.archive_volume()))
        worker.start()
        worker.join(0.3)
        assert "arc" not in out, "the copy did not wait for the mint in flight"
        with open(ledger["vol"], "ab") as f:
            f.write(b[40:])
    worker.join(5)
    assert Path(out["arc"][0]).read_bytes() == a + b


def test_an_out_of_order_archive_never_regresses_the_manifest(ledger, monkeypatch):
    """Two mints' post-lock archive steps finish in reverse order: the older
    prefix is already inside the newer copy, so it is not copied again and
    the manifest keeps describing the newest committed ledger."""
    monkeypatch.setattr(volume, "archive_min_free_gb", lambda: 0.0)
    a, b = _snap(1), _snap(2)
    ledger["vol"].write_bytes(a + b)
    newer = volume.archive_volume(upto=len(a + b))
    older = volume.archive_volume(upto=len(a))
    man = _manifest(ledger)
    assert man["latest_sha256"] == hashlib.sha256(a + b).hexdigest()
    assert len(man["archive"]) == 1 and len(list(ledger["arc"].glob("Volume_*.txt"))) == 1
    assert older[:2] == newer[:2]                          # covered by the newer copy


def test_copies_made_in_the_same_second_never_replace_each_other(ledger, monkeypatch):
    monkeypatch.setattr(volume, "archive_min_free_gb", lambda: 0.0)
    monkeypatch.setattr(volume, "now_utc_iso", lambda: "2026-01-01T00:00:00Z")
    a, b = _snap(1), _snap(2)
    ledger["vol"].write_bytes(a)
    first = volume.archive_volume(upto=len(a))
    ledger["vol"].write_bytes(a + b)
    second = volume.archive_volume(upto=len(a + b))
    assert first[0] != second[0]
    for entry in _manifest(ledger)["archive"]:
        assert hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
    assert Path(first[0]).read_bytes() == a and Path(second[0]).read_bytes() == a + b


def test_a_replaced_ledger_is_archived_again(ledger, monkeypatch):
    """The out-of-order rule compares copies of the same ledger file only."""
    monkeypatch.setattr(volume, "archive_min_free_gb", lambda: 0.0)
    ledger["vol"].write_bytes(_snap(1) + _snap(2))
    volume.archive_volume(upto=len(_snap(1) + _snap(2)))
    fresh = ledger["vol"].with_name("fresh.txt")
    fresh.write_bytes(_snap(3))
    os.replace(fresh, ledger["vol"])                       # a new ledger file
    arc, digest, _ = volume.archive_volume(upto=len(_snap(3)))
    assert Path(arc).read_bytes() == _snap(3)
    assert _manifest(ledger)["latest_sha256"] == digest


def test_min_free_gb_is_read_fresh_from_config_ini(tmp_path, monkeypatch):
    from Orchestrator.utils import config_ini
    ini = tmp_path / "config.ini"
    ini.write_text("[archive]\nmin_free_gb = 3\n")
    monkeypatch.setattr(config_ini, "DEFAULT_PATH", ini)
    assert volume.archive_min_free_gb() == 3.0
    ini.write_text("[archive]\nmin_free_gb = 7\n")       # a hand edit, no restart
    assert volume.archive_min_free_gb() == 7.0
    ini.write_text("[archive]\nmin_free_gb = lots\n")
    assert volume.archive_min_free_gb() == 2.0


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
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "expired.txt").write_text("old")
    os.utime(artifacts / "expired.txt", (time.time() - 400 * 86400, time.time() - 400 * 86400))
    monkeypatch.setattr(volume, "ARTIFACTS_DIR", artifacts)

    report = maintenance.run_storage_maintenance()

    assert [p.exists() for p in archives] == [False, False, False, True, True]
    assert not old.exists() and new.exists()
    assert report["archives"]["deleted_files"] == 3
    assert report["upload_sessions_removed"] == 1
    assert not (artifacts / "expired.txt").exists()


def test_storage_maintenance_is_scheduled_by_a_gated_boot_hook():
    from Orchestrator import startup
    from Orchestrator.utils.testing import BOOT_HOOK_ATTR
    hook = getattr(startup, "startup_storage_maintenance", None)
    assert hook is not None, "nothing schedules archive and upload cleanup"
    from Orchestrator.lifespan import lifespan_of
    registered = [h for h in lifespan_of(startup.app).hooks("startup")
                  if getattr(h, "__wrapped__", None) is hook]
    assert registered and getattr(registered[0], BOOT_HOOK_ATTR, False)
