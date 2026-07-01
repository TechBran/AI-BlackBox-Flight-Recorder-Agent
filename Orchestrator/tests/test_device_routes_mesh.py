"""TestClient tests for the M3 device-management API (device_routes.py).

Proves the tailnet join (GET /devices/mesh), operator assignment, primary toggle, and
default-provider setter — plus operator-isolation (can't touch another operator's device)
and that the pre-existing GET /devices/ CRUD still works (no regression). Hermetic: tailscale
is mocked (_get_tailnet_nodes), the registry is a tmp file, the operator roster is mocked.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import Orchestrator.routes.device_routes as dr
import Orchestrator.device_registry.registry as reg_mod
from Orchestrator.device_registry.models import Device, DeviceType, DeviceProtocol
from Orchestrator.local_provider.mesh import Node


NODES = [
    Node(hostname="brandon-fold6", dns_name="brandon-fold6.tailnet-abc.ts.net",
         ip="100.88.0.7", online=True, os="android"),           # registered → Brandon
    Node(hostname="work-tablet", dns_name="work-tablet.tailnet-abc.ts.net",
         ip="100.88.0.20", online=True, os="android"),          # online, UNCLAIMED
    Node(hostname="brandon-laptop", dns_name="brandon-laptop.tailnet-abc.ts.net",
         ip="100.88.0.9", online=False, os="windows"),          # offline, unclaimed
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(reg_mod, "DEVICES_FILE", tmp_path / "devices.json")
    monkeypatch.setattr(reg_mod, "_registry", None)
    r = reg_mod.get_registry()
    r.add_device(Device(id="brandon-fold6", name="Fold6", tailscale_ip="100.88.0.7",
                        device_type=DeviceType.ANDROID, protocol=DeviceProtocol.ADB,
                        owner="Brandon",
                        metadata={"tailscale_dns": "brandon-fold6.tailnet-abc.ts.net."}))
    monkeypatch.setattr(dr, "_get_tailnet_nodes", lambda: NODES)
    monkeypatch.setattr(dr, "_live_operators", lambda: ["Brandon", "Alice"])
    app = FastAPI()
    app.include_router(dr.router)
    return TestClient(app)


# ── GET /devices/mesh — the join ──

def test_mesh_lists_all_tailnet_nodes_with_ownership(client):
    rows = client.get("/devices/mesh").json()["devices"]
    by_id = {r["id"]: r for r in rows}
    assert by_id["brandon-fold6"]["owner"] == "Brandon"
    assert by_id["brandon-fold6"]["online"] is True
    # An online but un-registered tailnet node shows up, unclaimed.
    assert by_id["work-tablet"]["owner"] is None
    assert by_id["work-tablet"]["online"] is True
    # An offline node still appears with online=False.
    assert by_id["brandon-laptop"]["online"] is False
    # Contract shape for the UI wave.
    assert set(by_id["brandon-fold6"]) == {
        "id", "name", "tailnet", "type", "online", "owner", "is_primary", "default_provider"}


def test_mesh_operator_filter_hides_other_operators_devices(client):
    # Claim the UNCLAIMED work-tablet for Alice, then Brandon's filtered view must
    # not show it (owned by Alice), but must show his own fold6.
    client.post("/devices/work-tablet/operator", json={"operator": "Alice"})
    rows = client.get("/devices/mesh", params={"operator": "Brandon"}).json()["devices"]
    ids = {r["id"] for r in rows}
    assert "work-tablet" not in ids             # owned by Alice → hidden from Brandon
    assert "brandon-fold6" in ids               # Brandon's own → visible


# ── POST /devices/{id}/operator ──

def test_assign_operator_owned_by_other_is_409(client):
    # I1: brandon-fold6 is Brandon's — Alice cannot silently steal it. Must unassign first.
    client.post("/devices/brandon-fold6/primary", json={"operator": "Brandon"})
    resp = client.post("/devices/brandon-fold6/operator", json={"operator": "Alice"})
    assert resp.status_code == 409
    assert "already owned" in resp.json()["detail"].lower()
    # Ownership + primary untouched by the refused steal.
    got = client.get("/devices/brandon-fold6").json()
    assert got["owner"] == "Brandon"
    assert got["is_primary"] is True


def test_assign_operator_unclaimed_succeeds(client):
    # I1 positive path: an UNCLAIMED node can be claimed.
    resp = client.post("/devices/work-tablet/operator", json={"operator": "Alice"})
    assert resp.status_code == 200
    assert resp.json()["device"]["owner"] == "Alice"


def test_assign_operator_reaffirm_same_owner_succeeds(client):
    # I1: re-affirming the SAME owner is idempotent (not a cross-operator steal).
    resp = client.post("/devices/brandon-fold6/operator", json={"operator": "Brandon"})
    assert resp.status_code == 200
    assert resp.json()["device"]["owner"] == "Brandon"


def test_assign_operator_autoregisters_unclaimed_node(client):
    resp = client.post("/devices/work-tablet/operator", json={"operator": "Brandon"})
    assert resp.status_code == 200
    assert resp.json()["device"]["owner"] == "Brandon"
    # Now present in the registry CRUD.
    assert client.get("/devices/work-tablet").status_code == 200


def test_assign_rejects_system_and_unknown_operator(client):
    assert client.post("/devices/brandon-fold6/operator",
                       json={"operator": "system"}).status_code == 400
    assert client.post("/devices/brandon-fold6/operator",
                       json={"operator": "Nobody"}).status_code == 400


def test_assign_unknown_device_is_404(client):
    resp = client.post("/devices/ghost/operator", json={"operator": "Brandon"})
    assert resp.status_code == 404


# ── POST /devices/{id}/primary ──

def test_set_primary_ok(client):
    resp = client.post("/devices/brandon-fold6/primary", json={"operator": "Brandon"})
    assert resp.status_code == 200
    assert resp.json()["device"]["is_primary"] is True


def test_set_primary_isolation_rejects_other_owner(client):
    # Alice cannot make Brandon's device her primary.
    resp = client.post("/devices/brandon-fold6/primary", json={"operator": "Alice"})
    assert resp.status_code == 403


# ── POST /devices/{id}/default-provider ──

def test_set_default_provider_ok(client):
    resp = client.post("/devices/brandon-fold6/default-provider",
                       json={"provider": "gemma", "operator": "Brandon"})
    assert resp.status_code == 200
    assert resp.json()["device"]["default_provider"] == "gemma"


def test_set_default_provider_invalid_is_422(client):
    resp = client.post("/devices/brandon-fold6/default-provider",
                       json={"provider": "gpt-9", "operator": "Brandon"})
    assert resp.status_code == 422


def test_set_default_provider_isolation(client):
    # Asserting operator=Alice for Brandon's device → 403.
    resp = client.post("/devices/brandon-fold6/default-provider",
                       json={"provider": "gemma", "operator": "Alice"})
    assert resp.status_code == 403


def test_set_default_provider_requires_operator(client):
    # I1: operator context is ALWAYS required now (not conditional) → 400 if blank.
    resp = client.post("/devices/brandon-fold6/default-provider", json={"provider": "gemma"})
    assert resp.status_code == 400


def test_set_default_provider_clear(client):
    client.post("/devices/brandon-fold6/default-provider",
                json={"provider": "claude", "operator": "Brandon"})
    resp = client.post("/devices/brandon-fold6/default-provider",
                       json={"provider": None, "operator": "Brandon"})
    assert resp.status_code == 200
    assert resp.json()["device"]["default_provider"] is None


# ── I2: owner is unforgeable via the legacy create/update CRUD ──

def test_create_ignores_owner_from_body(client):
    # Planting owner=Alice at create must NOT work — the device is created UNCLAIMED;
    # ownership is set only via the authenticated /operator path.
    resp = client.post("/devices/", json={
        "id": "planted", "name": "Planted", "tailscale_ip": "100.9.9.9",
        "device_type": "android", "protocol": "adb", "owner": "Alice"})
    assert resp.status_code == 200
    assert resp.json()["device"]["owner"] in (None, "")
    # Genuinely unclaimed in the registry, too.
    assert client.get("/devices/planted").json()["owner"] in (None, "")


def test_update_cannot_set_owner(client):
    # PUT has no owner field — an owner in the body is ignored; Brandon keeps ownership.
    resp = client.put("/devices/brandon-fold6", json={"name": "Renamed", "owner": "Alice"})
    assert resp.status_code == 200
    assert client.get("/devices/brandon-fold6").json()["owner"] == "Brandon"


# ── regression: original CRUD still works ──

def test_existing_list_devices_still_works(client):
    resp = client.get("/devices/")
    assert resp.status_code == 200
    ids = {d["id"] for d in resp.json()["devices"]}
    assert "brandon-fold6" in ids
