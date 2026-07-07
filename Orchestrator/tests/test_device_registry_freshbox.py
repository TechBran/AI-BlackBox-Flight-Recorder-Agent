"""M1.1 fresh-box tests: Tailscale discovery leaves devices UNCLAIMED.

Pins the product story that a newly-discovered tailnet peer is created with a BLANK
owner (never a hardcoded "Brandon") and is NOT primary — ownership is set only later via
POST /{id}/operator. Also guards that the existing-device UPDATE branch never rewrites a
pre-set non-empty owner (so a re-sync can't steal/reset ownership).

Hermetic + isolated: monkeypatch ``registry.DEVICES_FILE`` to a tmp file, reset the
singleton, and mock the ``tailscale status --json`` subprocess. The live
``Orchestrator/device_registry/devices.json`` is NEVER touched.
"""
import asyncio
import json

import pytest

import Orchestrator.device_registry.registry as reg_mod
from Orchestrator.device_registry.models import Device, DeviceType, DeviceProtocol


# One online Android peer whose DNS first-label → device id "work-tablet".
ONE_PEER_STATUS = {
    "Peer": {
        "nodekey:aaa": {
            "HostName": "work-tablet",
            "DNSName": "work-tablet.tailnet-abc.ts.net.",
            "Online": True,
            "TailscaleIPs": ["100.88.0.20"],
            "OS": "android",
        },
    },
}


class _FakeProc:
    """Minimal stand-in for an asyncio subprocess: returncode + communicate()."""

    def __init__(self, stdout_bytes: bytes):
        self._stdout = stdout_bytes
        self.returncode = 0

    async def communicate(self):
        return (self._stdout, b"")


def _mock_tailscale(monkeypatch, status_dict):
    """Patch asyncio.create_subprocess_exec so sync_from_tailscale reads status_dict."""
    payload = json.dumps(status_dict).encode()

    async def fake_exec(*args, **kwargs):
        return _FakeProc(payload)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


@pytest.fixture
def fresh_registry(tmp_path, monkeypatch):
    """A fresh file-backed DeviceRegistry over an EMPTY tmp devices.json."""
    monkeypatch.setattr(reg_mod, "DEVICES_FILE", tmp_path / "devices.json")
    # Point the legacy-migration seam at a NON-EXISTENT tmp path so the M2.1 legacy
    # load never reaches the live package devices.json during tests.
    monkeypatch.setattr(reg_mod, "_LEGACY_DEVICES_FILE", tmp_path / "legacy-devices.json")
    monkeypatch.setattr(reg_mod, "_registry", None)
    return reg_mod.DeviceRegistry()


# ── M1.1: discovery creates an UNCLAIMED device ──

def test_discovery_creates_unclaimed_device(fresh_registry, monkeypatch):
    _mock_tailscale(monkeypatch, ONE_PEER_STATUS)
    results = asyncio.run(fresh_registry.sync_from_tailscale())
    assert results.get("work-tablet") == "added"

    dev = fresh_registry.get_device("work-tablet")
    assert dev is not None
    # THE invariant: a freshly discovered peer is UNCLAIMED (no hardcoded "Brandon").
    assert dev.owner == ""
    assert dev.is_primary is False


# ── M1.1 guard: the existing-device UPDATE branch never rewrites a set owner ──

def test_resync_does_not_overwrite_existing_owner(fresh_registry, monkeypatch):
    # Seed a device whose id matches the discovered peer, already owned + primary.
    fresh_registry.add_device(Device(
        id="work-tablet", name="Work Tablet", tailscale_ip="100.88.0.20",
        device_type=DeviceType.ANDROID, protocol=DeviceProtocol.ADB,
        owner="Anna", is_primary=True,
    ))
    _mock_tailscale(monkeypatch, ONE_PEER_STATUS)
    results = asyncio.run(fresh_registry.sync_from_tailscale())
    assert results.get("work-tablet") == "updated"

    dev = fresh_registry.get_device("work-tablet")
    # Re-sync updates liveness/IP only — ownership + primary are preserved.
    assert dev.owner == "Anna"
    assert dev.is_primary is True


# ── M2.1: portable, self-creating path + legacy migration ──

def test_legacy_migration_loads_and_rewrites_to_new_path(tmp_path, monkeypatch):
    # Configured path is MISSING; a legacy file at a DIFFERENT path holds one device.
    new_path = tmp_path / "new" / "devices.json"          # doesn't exist yet
    legacy_path = tmp_path / "legacy" / "devices.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(json.dumps({"devices": [{
        "id": "legacy-phone", "name": "Legacy Phone", "tailscale_ip": "100.5.5.5",
        "device_type": "android", "protocol": "adb", "owner": "Brandon",
        "status": "online",
    }]}))
    monkeypatch.setattr(reg_mod, "DEVICES_FILE", new_path)
    monkeypatch.setattr(reg_mod, "_LEGACY_DEVICES_FILE", legacy_path)
    monkeypatch.setattr(reg_mod, "_registry", None)

    reg = reg_mod.DeviceRegistry()
    # The legacy device is loaded (reusing the per-row parse)...
    dev = reg.get_device("legacy-phone")
    assert dev is not None and dev.owner == "Brandon"
    # ...AND migrated (re-saved) to the NEW configured path.
    assert new_path.exists()
    saved = json.loads(new_path.read_text())
    assert [d["id"] for d in saved["devices"]] == ["legacy-phone"]


