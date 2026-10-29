"""Scheduled archive copies, the manifest kept to the copies that exist, and
the ledger check (audit rank 77: O05-ledger-persistence-02/-03).

A mint no longer copies the whole ledger into Archive/ (up to 1 GB a day); it
records the ledger's prefix hash (test_mint_path), and the maintenance pass
makes one full copy a day, keeping [archive] keep_copies. The manifest's
archive[] lists only copies that still exist, with ISO times. The ledger
check (Orchestrator/ledger_fsck.py, scripts/ledger_fsck.py) verifies every
index entry's byte range, the prefix-hash chain and the copies, and /health
shows its counts. Every test runs on files under tmp_path.
"""
from __future__ import annotations

import gc
import hashlib
import json
import os
import subprocess
import sys
import time
import tracemalloc
from collections import namedtuple
from pathlib import Path

import pytest

from Orchestrator import fossils, ledger, ledger_fsck, volume

GB = 1024 ** 3
DiskUsage = namedtuple("DiskUsage", "total used free")
REPO = Path(__file__).resolve().parents[2]


def _snap(n: int, op: str = "Ana", body: str = "") -> str:
    sid = f"SNAP-20261001-{n}"
    utc = f"2026-10-01T00:{n % 60:02d}:00Z"
    return (f"=== START SNAPSHOT — UTC {utc} — {sid} (7.1.0) ===\n"
            f"OPERATOR: {op}\nRaw Session Log\n- [1] {utc} operator={op} user: "
            f"{body or f'turn {n} déjà vu 🙂'}\n"
            f"=== END SNAPSHOT — {sid} — UTC {utc} ===\n")


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A box root: ledger, Manifest/, Archive/, with volume.py pointed at it."""
    root = tmp_path / "ledgerbox"
    vol = root / "Volumes" / "SNAPSHOT_VOLUME.txt"
    man = root / "Manifest" / "manifest.json"
    arc = root / "Archive"
    for d in (vol.parent, man.parent, arc):
        d.mkdir(parents=True)
    man.write_text(json.dumps({"latest_path": "", "latest_sha256": "", "latest_utc": "", "archive": []}))
    for name, value in (("VOL_PATH", vol), ("MANIFEST", man), ("ARC_DIR", arc)):
        monkeypatch.setattr(volume, name, value)
    monkeypatch.setattr(volume, "_latest_copy", None)
    monkeypatch.setattr(volume, "_latest_published", None)
    monkeypatch.setattr(volume, "_PREFIX", ledger.PrefixHasher())
    monkeypatch.setattr(volume, "_counts_cache", {})
    monkeypatch.setattr(volume, "archive_min_free_gb", lambda: 0.0)
    ns = type("Box", (), {})()
    ns.root, ns.vol, ns.man, ns.arc = root, vol, man, arc
    ns.index_path = man.parent / "snapshot_index.json"
    return ns


def _mint(box, n, op="Ana", body=""):
    """What every writer does: append, index, record (index kept in a file)."""
    start = box.vol.stat().st_size if box.vol.exists() else 0
    volume.append_snapshot_text(_snap(n, op, body))
    end = box.vol.stat().st_size
    index = json.loads(box.index_path.read_text()) if box.index_path.exists() else {}
    index[f"SNAP-20261001-{n}"] = {"byte_start": start, "byte_end": end, "operator": op,
                                   "timestamp": "2026-10-01T00:00:00Z", "type": "normal"}
    box.index_path.write_text(json.dumps(index))
    rec = volume.record_append(f"SNAP-20261001-{n}", start, end, "2026-10-01T00:00:00Z")
    volume.publish_latest(rec)
    return rec


def _manifest(box):
    return json.loads(box.man.read_text())


def _age(path: Path, hours: float):
    t = time.time() - hours * 3600
    os.utime(path, (t, t))


def _a_day_passes(arc: Path):
    for p in arc.glob("Volume_*.txt"):
        t = p.stat().st_mtime - 25 * 3600
        os.utime(p, (t, t))


# -- the scheduled copy ---------------------------------------------------------------

def test_a_box_with_no_copy_gets_one(box):
    for n in range(1, 6):
        _mint(box, n)
    out = volume.scheduled_archive_copy()
    assert out["copied"] is True
    copy = box.arc / out["path"]
    ledger_bytes = box.vol.read_bytes()
    assert copy.read_bytes() == ledger_bytes
    assert out["sha256"] == hashlib.sha256(ledger_bytes).hexdigest()
    [entry] = _manifest(box)["archive"]
    assert entry == {"utc": out["utc"], "path": str(copy.as_posix()), "sha256": out["sha256"],
                     "bytes": len(ledger_bytes)}
    assert entry["utc"].count(":") == 2 and "T" in entry["utc"]   # ISO, not 2026:10:01T..
    assert [p.name for p in box.arc.iterdir()] == [copy.name]     # no temp left behind


def test_a_copy_is_made_once_a_day_and_only_when_the_ledger_grew(box):
    _mint(box, 1)
    first = volume.scheduled_archive_copy(interval_hours=24)
    assert first["copied"]
    assert volume.scheduled_archive_copy(interval_hours=24)["reason"] == "not due"
    _age(box.arc / first["path"], 25)
    assert volume.scheduled_archive_copy(interval_hours=24)["reason"] == "unchanged"   # idle box
    _mint(box, 2)
    second = volume.scheduled_archive_copy(interval_hours=24)
    assert second["copied"] and (box.arc / second["path"]).read_bytes() == box.vol.read_bytes()
    assert len(list(box.arc.glob("Volume_*.txt"))) == 2


def test_a_copy_never_crosses_the_free_space_floor(box, monkeypatch):
    _mint(box, 1)
    monkeypatch.setattr(volume, "archive_min_free_gb", lambda: 1.0)
    monkeypatch.setattr(volume.shutil, "disk_usage", lambda _p: DiskUsage(100 * GB, 100 * GB - 512, 512))
    with pytest.raises(volume.LedgerError, match="ARCHIVE-SKIPPED-LOW-DISK"):
        volume.scheduled_archive_copy()
    assert list(box.arc.iterdir()) == []


def test_a_full_copy_streams_instead_of_holding_the_ledger(box):
    box.vol.write_bytes("".join(_snap(n, body="filler " * 900) for n in range(1, 900)).encode())
    size = box.vol.stat().st_size
    gc.collect()
    tracemalloc.start()
    arc, digest, _utc = volume.archive_volume(upto=size)
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert Path(arc).read_bytes() == box.vol.read_bytes()
    assert digest == hashlib.sha256(box.vol.read_bytes()).hexdigest()
    assert peak < size // 4, f"the copy held {peak:,} bytes of a {size:,}-byte ledger"


def test_maintenance_copies_keeps_n_and_checks_the_ledger(box, monkeypatch, tmp_path):
    from Orchestrator import maintenance
    monkeypatch.setattr(maintenance, "session_uploads_dir", lambda: tmp_path / "none")
    monkeypatch.setattr(volume, "ARTIFACTS_DIR", tmp_path / "no-artifacts")
    monkeypatch.setattr(volume, "archive_keep_copies", lambda: 3)
    monkeypatch.setattr(fossils, "load_snapshot_index", lambda: json.loads(box.index_path.read_text()))
    for day in range(6):
        _mint(box, day + 1)
        report = maintenance.run_storage_maintenance()
        assert report["errors"] == [], report["errors"]
        assert report["archive_copy"]["copied"] is True
        _a_day_passes(box.arc)
    copies = sorted(box.arc.glob("Volume_*.txt"), key=lambda p: p.stat().st_mtime)
    assert len(copies) == 3
    assert copies[-1].read_bytes() == box.vol.read_bytes()
    entries = _manifest(box)["archive"]
    assert sorted(Path(e["path"]).name for e in entries) == sorted(p.name for p in copies)
    fsck = volume.fsck_report_path()
    assert fsck.exists() and json.loads(fsck.read_text())["ok"] is True
    # Checked once a day, not on every hourly pass.
    assert volume.ledger_fsck_due() is False


# -- the manifest lists only copies that exist ------------------------------------------

def _legacy_manifest(box, n_files=4, n_entries=40):
    """The shape every box has today: one entry per mint ever made, most of
    their files long deleted, times mangled to 2026:10:08T11-05-55Z."""
    files = []
    for i in range(n_files):
        p = box.arc / f"Volume_2026-10-0{i + 1}T11-05-55Z.txt"
        p.write_bytes(b"x" * 10)
        _age(p, 24 * (n_files - i))
        files.append(p)
    entries = [{"utc": f"2026:09:{i % 28 + 1:02d}T11-05-55Z",
                "path": str(box.arc / f"Volume_2026-09-{i % 28 + 1:02d}T11-05-{i:02d}Z.txt"),
                "sha256": "0" * 64} for i in range(n_entries - n_files)]
    entries += [{"utc": f"2026:10:0{i + 1}T11-05-55Z", "path": str(p), "sha256": "1" * 64}
                for i, p in enumerate(files)]
    box.man.write_text(json.dumps({"latest_path": "Volumes/SNAPSHOT_VOLUME.txt",
                                   "latest_sha256": "1" * 64,
                                   "latest_utc": "2026:10:04T11-05-55Z", "archive": entries}))
    return files


def test_cleanup_drops_entries_for_deleted_copies_and_fixes_their_times(box):
    files = _legacy_manifest(box)
    stats = volume.cleanup_old_archives(days_to_keep=3650, max_size_gb=50, max_files=2)
    assert [p.exists() for p in files] == [False, False, True, True]
    man = _manifest(box)
    assert [Path(e["path"]).name for e in man["archive"]] == [p.name for p in files[2:]]
    assert [e["utc"] for e in man["archive"]] == ["2026-10-03T11:05:55Z", "2026-10-04T11:05:55Z"]
    assert man["latest_utc"] == "2026-10-04T11:05:55Z"
    assert stats["manifest_entries_pruned"] == 38


def test_a_dry_run_leaves_the_manifest_alone(box):
    _legacy_manifest(box)
    before = box.man.read_bytes()
    stats = volume.cleanup_old_archives(days_to_keep=3650, max_size_gb=50, max_files=2, dry_run=True)
    assert stats["manifest_entries_pruned"] == 38 and box.man.read_bytes() == before


def test_an_empty_archive_still_prunes_the_manifest(box):
    _legacy_manifest(box, n_files=0, n_entries=12)
    volume.cleanup_old_archives()
    assert _manifest(box)["archive"] == []


# -- /artifact and /health --------------------------------------------------------------

def test_artifact_finds_a_copy_by_its_recorded_iso_time(box, app_client, monkeypatch):
    from Orchestrator.routes import admin_routes
    monkeypatch.setattr(admin_routes, "MANIFEST", box.man)
    monkeypatch.setattr(admin_routes, "ARC_DIR", box.arc)
    _mint(box, 1)
    out = volume.scheduled_archive_copy()
    r = app_client.get("/artifact", params={"which": "x", "utc": out["utc"]})
    assert r.status_code == 200 and r.content == box.vol.read_bytes()
    # which=latest serves the newest copy that still exists.
    man = _manifest(box)
    man["archive"].append({"utc": "2026-12-01T00:00:00Z", "path": str(box.arc / "gone.txt"), "sha256": ""})
    box.man.write_text(json.dumps(man))
    assert app_client.get("/artifact").content == box.vol.read_bytes()
    assert app_client.get("/artifact", params={"which": "x", "utc": "../x"}).status_code == 400


def test_health_reports_the_ledger_counts(box, app_client, monkeypatch):
    from Orchestrator.routes import admin_routes
    monkeypatch.setattr(admin_routes, "MANIFEST", box.man)
    monkeypatch.setattr(admin_routes, "VOL_PATH", box.vol)
    fresh = app_client.get("/health").json()["ledger"]
    assert fresh["bytes"] == 0 and fresh["hash_records"] == 0 and fresh["fsck"] is None
    assert fresh["archive_copies"] == 0
    for n in range(1, 4):
        last = _mint(box, n)
    volume.scheduled_archive_copy()
    index = json.loads(box.index_path.read_text())
    monkeypatch.setattr(admin_routes, "load_snapshot_index", lambda: index)
    volume.run_ledger_fsck(index=index)
    body = app_client.get("/health").json()
    block = body["ledger"]
    assert block["bytes"] == box.vol.stat().st_size == block["indexed_bytes"]
    assert block["snapshots_indexed"] == 3 and block["unindexed_tail_bytes"] == 0
    assert block["hash_records"] == 3 and block["last_hash_record"]["sha256"] == last["sha256"]
    assert block["archive_copies"] == 1 and block["archive_bytes"] == box.vol.stat().st_size
    assert block["fsck"]["ok"] is True and block["fsck"]["hash_chain"]["verified"] == 3
    assert "problems" not in block["fsck"]
    # The M4 manifest summary is still there, extended.
    assert body["latest_manifest"]["latest_sha256"] == last["sha256"]
    assert body["latest_manifest"]["latest_bytes"] == box.vol.stat().st_size


def test_the_dashboard_shows_the_retention_the_pass_applies(app_client, monkeypatch):
    monkeypatch.setattr(volume, "archive_keep_copies", lambda: 7)
    settings = app_client.get("/dashboard").json()["archive_status"]["cleanup_settings"]
    assert settings == {"days_to_keep": 30, "max_size_gb": 5.0, "max_files": 7}


# -- the ledger check -------------------------------------------------------------------

def _check(box, **kw):
    return ledger_fsck.run(box.vol, json.loads(box.index_path.read_text()),
                           volume.ledger_hashes_path(), box.man, box.arc, **kw)


def _healthy(box, n=12):
    for i in range(1, n + 1):
        _mint(box, i, op=("Ana", "Ben")[i % 2])
        if i in (4, 9):
            volume.archive_volume()


def test_a_healthy_ledger_checks_out(box):
    _healthy(box)
    r = _check(box, verify_copies=True)
    assert r["ok"] is True and r["failures"] == 0 and r["warnings"] == 0, r["problems"]
    assert r["snapshots"]["verified"] == 12
    assert r["hash_chain"]["records"] == r["hash_chain"]["verified"] == 12
    assert r["hash_chain"]["breaks"] == 0 and r["hash_chain"]["first_byte"] == 0
    assert r["archives"]["copies"] == r["archives"]["verified"] == 2
    assert r["ledger"]["sha256"] == hashlib.sha256(box.vol.read_bytes()).hexdigest()


def test_a_rewritten_byte_breaks_the_chain_and_the_copies(box):
    _healthy(box)
    data = bytearray(box.vol.read_bytes())
    at = data.index("turn 3".encode())
    data[at] = ord("T")                            # same size, one byte changed
    box.vol.write_bytes(bytes(data))
    r = _check(box)
    assert r["ok"] is False
    assert r["hash_chain"]["mismatched"] == 10 and r["hash_chain"]["verified"] == 2
    assert r["archives"]["not_prefix"] == 2
    assert r["snapshots"]["verified"] == 12        # the markers are intact: only the hashes see it
    assert any("were changed" in p for p in r["problems"])


def test_index_entries_pointing_at_the_wrong_bytes_fail(box):
    _healthy(box, n=6)
    index = json.loads(box.index_path.read_text())
    index["SNAP-20261001-2"]["byte_start"] += 3                      # shifted
    index["SNAP-20261001-4"]["byte_end"] = index["SNAP-20261001-5"]["byte_end"]  # swallows 5
    index["SNAP-20261001-6"]["byte_end"] = box.vol.stat().st_size + 99           # past EOF
    r = ledger_fsck.run(box.vol, index, volume.ledger_hashes_path(), box.man, box.arc)
    s = r["snapshots"]
    assert r["ok"] is False
    assert s["bad_start"] == 1 and s["overlapping"] == 1 and s["out_of_bounds"] == 1
    assert s["bad_end"] == 1                       # 4 now ends with 5's END marker


def test_unindexed_duplicate_and_torn_snapshots_are_warnings(box):
    _healthy(box, n=4)
    volume.append_snapshot_text(_snap(50))         # appended, never indexed
    volume.append_snapshot_text(_snap(2))          # the same id twice
    volume.append_snapshot_text(_snap(51)[:70])    # an interrupted append
    r = _check(box)
    s = r["snapshots"]
    assert r["ok"] is True and r["warnings"] == 3
    assert s["unindexed"] == 1 and s["duplicate_ids"] == 1 and s["torn"] == 1 and s["tail_torn"]


def test_a_mint_during_the_check_is_not_a_torn_tail(box):
    """The app checks up to the size it measured under mint_lock; an append in
    flight past it is not reported."""
    _healthy(box, n=3)
    committed = box.vol.stat().st_size
    with open(box.vol, "ab") as f:
        f.write(_snap(4).encode()[:90])            # a mint half way through
    assert _check(box, size=committed)["warnings"] == 0
    assert _check(box)["snapshots"]["tail_torn"] is True


def test_records_out_of_order_broken_or_unreadable_are_counted(box):
    _healthy(box, n=5)
    path = volume.ledger_hashes_path()
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2], lines[1], "not json", lines[4]]) + "\n")
    c = _check(box)["hash_chain"]
    assert c["bad_lines"] == 1 and c["unordered"] == 1 and c["breaks"] == 2
    assert c["mismatched"] == 0                    # every hash still matches its prefix


def test_a_corrupt_copy_is_found_only_when_copies_are_hashed(box):
    _healthy(box, n=5)
    copy = Path(volume.archive_volume()[0])
    data = bytearray(copy.read_bytes())
    data[10] ^= 1
    copy.write_bytes(bytes(data))
    assert _check(box)["ok"] is True               # the ledger still begins with what was recorded
    r = _check(box, verify_copies=True)
    assert r["ok"] is False and r["archives"]["copy_mismatched"] == 1


# -- scripts/ledger_fsck.py --------------------------------------------------------------

def _cli(box, *args):
    """The script on the box's files, from a directory with no config.ini:
    it must not load the app's configuration module (which would exit)."""
    (box.root / "config.ini").write_text(
        "[paths]\nvolume = ./Volumes/SNAPSHOT_VOLUME.txt\narchive = ./Archive/\n"
        "manifest = ./Manifest/manifest.json\n")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH",)}
    return subprocess.run([sys.executable, "-I", str(REPO / "scripts" / "ledger_fsck.py"),
                           "--root", str(box.root), *args],
                          cwd=str(box.root / "Archive"), env=env,
                          capture_output=True, text=True, timeout=60)


