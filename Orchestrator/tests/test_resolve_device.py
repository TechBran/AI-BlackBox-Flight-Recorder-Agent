"""Unit tests for mesh.resolve_device — the M3 origin-aware routing invariant.

Precedence proven here (research §5.5 decision 3):
  explicit target → origin (must belong to operator, else RAISE) → operator's PRIMARY
  (registry × reachability) → legacy attested device → raise no_device.

The firm invariant: an origin naming a device that is NOT this operator's is an ERROR
(origin_mismatch), NEVER a silent retarget. Fully hermetic — tailscale status is injected
(status_json), the device_registry is a fake, and the local attestation registry is either
empty (reachable_devices monkeypatched to []) or a tmp-file fixture.
"""
import json
import pytest

import Orchestrator.local_provider.registry as r
import Orchestrator.local_provider.mesh as m
from Orchestrator.local_provider.mesh import DeviceResolutionError


# brandon-fold6 ONLINE (android), brandon-laptop OFFLINE (windows), linux Self.
SAMPLE_STATUS = json.dumps({
    "Self": {"HostName": "ai-black-box-fc", "DNSName": "ai-black-box-fc.tailnet-abc.ts.net.",
             "Online": True, "TailscaleIPs": ["100.74.17.54"], "OS": "linux"},
    "Peer": {
        "nodekey:aaa": {"HostName": "brandon-fold6",
                        "DNSName": "brandon-fold6.tailnet-abc.ts.net.", "Online": True,
                        "TailscaleIPs": ["100.88.0.7"], "OS": "android"},
        "nodekey:bbb": {"HostName": "brandon-laptop",
                        "DNSName": "brandon-laptop.tailnet-abc.ts.net.", "Online": False,
                        "TailscaleIPs": ["100.88.0.9"], "OS": "windows"},
    },
})


class FakeDevice:
    def __init__(self, id, owner, is_primary=False, tailscale_ip="", dns="", hostname=""):
        self.id = id
        self.owner = owner
        self.is_primary = is_primary
        self.tailscale_ip = tailscale_ip
        self.name = id
        self.metadata = {"tailscale_dns": dns, "tailscale_hostname": hostname}


class FakeRegistry:
    def __init__(self, devices):
        self._d = list(devices)

    def get_devices_by_owner(self, owner):
        return [d for d in self._d if d.owner.lower() == owner.lower()]

    def get_primary_device(self, owner):
        return next((d for d in self._d
                     if d.owner.lower() == owner.lower() and d.is_primary), None)

    def get_all_devices(self):
        return list(self._d)


@pytest.fixture
def no_attestations(monkeypatch):
    """Isolate ownership to the fake device_registry (empty local attestation path)."""
    monkeypatch.setattr(m, "reachable_devices", lambda *a, **k: [])


@pytest.fixture
def fresh_local_registry(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "STORE_FILE", tmp_path / "local_devices.json")
    monkeypatch.setattr(r, "_registry", None)
    return r.get_local_registry()


# ── 1. explicit target → ANY tailnet node ──

def test_explicit_target_resolves_any_online_node():
    node = m.resolve_device("Brandon", target_device_id="brandon-fold6",
                            status_json=SAMPLE_STATUS, registry=FakeRegistry([]))
    assert node.hostname == "brandon-fold6"


def test_explicit_target_invalid_raises_invalid_target():
    with pytest.raises(DeviceResolutionError) as ei:
        m.resolve_device("Brandon", target_device_id="ghost-tablet",
                         status_json=SAMPLE_STATUS, registry=FakeRegistry([]))
    assert ei.value.kind == "invalid_target"
    assert ei.value.detail["requested"] == "ghost-tablet"


def test_offline_node_is_not_a_valid_target():
    # brandon-laptop is present but OFFLINE → not reachable → invalid_target.
    with pytest.raises(DeviceResolutionError) as ei:
        m.resolve_device("Brandon", target_device_id="brandon-laptop",
                         status_json=SAMPLE_STATUS, registry=FakeRegistry([]))
    assert ei.value.kind == "invalid_target"


# ── 2. origin → that device, but only if it belongs to the operator ──

def test_origin_defaults_to_that_device_when_owned(no_attestations):
    reg = FakeRegistry([FakeDevice(id="brandon-fold6", owner="Brandon")])
    node = m.resolve_device("Brandon", origin_device_id="brandon-fold6",
                            status_json=SAMPLE_STATUS, registry=reg)
    assert node.hostname == "brandon-fold6"


def test_origin_owned_via_attestation(fresh_local_registry):
    # Ownership can come from the local attestation registry too (fresh-box path).
    fresh_local_registry.attest(operator="Brandon", device_id="fold6",
                                model_slug="gemma-4-e4b", version="1", sha256="x",
                                delegate="gpu", autonomy_mode="yolo",
                                tailnet_name="brandon-fold6")
    node = m.resolve_device("Brandon", origin_device_id="brandon-fold6",
                            status_json=SAMPLE_STATUS, registry=FakeRegistry([]))
    assert node.hostname == "brandon-fold6"