def test_makedirs_creates_missing_parent_dir_on_save(tmp_path, monkeypatch):
    # Configured path lives under a NON-EXISTENT nested dir; construction + a save must
    # create the parent (os.makedirs), not crash.
    nested = tmp_path / "sub" / "dir" / "devices.json"
    monkeypatch.setattr(reg_mod, "DEVICES_FILE", nested)
    monkeypatch.setattr(reg_mod, "_LEGACY_DEVICES_FILE", tmp_path / "legacy-devices.json")
    monkeypatch.setattr(reg_mod, "_registry", None)
    reg = reg_mod.DeviceRegistry()             # makedirs on init
    reg.add_device(Device(id="d1", name="D1", tailscale_ip="100.6.6.6",
                          device_type=DeviceType.ANDROID, protocol=DeviceProtocol.ADB,
                          owner=""))
    assert nested.exists()                     # save succeeded into the created dir


# ── M2.2: new-peer IP-collision handling on sync (prevent NEW dups) ──

# A peer at 100.0.0.9 whose DNS first-label → id "renamed-fold" (a DIFFERENT id than the
# existing device that already holds that IP → the "same physical device renamed" case).
COLLISION_STATUS = {
    "Peer": {
        "nodekey:zzz": {
            "HostName": "renamed-fold",
            "DNSName": "renamed-fold.tailnet-abc.ts.net.",
            "Online": True,
            "TailscaleIPs": ["100.0.0.9"],
            "OS": "android",
        },
    },
}


# ── M5.1: fresh-box claim (assign owner) persists across a reload ──

def test_freshbox_assign_owner_persists_across_reload(tmp_path, monkeypatch):
    # Start from an EMPTY (template) registry, discover/add an UNCLAIMED device, then CLAIM
    # it for an operator via the assign primitive the route uses
    # (registry.update_device(owner=..., is_primary=False)). Reload a BRAND-NEW
    # DeviceRegistry from the SAME file → the ownership must survive the "restart".
    devices_file = tmp_path / "devices.json"
    monkeypatch.setattr(reg_mod, "DEVICES_FILE", devices_file)
    monkeypatch.setattr(reg_mod, "_LEGACY_DEVICES_FILE", tmp_path / "legacy-devices.json")
    monkeypatch.setattr(reg_mod, "_registry", None)

    r = reg_mod.DeviceRegistry()
    assert r.get_all_devices() == []            # empty template state
    r.add_device(Device(id="front-desk", name="Front Desk", tailscale_ip="100.71.0.4",
                        device_type=DeviceType.ANDROID, protocol=DeviceProtocol.ADB,
                        owner=""))              # discovered UNCLAIMED
    # Claim it — mirrors POST /{id}/operator → registry.update_device(owner=..., is_primary=False).
    r.update_device("front-desk", owner="Casey", is_primary=False)

    # A fresh registry reading the same file sees the persisted claim (no restart lag).
    reloaded = reg_mod.DeviceRegistry()
    got = reloaded.get_device("front-desk")
    assert got is not None
    assert got.owner == "Casey"
    assert got.is_primary is False


def test_sync_new_peer_ip_collision_updates_existing_not_minted(fresh_registry, monkeypatch):
    # Existing device A already holds 100.0.0.9, owned + primary.
    fresh_registry.add_device(Device(
        id="device-a", name="Device A", tailscale_ip="100.0.0.9",
        device_type=DeviceType.ANDROID, protocol=DeviceProtocol.ADB,
        owner="Brandon", is_primary=True))
    _mock_tailscale(monkeypatch, COLLISION_STATUS)

    results = asyncio.run(fresh_registry.sync_from_tailscale())
    # No SECOND row minted for the renamed peer...
    assert fresh_registry.get_device("renamed-fold") is None
    assert len(fresh_registry.get_all_devices()) == 1
    # ...the existing row is updated + the collision surfaced in the results.
    assert results.get("device-a") == "ip_collision"
    a = fresh_registry.get_device("device-a")
    # Ownership PRESERVED (never dropped/overwritten); liveness refreshed in place.
    assert a.owner == "Brandon"
    assert a.is_primary is True
    assert a.metadata["tailscale_dns"] == "renamed-fold.tailnet-abc.ts.net."
