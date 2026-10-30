"""Full-disk encryption status (spec §3.6 "Encryption"): whether the root
filesystem, and the one holding the health stores, sit on dm-crypt / LUKS.
Read from a fake /proc/self/mounts and /sys tree: no sudo, no lsblk, never
raises, null when it cannot tell. The signed status and wellness_sources
carry it; the unsigned status does not."""
from __future__ import annotations

import os
import types

import pytest

from Orchestrator.wellness import fde


class FakeBox:
    """A /sys/dev/block + /sys/devices tree, a mounts file and a stat()."""

    def __init__(self, tmp_path):
        self.sys = tmp_path / "sys"
        (self.sys / "dev" / "block").mkdir(parents=True)
        self.mounts = tmp_path / "mounts"
        self.mounts.write_text("")
        self.devs = {}          # path -> st_dev
        self.rdevs = {}         # device node path -> st_rdev

    def block(self, name, majmin, *, uuid=None, slaves=(), parent=None, partition=False):
        base = (self.sys / "devices" / "virtual" / "block") if parent is None else parent
        d = base / name
        d.mkdir(parents=True)
        if uuid is not None:
            (d / "dm").mkdir()
            (d / "dm" / "uuid").write_text(uuid + "\n")
        if partition:
            (d / "partition").write_text("1\n")
        (d / "slaves").mkdir()
        for s in slaves:
            os.symlink(s, d / "slaves" / s.name)
        os.symlink(d, self.sys / "dev" / "block" / majmin)
        return d

    def mount(self, source, point, fstype="ext4"):
        with self.mounts.open("a") as f:
            f.write(f"{source} {point} {fstype} rw,relatime 0 0\n")

    def stat(self, path):
        path = str(path)
        if path in self.rdevs:
            return types.SimpleNamespace(st_dev=0, st_rdev=self.rdevs[path])
        for p in sorted(self.devs, key=len, reverse=True):
            if path == p or path.startswith(p.rstrip("/") + "/"):
                return types.SimpleNamespace(st_dev=self.devs[p], st_rdev=0)
        raise FileNotFoundError(path)

    def status(self, store_dir="/srv/bbx/data/wellness"):
        return fde.status(store_dir, proc_mounts=str(self.mounts), sys_root=str(self.sys),
                          stat=self.stat)


@pytest.fixture
def box(tmp_path):
    return FakeBox(tmp_path)


def test_lvm_on_luks_is_encrypted(box):
    crypt = box.block("dm-0", "253:0", uuid="CRYPT-LUKS2-0a1b2c3d-dm_crypt-0")
    box.block("dm-1", "253:1", uuid="LVM-Q0lqVg0", slaves=[crypt])
    box.devs["/"] = os.makedev(253, 1)
    assert box.status() == {"root_encrypted": True, "method": "luks", "store_encrypted": True}


def test_plain_dm_crypt(box):
    box.block("dm-0", "253:0", uuid="CRYPT-PLAIN-cryptroot")
    box.devs["/"] = os.makedev(253, 0)
    assert box.status()["method"] == "dm-crypt" and box.status()["root_encrypted"] is True


def test_a_plain_partition_is_not_encrypted(box):
    disk = box.block("nvme0n1", "259:0", parent=box.sys / "devices" / "pci0000:00")
    box.block("nvme0n1p2", "259:2", parent=disk, partition=True)
    box.devs["/"] = os.makedev(259, 2)
    assert box.status() == {"root_encrypted": False, "method": None, "store_encrypted": False}


def test_a_partition_of_an_encrypted_mapping(box):
    crypt = box.block("dm-0", "253:0", uuid="CRYPT-LUKS1-ffff-root")
    box.block("dm-0p1", "253:1", parent=crypt, partition=True)
    box.devs["/"] = os.makedev(253, 1)
    assert box.status()["root_encrypted"] is True


def test_an_unencrypted_mapping_is_not_encrypted(box):
    disk = box.block("sda", "8:0", parent=box.sys / "devices" / "pci0000:00")
    part = box.block("sda3", "8:3", parent=disk, partition=True)
    box.block("dm-0", "253:0", uuid="LVM-plainvg", slaves=[part])
    box.devs["/"] = os.makedev(253, 0)
    assert box.status()["root_encrypted"] is False


def test_btrfs_reads_its_device_from_mounts(box):
    """btrfs reports an anonymous device number; the mounts line names the
    real one (spaces in mount points come escaped as \\040)."""
    box.block("dm-0", "253:0", uuid="CRYPT-LUKS2-abcd-luks-abcd")
    box.mount("/dev/mapper/luks-abcd", "/")
    box.mount("/dev/mapper/luks-abcd", "/srv/my\\040data", "btrfs")
    box.devs["/"] = os.makedev(0, 31)
    box.rdevs["/dev/mapper/luks-abcd"] = os.makedev(253, 0)
    assert box.status("/srv/my data/wellness") == {"root_encrypted": True, "method": "luks",
                                                   "store_encrypted": True}