def test_origin_mismatch_raises_never_silent_retarget(no_attestations):
    # The origin device is reachable but registered to NOBODY (not this operator) →
    # ERROR, not a fallback to some other device. THE invariant.
    with pytest.raises(DeviceResolutionError) as ei:
        m.resolve_device("Brandon", origin_device_id="brandon-fold6",
                         status_json=SAMPLE_STATUS, registry=FakeRegistry([]))
    assert ei.value.kind == "origin_mismatch"
    assert ei.value.detail["origin"] == "brandon-fold6"


def test_origin_owned_by_other_operator_raises(no_attestations):
    # brandon-fold6 belongs to Alice; Brandon claims it as origin → origin_mismatch,
    # NOT a silent retarget to any Brandon device.
    reg = FakeRegistry([FakeDevice(id="brandon-fold6", owner="Alice")])
    with pytest.raises(DeviceResolutionError) as ei:
        m.resolve_device("Brandon", origin_device_id="brandon-fold6",
                         status_json=SAMPLE_STATUS, registry=reg)
    assert ei.value.kind == "origin_mismatch"


def test_origin_not_reachable_raises_no_device(no_attestations):
    with pytest.raises(DeviceResolutionError) as ei:
        m.resolve_device("Brandon", origin_device_id="ghost",
                         status_json=SAMPLE_STATUS, registry=FakeRegistry([]))
    assert ei.value.kind == "no_device"


# ── 3. primary fallback (non-device origin) ──

def test_primary_fallback_when_no_target_or_origin(no_attestations):
    reg = FakeRegistry([FakeDevice(id="brandon-fold6", owner="Brandon", is_primary=True)])
    node = m.resolve_device("Brandon", status_json=SAMPLE_STATUS, registry=reg)
    assert node.hostname == "brandon-fold6"


def test_primary_designated_but_offline_raises_no_primary_device(no_attestations):
    # Primary points at the OFFLINE laptop → not reachable → no_primary_device.
    reg = FakeRegistry([FakeDevice(id="brandon-laptop", owner="Brandon", is_primary=True)])
    with pytest.raises(DeviceResolutionError) as ei:
        m.resolve_device("Brandon", status_json=SAMPLE_STATUS, registry=reg)
    assert ei.value.kind == "no_primary_device"
    assert ei.value.detail["primary"] == "brandon-laptop"


# ── 4. nothing resolvable ──

def test_none_path_raises_no_device(no_attestations, monkeypatch):
    monkeypatch.setattr(m, "resolve_origin", lambda *a, **k: None)  # no legacy device
    with pytest.raises(DeviceResolutionError) as ei:
        m.resolve_device("Brandon", status_json=SAMPLE_STATUS, registry=FakeRegistry([]))
    assert ei.value.kind == "no_device"


def test_legacy_attested_device_used_when_no_primary(fresh_local_registry):
    # No explicit primary, but the operator has a single attested reachable device →
    # resolve_origin fallback returns it (back-compat).
    fresh_local_registry.attest(operator="Brandon", device_id="fold6",
                                model_slug="gemma-4-e4b", version="1", sha256="x",
                                delegate="gpu", autonomy_mode="yolo",
                                tailnet_name="brandon-fold6")
    node = m.resolve_device("Brandon", status_json=SAMPLE_STATUS, registry=FakeRegistry([]))
    assert node.hostname == "brandon-fold6"


def test_resolve_device_never_returns_none():
    # Contract: always a Node or a raise — never None (unlike resolve_origin).
    try:
        node = m.resolve_device("Nobody", status_json=SAMPLE_STATUS, registry=FakeRegistry([]))
        assert node is not None
    except DeviceResolutionError:
        pass  # raising is the correct "nothing" behavior


# ── 5. M3 single-spawn + M4 blank-operator guard ──

def test_resolve_device_shells_tailscale_once(monkeypatch, fresh_local_registry):
    # M3: a single live resolve (status_json=None) must spawn `tailscale status` at most
    # ONCE, even though online-nodes + origin-ownership both consult the status.
    calls = {"n": 0}

    def counting_status():
        calls["n"] += 1
        return SAMPLE_STATUS

    monkeypatch.setattr(m, "_run_tailscale_status", counting_status)
    # Ownership resolves via the attestation registry (forces the reachable_devices
    # branch of _origin_belongs_to_operator, which historically re-shelled).
    fresh_local_registry.attest(operator="Brandon", device_id="fold6",
                                model_slug="gemma-4-e4b", version="1", sha256="x",
                                delegate="gpu", autonomy_mode="yolo",
                                tailnet_name="brandon-fold6")
    node = m.resolve_device("Brandon", origin_device_id="brandon-fold6",
                            registry=FakeRegistry([]))  # status_json=None → live path
    assert node.hostname == "brandon-fold6"
    assert calls["n"] == 1


def test_origin_belongs_to_operator_rejects_blank_operator():
    # M4: a blank operator must never be treated as an owner — otherwise it would match
    # an UNCLAIMED (owner=="") device_registry row and silently pass.
    node = m.Node(hostname="brandon-fold6", dns_name="brandon-fold6.tailnet-abc.ts.net",
                  ip="100.88.0.7", online=True, os="android")
    reg = FakeRegistry([FakeDevice(id="brandon-fold6", owner="")])  # UNCLAIMED
    assert m._origin_belongs_to_operator("", node, reg, SAMPLE_STATUS) is False
