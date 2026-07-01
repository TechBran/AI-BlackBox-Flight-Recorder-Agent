"""Unit tests for the M3 Device-model migration + registry primary/provider methods.

Covers: migration-safe from_dict (legacy json → defaults, unknown keys dropped),
default_provider validation, to_dict/from_dict round-trip, the single-primary-per-owner
invariant (set_primary_device atomic clear + load-time dedupe + owner isolation), and the
default_provider round-trip/validation. File-backed via a tmp devices.json.
"""
import json
import pytest

import Orchestrator.device_registry.registry as reg_mod
from Orchestrator.device_registry.models import Device, DeviceType, DeviceProtocol


LEGACY_DEVICE_JSON = {
    "id": "old-phone", "name": "Old Phone", "tailscale_ip": "100.1.1.1",
    "device_type": "android", "protocol": "adb", "owner": "Brandon",
    "description": "pre-M3 row", "adb_port": 5555, "vnc_port": 5900, "rdp_port": 3389,
    "status": "online", "last_seen": None, "metadata": {},
    # NOTE: no is_primary, no default_provider — this is the migration case.
}


# ── Device model migration ──

def test_from_dict_legacy_row_gets_defaults():
    d = Device.from_dict(LEGACY_DEVICE_JSON)
    assert d.is_primary is False
    assert d.default_provider is None


def test_from_dict_drops_unknown_keys():
    data = dict(LEGACY_DEVICE_JSON, some_future_field="x", another=42)
    d = Device.from_dict(data)   # must not raise on unknown keys
    assert d.id == "old-phone"


def test_from_dict_sanitizes_bad_default_provider():
    data = dict(LEGACY_DEVICE_JSON, default_provider="not-a-provider")
    assert Device.from_dict(data).default_provider is None
    data2 = dict(LEGACY_DEVICE_JSON, default_provider="GEMINI")   # case-normalized
    assert Device.from_dict(data2).default_provider == "gemini"


def test_to_dict_from_dict_roundtrip_preserves_new_fields():
    d = Device(id="p", name="P", tailscale_ip="100.2.2.2",
               device_type=DeviceType.ANDROID, protocol=DeviceProtocol.ADB,
               owner="Brandon", is_primary=True, default_provider="claude")
    d2 = Device.from_dict(d.to_dict())
    assert d2.is_primary is True
    assert d2.default_provider == "claude"


# ── Registry: primary + provider ──

@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A fresh file-backed DeviceRegistry over a tmp devices.json."""
    monkeypatch.setattr(reg_mod, "DEVICES_FILE", tmp_path / "devices.json")
    monkeypatch.setattr(reg_mod, "_registry", None)
    r = reg_mod.DeviceRegistry()
    for did in ("phone", "tablet"):
        r.add_device(Device(id=did, name=did.title(), tailscale_ip="100.0.0.1",
                            device_type=DeviceType.ANDROID, protocol=DeviceProtocol.ADB,
                            owner="Brandon"))
    r.add_device(Device(id="alice-phone", name="Alice", tailscale_ip="100.0.0.2",
                        device_type=DeviceType.ANDROID, protocol=DeviceProtocol.ADB,
                        owner="Alice"))
    return r


def test_set_primary_persists_and_is_readable(registry, tmp_path):
    assert registry.get_primary_device("Brandon") is None
    registry.set_primary_device("Brandon", "phone")
    assert registry.get_primary_device("Brandon").id == "phone"
    # Persisted atomically — a fresh registry reading the same file sees it.
    assert (tmp_path / "devices.json").exists()
    reloaded = reg_mod.DeviceRegistry()
    assert reloaded.get_primary_device("Brandon").id == "phone"


def test_set_primary_clears_the_old_primary(registry):
    registry.set_primary_device("Brandon", "phone")
    registry.set_primary_device("Brandon", "tablet")
    assert registry.get_primary_device("Brandon").id == "tablet"
    # Exactly one primary for Brandon.
    primaries = [d.id for d in registry.get_all_devices()
                 if d.owner == "Brandon" and d.is_primary]
    assert primaries == ["tablet"]


def test_set_primary_rejects_device_of_another_owner(registry):
    # Operator-isolation: Brandon cannot make Alice's device his primary.
    assert registry.set_primary_device("Brandon", "alice-phone") is None
    assert registry.get_primary_device("Brandon") is None


def test_set_primary_unknown_device_returns_none(registry):
    assert registry.set_primary_device("Brandon", "nope") is None


def test_dedupe_primaries_on_load(tmp_path, monkeypatch):
    # A hand-edited json with TWO Brandon primaries → dedupe to one on load.
    devices = {"devices": [
        dict(id="a", name="A", tailscale_ip="1", device_type="android", protocol="adb",
             owner="Brandon", is_primary=True, status="online"),
        dict(id="b", name="B", tailscale_ip="2", device_type="android", protocol="adb",
             owner="Brandon", is_primary=True, status="online"),
    ]}
    f = tmp_path / "devices.json"
    f.write_text(json.dumps(devices))
    monkeypatch.setattr(reg_mod, "DEVICES_FILE", f)
    monkeypatch.setattr(reg_mod, "_registry", None)
    r = reg_mod.DeviceRegistry()
    brandon_primaries = [d.id for d in r.get_all_devices() if d.is_primary]
    assert brandon_primaries == ["a"]   # first kept, second cleared


def test_load_skips_malformed_row_keeps_good_ones(tmp_path, monkeypatch):
    # M1: a future/unknown device_type ("tv") can't be parsed — that ONE row is skipped
    # and logged, but the good rows still load (one bad row can't strand the registry).
    devices = {"devices": [
        dict(id="good", name="Good", tailscale_ip="1", device_type="android",
             protocol="adb", owner="Brandon", status="online"),
        dict(id="future-tv", name="Future TV", tailscale_ip="2", device_type="tv",
             protocol="adb", owner="Brandon", status="online"),
        dict(id="good2", name="Good2", tailscale_ip="3", device_type="linux",
             protocol="vnc", owner="Alice", status="online"),
    ]}
    f = tmp_path / "devices.json"
    f.write_text(json.dumps(devices))
    monkeypatch.setattr(reg_mod, "DEVICES_FILE", f)
    monkeypatch.setattr(reg_mod, "_registry", None)
    r = reg_mod.DeviceRegistry()
    ids = {d.id for d in r.get_all_devices()}
    assert ids == {"good", "good2"}          # bad-enum row skipped, good rows loaded


def test_default_provider_roundtrip(registry):
    assert registry.get_default_provider("phone") is None
    registry.set_default_provider("phone", "gemma")
    assert registry.get_default_provider("phone") == "gemma"
    # Clearing.
    registry.set_default_provider("phone", None)
    assert registry.get_default_provider("phone") is None


def test_set_default_provider_invalid_raises(registry):
    with pytest.raises(ValueError):
        registry.set_default_provider("phone", "gpt-9")


def test_set_default_provider_unknown_device_returns_none(registry):
    assert registry.set_default_provider("nope", "gemini") is None
