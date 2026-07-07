"""M1.4 — the operator roster is LIVE (no restart lag).

Regression guard for the verified bug: ``POST /operator/add`` and
``DELETE /operator/{name}`` used to do ``global USERS_LIST; USERS_LIST = [...]``,
which rebinds only *admin_routes'* alias. ``device_routes._live_operators`` re-reads
``config.USERS_LIST`` (unchanged) and ``notification_routes`` captured it at import —
so a freshly-added operator showed up in ``GET /operators`` yet a device-assign 400'd
("not a live operator") until the service restarted.

The fix mutates the SHARED ``config.USERS_LIST`` object in place (``[:] = …``); because
every importer holds that one object by reference, the update is seen everywhere with
NO restart. ``remove_operator`` additionally re-points a dangling
``USERS_DEFAULT``/``CURRENT_OPERATOR`` so nothing names a phantom operator.

Isolation (MANDATORY — these handlers WRITE config.ini):
  * ``monkeypatch.chdir(tmp_path)`` so the ``config.ini`` they write (a relative
    ``Path("config.ini")``) lands in a throwaway dir; the REAL config.ini is asserted
    byte-identical on teardown as a safety net.
  * snapshot+restore ``CFG[users]`` and ``config.USERS_LIST/USERS_DEFAULT/CURRENT_OPERATOR``.
  * rebind ``admin_routes.USERS_LIST`` to the shared config object at setup so the
    same-object baseline holds regardless of test order (a buggy run rebinds it away).
  * the device registry is a tmp file with one UNCLAIMED (claimable) device; the real
    ``_live_operators`` is deliberately NOT mocked (that is the code under test).
"""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import Orchestrator.config as cfg
import Orchestrator.routes.admin_routes as ar
import Orchestrator.routes.device_routes as dr
import Orchestrator.device_registry.registry as reg_mod
from Orchestrator.device_registry.models import Device, DeviceType, DeviceProtocol


@pytest.fixture
def client(tmp_path, monkeypatch):
    real_config = Path.cwd() / "config.ini"
    real_bytes = real_config.read_bytes()

    # --- snapshot every piece of shared state the handlers touch ---
    snap_list = list(cfg.USERS_LIST)
    snap_default = cfg.USERS_DEFAULT
    snap_current = cfg.CURRENT_OPERATOR
    snap_cfg_list = cfg.CFG.get("users", "list", fallback="")
    snap_cfg_default = cfg.CFG.get("users", "default", fallback="")

    # --- isolate the config.ini WRITE into a throwaway dir ---
    monkeypatch.chdir(tmp_path)

    # --- baseline roster == ["Brandon"], globals + CFG aligned, same-object baseline ---
    monkeypatch.setattr(ar, "USERS_LIST", cfg.USERS_LIST)  # undo any prior rebind
    cfg.USERS_LIST[:] = ["Brandon"]
    cfg.USERS_DEFAULT = "Brandon"
    cfg.CURRENT_OPERATOR = "Brandon"
    if not cfg.CFG.has_section("users"):
        cfg.CFG.add_section("users")
    cfg.CFG.set("users", "list", "Brandon")
    cfg.CFG.set("users", "default", "Brandon")

    # --- tmp device registry with one UNCLAIMED device (any live operator may claim) ---
    monkeypatch.setattr(reg_mod, "DEVICES_FILE", tmp_path / "devices.json")
    monkeypatch.setattr(reg_mod, "_registry", None)
    registry = reg_mod.get_registry()
    registry.add_device(Device(
        id="work-tablet", name="Work Tablet", tailscale_ip="100.88.0.20",
        device_type=DeviceType.ANDROID, protocol=DeviceProtocol.ADB, owner=""))

    # --- one app carrying the REAL operator handlers + the device router, over HTTP ---
    app = FastAPI()
    app.include_router(dr.router)
    app.add_api_route("/operator/add", ar.add_operator, methods=["POST"])
    app.add_api_route("/operators", ar.list_operators, methods=["GET"])
    app.add_api_route("/operator/{name}", ar.remove_operator, methods=["DELETE"])

    yield TestClient(app)

    # --- restore in-memory state ---
    cfg.USERS_LIST[:] = snap_list
    cfg.USERS_DEFAULT = snap_default
    cfg.CURRENT_OPERATOR = snap_current
    if cfg.CFG.has_section("users"):
        cfg.CFG.set("users", "list", snap_cfg_list)
        cfg.CFG.set("users", "default", snap_cfg_default)
    # safety net: the handlers must never have touched the real config.ini
    assert real_config.read_bytes() == real_bytes, "TEST POLLUTED THE REAL config.ini"


def test_new_operator_is_live_without_restart(client):
    # sanity: baseline roster is Brandon-only, Anna is not yet live.
    assert dr._live_operators() == ["Brandon"]

    r = client.post("/operator/add", json={"name": "Anna"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "success"

    # (a) device_routes' live-operator view sees Anna with NO restart.
    assert "Anna" in dr._live_operators()

    # (b) device-assign validation now ACCEPTS Anna (400 -> 200 was the bug).
    resp = client.post("/devices/work-tablet/operator", json={"operator": "Anna"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["device"]["owner"] == "Anna"

    # (c) GET /operators includes Anna.
    assert "Anna" in client.get("/operators").json()["operators"]

    # same-object invariant: add_operator must NOT rebind admin_routes' alias away.
    assert ar.USERS_LIST is cfg.USERS_LIST


def test_remove_operator_repoints_dangling_default(client, monkeypatch):
    # roster becomes [Brandon, Anna] with Anna as the box default + current operator,
    # persisted to CFG and captured by admin_routes' by-value USERS_DEFAULT alias (as
    # at load) — the exact stale-alias condition that produced the phantom default.
    assert client.post("/operator/add", json={"name": "Anna"}).status_code == 200
    cfg.USERS_DEFAULT = "Anna"
    cfg.CURRENT_OPERATOR = "Anna"
    cfg.CFG.set("users", "default", "Anna")
    monkeypatch.setattr(ar, "USERS_DEFAULT", "Anna")

    r = client.delete("/operator/Anna")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "removed"

    # Anna is gone from the live roster with no restart.
    assert "Anna" not in cfg.USERS_LIST
    assert "Anna" not in dr._live_operators()

    # the dangling default/current operator is re-pointed to a survivor (no phantom).
    assert cfg.USERS_DEFAULT == "Brandon"
    assert cfg.CURRENT_OPERATOR == "Brandon"

    # GET /operators serves the LIVE default — the phantom "Anna" before this fix.
    body = client.get("/operators").json()
    assert body["default"] == "Brandon"
    assert "Anna" not in body["operators"]

    # persisted for the restart path: CFG's [users] default now names the survivor.
    assert cfg.CFG.get("users", "default") == "Brandon"
