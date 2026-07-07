"""M2.3 tests: registry.normalize() + POST /devices/normalize.

Proves the opt-in data-hygiene pass that fixes an already-poisoned registry:
  * PHANTOM OWNERS — a device owned by an operator NOT on this box's live roster has its
    owner cleared and is demoted as primary.
  * DUPLICATE IPs — pre-existing rows sharing a tailscale_ip collapse into ONE surviving
    row (keeper preferred by DNS-slug consistency), carrying forward a non-empty
    owner/is_primary/default_provider so a re-home is never silently lost.
  * the summary lists both, and the route returns it 200.

Hermetic + isolated: monkeypatch ``registry.DEVICES_FILE`` AND ``registry._LEGACY_DEVICES_FILE``
to tmp paths, reset the singleton, and mock the operator roster. The live devices.json is
NEVER touched.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import Orchestrator.routes.device_routes as dr
import Orchestrator.device_registry.registry as reg_mod
from Orchestrator.device_registry.models import Device, DeviceType, DeviceProtocol


def _mk(id, ip, owner="", is_primary=False, dns=None, default_provider=None,
        adb_port=5555, meta=None):
    """A Device whose metadata tailscale_dns defaults to a slug CONSISTENT with its id."""
    dns = f"{id}.tailnet-abc.ts.net." if dns is None else dns
    metadata = {"tailscale_dns": dns}
    if meta:
        metadata.update(meta)
    return Device(id=id, name=id.title(), tailscale_ip=ip,
                  device_type=DeviceType.ANDROID, protocol=DeviceProtocol.ADB,
                  owner=owner, is_primary=is_primary, default_provider=default_provider,
                  adb_port=adb_port, metadata=metadata)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(reg_mod, "DEVICES_FILE", tmp_path / "devices.json")
    monkeypatch.setattr(reg_mod, "_LEGACY_DEVICES_FILE", tmp_path / "legacy-devices.json")
    monkeypatch.setattr(reg_mod, "_registry", None)
    return reg_mod.DeviceRegistry()


# ── registry.normalize() ──

def test_normalize_clears_phantom_owner_and_collapses_dup(registry, tmp_path):
    # (a) A device owned by a phantom operator "Ghost" (not on the live roster), primary.
    registry.add_device(_mk("ghost-owned", "100.7.7.7", owner="Ghost", is_primary=True))
    # (b) A same-IP dup pair: the CONSISTENT row (id == its dns slug) should survive; the
    # STALE-dns row (slug "renamed-away" != id "samsung-old") is dropped.
    registry.add_device(_mk("brandons-z-fold6", "100.9.9.9", owner="Brandon"))
    registry.add_device(_mk("samsung-old", "100.9.9.9", owner="Brandon",
                            dns="renamed-away.tailnet-abc.ts.net."))

    summary = registry.normalize({"Brandon"})

    # Phantom owner cleared + demoted.
    assert "ghost-owned" in summary["cleared_owner"]
    g = registry.get_device("ghost-owned")
    assert g.owner == "" and g.is_primary is False

    # Dup collapsed: consistent row kept, stale row dropped.
    assert registry.get_device("brandons-z-fold6") is not None
    assert registry.get_device("samsung-old") is None
    kept_ids = {e["kept"] for e in summary["deduped"]}
    dropped_ids = {i for e in summary["deduped"] for i in e["dropped"]}
    assert kept_ids == {"brandons-z-fold6"}
    assert dropped_ids == {"samsung-old"}

    # Exactly two rows remain (ghost-owned unclaimed + the single Fold row).
    assert len(registry.get_all_devices()) == 2

    # Persisted — a fresh registry over the same file sees the normalized state.
    reloaded = reg_mod.DeviceRegistry()
    assert reloaded.get_device("samsung-old") is None
    assert reloaded.get_device("ghost-owned").owner == ""


def test_normalize_carries_forward_ownership_to_keeper(registry):
    # Keeper is UNCLAIMED but DNS-consistent; the dropped dup (the SAME physical device)
    # carries a real owner + primary + default_provider AND real connection state (a
    # non-default adb_port + a unique pairing metadata key) → ALL must survive on the
    # keeper. This is exactly the live-Fold case (samsung-sm-f956u-1's adb_port 40321).
    registry.add_device(_mk("keep-me", "100.3.3.3", owner=""))          # consistent, blank
    registry.add_device(_mk("drop-me", "100.3.3.3", owner="Brandon", is_primary=True,
                            default_provider="gemma", adb_port=40321,
                            meta={"adb_pairing_status": "paired"},
                            dns="stale-name.tailnet-abc.ts.net."))       # inconsistent
    summary = registry.normalize({"Brandon"})

    keeper = registry.get_device("keep-me")
    assert keeper is not None
    assert keeper.owner == "Brandon"          # carried forward
    assert keeper.is_primary is True          # carried forward
    assert keeper.default_provider == "gemma" # carried forward
    # I1: connection state from the dropped dup survives on the keeper.
    assert keeper.adb_port == 40321
    assert keeper.metadata["adb_pairing_status"] == "paired"
    # ...but the keeper's OWN metadata keys win (its dns is NOT overwritten by the stale one).
    assert keeper.metadata["tailscale_dns"] == "keep-me.tailnet-abc.ts.net."
    assert registry.get_device("drop-me") is None
    # An owned/primary dropped row is surfaced, not silently dropped.
    entry = next(e for e in summary["deduped"] if e["kept"] == "keep-me")
    assert entry["dropped_owned_or_primary"] == ["drop-me"]


def test_normalize_noop_when_clean(registry, monkeypatch):
    registry.add_device(_mk("a", "100.1.1.1", owner="Brandon"))
    registry.add_device(_mk("b", "100.2.2.2", owner="Brandon"))
    # N2: a clean normalize must perform NO write (guard the no-op write).
    calls = {"n": 0}
    orig_save = registry._save_to_file
    monkeypatch.setattr(registry, "_save_to_file",
                        lambda: (calls.__setitem__("n", calls["n"] + 1), orig_save())[1])
    summary = registry.normalize({"Brandon"})
    assert summary == {"cleared_owner": [], "deduped": []}
    assert calls["n"] == 0
    assert len(registry.get_all_devices()) == 2


# ── POST /devices/normalize route ──

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(reg_mod, "DEVICES_FILE", tmp_path / "devices.json")
    monkeypatch.setattr(reg_mod, "_LEGACY_DEVICES_FILE", tmp_path / "legacy-devices.json")
    monkeypatch.setattr(reg_mod, "_registry", None)
    r = reg_mod.get_registry()
    r.add_device(_mk("ghost-owned", "100.7.7.7", owner="Ghost", is_primary=True))
    r.add_device(_mk("brandons-z-fold6", "100.9.9.9", owner="Brandon"))
    r.add_device(_mk("samsung-old", "100.9.9.9", owner="Brandon",
                     dns="renamed-away.tailnet-abc.ts.net."))
    monkeypatch.setattr(dr, "_get_tailnet_nodes", lambda: [])
    monkeypatch.setattr(dr, "_live_operators", lambda: ["Brandon"])
    app = FastAPI()
    app.include_router(dr.router)
    return TestClient(app)


def test_normalize_route_returns_summary(client):
    resp = client.post("/devices/normalize")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "normalized"
    assert "ghost-owned" in body["cleared_owner"]
    dropped_ids = {i for e in body["deduped"] for i in e["dropped"]}
    assert "samsung-old" in dropped_ids
    # "normalize" was not captured as a device id (declared before GET /{device_id}).
    assert client.get("/devices/samsung-old").status_code == 404
