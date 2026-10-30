"""The Devices mesh rows' owner-only health fields (spec §3.1, rule W17):
health_source / health_state are filled ONLY when the request's ?operator=
equals that row's owner; every other row reads false / null, so the badge
never tells one operator about another's enrollment."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import Orchestrator.device_registry.registry as reg_mod
import Orchestrator.routes.device_routes as dr
from Orchestrator.device_registry.models import Device, DeviceProtocol, DeviceType
from Orchestrator.local_provider.mesh import Node
from Orchestrator.tests.wellness_support import sandbox, write_record

OWNER = "wellness-fixture"
OTHER = "other-human"
NODES = [Node(hostname="fixture-phone", dns_name="fixture-phone.tailnet.ts.net",
              ip="100.100.7.7", online=True, os="android"),
         Node(hostname="other-phone", dns_name="other-phone.tailnet.ts.net",
              ip="100.100.7.8", online=True, os="android")]


@pytest.fixture
def client(tmp_path, monkeypatch):
    sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(reg_mod, "DEVICES_FILE", tmp_path / "devices.json")
    monkeypatch.setattr(reg_mod, "_LEGACY_DEVICES_FILE", tmp_path / "legacy.json")
    monkeypatch.setattr(reg_mod, "_registry", None)
    r = reg_mod.get_registry()
    for did, ip, owner in (("fixture-phone", "100.100.7.7", OWNER),
                           ("other-phone", "100.100.7.8", OTHER)):
        r.add_device(Device(id=did, name=did, tailscale_ip=ip, device_type=DeviceType.ANDROID,
                            protocol=DeviceProtocol.ADB, owner=owner))
    monkeypatch.setattr(dr, "_get_tailnet_nodes", lambda: NODES)
    monkeypatch.setattr(dr, "_live_operators", lambda: [OWNER, OTHER])
    write_record(OWNER, device_id="fixture-phone", last_push_at="2099-01-01T00:00:00Z")
    write_record(OTHER, op_key="0" * 16, enrollment_id="en_00000002", device_id="other-phone",
                 last_push_at="2099-01-01T00:00:00Z")
    app = FastAPI()
    app.include_router(dr.router)
    return TestClient(app)


def _rows(client, operator=None):
    params = {"operator": operator} if operator else None
    return {r["id"]: r for r in client.get("/devices/mesh", params=params).json()["devices"]}


def test_owner_sees_their_own_badge(client):
    rows = _rows(client, OWNER)
    assert rows["fixture-phone"]["health_source"] is True
    assert rows["fixture-phone"]["health_state"] == "active"


def test_nobody_else_learns_anything(client):
    rows = _rows(client)                                  # no operator: no badges at all
    assert all(r["health_source"] is False and r["health_state"] is None for r in rows.values())
    mine = _rows(client, OWNER)
    assert "other-phone" not in mine                      # the filter hides it anyway
    theirs = _rows(client, OTHER)
    assert theirs["other-phone"]["health_source"] is True
    assert "fixture-phone" not in theirs


def test_awaiting_phone_is_a_state_not_a_source(client):
    write_record(OWNER, device_id="fixture-phone", state="awaiting_phone")
    row = _rows(client, OWNER)["fixture-phone"]
    assert row["health_source"] is False and row["health_state"] == "awaiting_phone"


def test_unenrolled_rows_are_false_and_null(client):
    from Orchestrator.wellness import enroll
    with enroll.editing() as data:
        data.pop(OWNER)
    row = _rows(client, OWNER)["fixture-phone"]
    assert row["health_source"] is False and row["health_state"] is None


def test_a_broken_store_never_breaks_the_devices_view(client, monkeypatch):
    from Orchestrator.wellness import enroll
    monkeypatch.setattr(enroll, "get_enrollment", lambda op: 1 / 0)
    row = _rows(client, OWNER)["fixture-phone"]
    assert row["health_source"] is False