def test_the_store_can_sit_on_another_filesystem(box):
    box.block("dm-0", "253:0", uuid="CRYPT-LUKS2-abcd-root")
    disk = box.block("sdb", "8:16", parent=box.sys / "devices" / "pci0000:00")
    box.block("sdb1", "8:17", parent=disk, partition=True)
    box.devs["/"] = os.makedev(253, 0)
    box.devs["/srv/bbx"] = os.makedev(8, 17)
    assert box.status() == {"root_encrypted": True, "method": "luks", "store_encrypted": False}


@pytest.mark.parametrize("source,fstype", [("overlay", "overlay"), ("rpool/ROOT/ubuntu", "zfs"),
                                           ("tmpfs", "tmpfs"), ("nas:/export", "nfs4")])
def test_what_it_cannot_tell_is_null(box, source, fstype):
    box.mount(source, "/", fstype)
    box.devs["/"] = os.makedev(0, 44)
    assert box.status() == {"root_encrypted": None, "method": None, "store_encrypted": None}


def test_it_never_raises(box, tmp_path):
    def boom(path):
        raise PermissionError(path)

    out = fde.status("/x", proc_mounts=str(tmp_path / "none"), sys_root=str(tmp_path / "nosys"),
                     stat=boom)
    assert out == {"root_encrypted": None, "method": None, "store_encrypted": None}
    box.devs["/"] = os.makedev(253, 9)               # no sysfs entry for it
    assert box.status()["root_encrypted"] is None


def test_a_slave_loop_does_not_hang(box):
    a = box.block("dm-0", "253:0", uuid="LVM-a")
    b = box.block("dm-1", "253:1", uuid="LVM-b", slaves=[a])
    os.symlink(b, a / "slaves" / "dm-1")
    box.devs["/"] = os.makedev(253, 1)
    assert box.status()["root_encrypted"] is False


def test_this_box_answers_in_shape():
    out = fde.status()
    assert set(out) == {"root_encrypted", "method", "store_encrypted"}
    assert out["root_encrypted"] in (True, False, None)
    assert out["method"] in ("luks", "dm-crypt", None)


# ------------------------------------------------------------------ where it shows

def test_the_signed_status_and_sources_carry_it(tmp_path, monkeypatch):
    from Orchestrator.routes import wellness_routes
    from Orchestrator.tests.wellness_support import OPERATOR, enrolled_fixture
    from Orchestrator.wellness import api
    _rec, _st, data = enrolled_fixture(tmp_path, monkeypatch)
    known = {"root_encrypted": True, "method": "luks", "store_encrypted": True}
    monkeypatch.setattr(fde, "status", lambda *a, **k: dict(known))
    assert wellness_routes.full_status(OPERATOR, now_ms=data["now_ms"])["fde"] == known
    assert "fde" not in wellness_routes._minimal_status(OPERATOR)
    env = api.sources(OPERATOR, now_ms=data["now_ms"])
    assert env["ok"] and env["fde"] == known


# ------------------------------------------------------------------ local days in the status view

def test_the_status_wear_counts_local_nights(tmp_path, monkeypatch):
    """At 21:00 local (UTC-4) the UTC date is already tomorrow; the last 7
    nights are still counted from the local day."""
    from Orchestrator.routes import wellness_routes
    from Orchestrator.tests.wellness_support import OPERATOR, enrolled_fixture, fixtures
    fx = fixtures()
    enrolled_fixture(tmp_path, monkeypatch)
    # Local nights -2..-8: worn on -2, -3, -5, -7, -8 (-4 and -6 were not).
    full = wellness_routes.full_status(OPERATOR, now_ms=fx.ms(-2, 21))
    assert full["wear"]["nights_worn_7"] == 5


def test_disclosure_times_use_the_offset_of_their_day(tmp_path, monkeypatch):
    from Orchestrator.routes import wellness_routes
    from Orchestrator.tests.wellness_support import enrolled_fixture, fixtures
    fx = fixtures()
    _rec, st, _data = enrolled_fixture(tmp_path, monkeypatch, name="dst_spring_21")
    z = fx.zone("dst_spring_21")
    st.record_delivery("w-20260301-aaaa", "t1", "android", "local", None, "wellness_summary",
                       {}, ["sleep"], {}, ts=z.ms(-17, 9))
    st.record_delivery("w-20260317-bbbb", "t2", "android", "local", None, "wellness_summary",
                       {}, ["sleep"], {}, ts=z.ms(-1, 9))
    times = sorted(r["time"] for r in wellness_routes.disclosure_log(st))
    assert times == ["2026-03-01T09:00-05:00", "2026-03-17T09:00-04:00"]


def test_the_privacy_policy_points_at_the_status():
    from Orchestrator.routes.wellness_routes import POLICY_PATH
    text = POLICY_PATH.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("- **At rest,**"))
    assert "Settings, Health" in line and "encrypted" in line
    assert "ask whoever set up your box" not in text