def _tree(root: Path):
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file() and p.name != "config.ini"}


def test_the_script_checks_a_box_read_only(box):
    _healthy(box)
    before = _tree(box.root)
    ok = _cli(box)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "12 entries: 12 verified" in ok.stdout and ok.stdout.rstrip().endswith("OK")
    as_json = _cli(box, "--json")
    assert json.loads(as_json.stdout)["hash_chain"]["verified"] == 12
    assert _tree(box.root) == before               # nothing written
    recorded = _cli(box, "--record")
    assert recorded.returncode == 0
    assert json.loads((box.man.parent / "ledger_fsck.json").read_text())["ok"] is True


def test_the_script_fails_on_a_changed_ledger_and_on_no_ledger(box):
    _healthy(box)
    data = bytearray(box.vol.read_bytes())
    data[200] ^= 1
    box.vol.write_bytes(bytes(data))
    bad = _cli(box)
    assert bad.returncode == 1 and "FAILED" in bad.stdout
    box.vol.unlink()
    assert _cli(box).returncode == 2


def test_the_real_ledger_shapes_check_out():
    """The first days of the ledger wrote STARTs with no closing ===, ENDs
    with a version or a zero-width space before them, and ENDs with no ===;
    index entries end at the END's === or after its newline. All of those are
    healthy; whole snapshots the index never listed are warnings."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "SNAPSHOT_VOLUME.txt"
        blocks = [
            "=== START SNAPSHOT — UTC 2025-09-01T00:00:00Z — SNAP-20250901-1 (2.0.0)\r\n\r\nbody\r\n"
            "=== END SNAPSHOT — SNAP-20250901-1 — UTC 2025-09-01T00:00:00Z\r\n\r\n",
            "=== START SNAPSHOT — UTC 2025-09-11T23:35:00Z — SNAP-20250911-64 (2.0.0)\r\n\r\nbody\r\n"
            "=== END SNAPSHOT — SNAP-20250911-64 — UTC 2025-09-11T23:35:00Z ===",
            "\r\n\r\n=== START SNAPSHOT — UTC 2025-09-20T16:46:46Z — SNAP-20250919-107 (5.6.5) ===\r\n"
            "body\r\n​=== END SNAPSHOT — SNAP-20250919-107 — UTC 2025-09-20T16:46:46Z ===",
            "\r\n=== START SNAPSHOT — UTC 2025-09-23T00:00:29Z — SNAP-20250922-125 (6.2.0) ===\r\n"
            "body\r\n=== END SNAPSHOT — SNAP-20250922-125 (6.2.0) — UTC 2025-09-23T00:00:29Z ===\r\n",
        ]
        data, index, pos = b"", {}, 0
        for i, b in enumerate(blocks):
            raw = b.encode()
            if i:
                sid = b.split(" — ")[2].split(" ")[0]
                lead = len(raw) - len(raw.lstrip(b"\r\n"))
                index[sid] = {"byte_start": pos + lead, "byte_end": pos + len(raw.rstrip(b"\r\n"))
                              + (2 if i == 3 else 0)}
            data += raw
            pos += len(raw)
        p.write_bytes(data)
        r = ledger_fsck.run(p, index)
        assert r["ok"] is True, r["problems"]
        assert r["snapshots"]["verified"] == 3 and r["snapshots"]["unindexed"] == 1
        assert r["snapshots"]["torn"] == 0
