"""M1.2 + M1.3 tests: registry.clear_owner() + POST /devices/{id}/unassign.

Proves the first-class UNASSIGN / re-home path that repairs the "assign 409s but there is
no unassign" dead-end:
  * registry.clear_owner blanks owner AND demotes primary, and persists across a reload.
  * the route requires a live operator (400 on blank/system/unknown), 404s an unknown id,
    and — the key story — after unassign a Brandon-owned device can be RE-HOMED to another
    live operator via POST /{id}/operator (no 409).

Hermetic + isolated: monkeypatch ``registry.DEVICES_FILE`` to a tmp file, reset the
singleton, mock tailscale + the operator roster. The live devices.json is NEVER touched.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import Orchestrator.routes.device_routes as dr
import Orchestrator.device_registry.registry as reg_mod
from Orchestrator.device_registry.models import Device, DeviceType, DeviceProtocol


# ── M1.2: registry.clear_owner primitive ──

@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(reg_mod, "DEVICES_FILE", tmp_path / "devices.json")
    monkeypatch.setattr(reg_mod, "_LEGACY_DEVICES_FILE", tmp_path / "legacy-devices.json")
    monkeypatch.setattr(reg_mod, "_registry", None)
    r = reg_mod.DeviceRegistry()
    r.add_device(Device(id="brandon-fold6", name="Fold6", tailscale_ip="100.88.0.7",
                        device_type=DeviceType.ANDROID, protocol=DeviceProtocol.ADB,
                        owner="Brandon", is_primary=True))
    return r


def test_clear_owner_blanks_owner_and_demotes_primary(registry, tmp_path):
    dev = registry.clear_owner("brandon-fold6")
    assert dev is not None
    assert dev.owner == ""
    assert dev.is_primary is False
    # Persisted — a fresh registry reading the same file sees the unclaim.
    assert (tmp_path / "devices.json").exists()
    reloaded = reg_mod.DeviceRegistry()
    got = reloaded.get_device("brandon-fold6")
    assert got.owner == ""
    assert got.is_primary is False


def test_clear_owner_unknown_device_returns_none(registry):
    assert registry.clear_owner("ghost") is None


def test_clear_owner_already_unclaimed_is_idempotent_noop(registry):
    # Unassigning an already-UNCLAIMED device is a harmless no-op: still returns the
    # device, still owner=="" / is_primary False (no error, no state churn).
    registry.clear_owner("brandon-fold6")          # first unassign
    dev = registry.clear_owner("brandon-fold6")     # second unassign — idempotent
    assert dev is not None
    assert dev.owner == ""
    assert dev.is_primary is False


# ── M1.3: POST /devices/{id}/unassign route ──

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(reg_mod, "DEVICES_FILE", tmp_path / "devices.json")
    monkeypatch.setattr(reg_mod, "_LEGACY_DEVICES_FILE", tmp_path / "legacy-devices.json")
    monkeypatch.setattr(reg_mod, "_registry", None)
    r = reg_mod.get_registry()
    r.add_device(Device(id="brandon-fold6", name="Fold6", tailscale_ip="100.88.0.7",
                        device_type=DeviceType.ANDROID, protocol=DeviceProtocol.ADB,
                        owner="Brandon", is_primary=True,
                        metadata={"tailscale_dns": "brandon-fold6.tailnet-abc.ts.net."}))
    # No live tailnet nodes needed; roster mocked so validation is deterministic.
    monkeypatch.setattr(dr, "_get_tailnet_nodes", lambda: [])
    monkeypatch.setattr(dr, "_live_operators", lambda: ["Brandon", "Alice"])
    app = FastAPI()
    app.include_router(dr.router)
    return TestClient(app)


def test_unassign_then_rehome_to_another_operator(client):
    # (a) Unassign a Brandon-owned device (provenance = current owner) → 200, owner "".
    resp = client.post("/devices/brandon-fold6/unassign", json={"operator": "Brandon"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unassigned"
    assert body["device"]["owner"] in (None, "")
    assert body["device"]["is_primary"] is False
    # (b) Re-home now works (no 409) — the whole point of the unassign path.
    resp2 = client.post("/devices/brandon-fold6/operator", json={"operator": "Alice"})
    assert resp2.status_code == 200
    assert resp2.json()["device"]["owner"] == "Alice"


def test_unassign_unknown_device_is_404(client):
    resp = client.post("/devices/ghost/unassign", json={"operator": "Brandon"})
    assert resp.status_code == 404


def test_unassign_already_unclaimed_is_idempotent_200(client):
    # Re-home Brandon's device away, then unassign the now-UNCLAIMED device again:
    # still 200, still owner "" (idempotent no-op through the route).
    client.post("/devices/brandon-fold6/unassign", json={"operator": "Brandon"})
    resp = client.post("/devices/brandon-fold6/unassign", json={"operator": "Alice"})
    assert resp.status_code == 200
    assert resp.json()["device"]["owner"] in (None, "")
    assert resp.json()["device"]["is_primary"] is False


def test_unassign_requires_live_operator(client):
    # Blank operator → 400.
    assert client.post("/devices/brandon-fold6/unassign",
                       json={"operator": ""}).status_code == 400
    # 'system' is not a live operator → 400.
    assert client.post("/devices/brandon-fold6/unassign",
                       json={"operator": "system"}).status_code == 400
    # Unknown operator → 400.
    assert client.post("/devices/brandon-fold6/unassign",
                       json={"operator": "Nobody"}).status_code == 400
