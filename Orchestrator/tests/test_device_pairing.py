"""Box-side pairing secrets for the phone's control listener (audit X02/X04, rank 29).

The phone's RemoteControlServer (:8765) now requires ``Authorization: Bearer <secret>``
on every route except GET /healthz. The phone mints the secret and registers it here
(POST /devices/pairing); every box-side caller of the listener reads it back by the
target node's tailnet IP. These tests pin the store's security contract:

  * the secret lands in a 0600 file, written atomically, never in a list response;
  * a lookup is by the EXACT tailnet IP the box is about to call, so one device's
    secret can never be sent to another node;
  * a node with no recorded secret fails with an actionable "open the app once"
    message instead of a silent unauthenticated call.

Hermetic: the store path is a tmp file, no tailscale, no network.
"""
import json
import os
import stat

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from Orchestrator.device_registry import pairing
from Orchestrator.local_provider.mesh import Node

SECRET = "a1" * 32           # 64 hex chars = 256 bits (the phone's format)
OTHER_SECRET = "b2" * 32
PHONE_IP = "100.88.0.7"
NODE = Node(hostname="test-phone", dns_name="test-phone.tailnet-abc.ts.net",
            ip=PHONE_IP, online=True, os="android")


@pytest.fixture(autouse=True)
def store_path(tmp_path, monkeypatch):
    path = tmp_path / "credentials" / "device_pairing.json"
    monkeypatch.setattr(pairing, "PAIRING_PATH", path)
    return path


def _register(**overrides):
    kwargs = dict(device_id="android-id-1", tailnet_ip=PHONE_IP, secret=SECRET,
                  display_name="Test Phone", device_kind="android")
    kwargs.update(overrides)
    return pairing.register(**kwargs)


# ── storage contract ──────────────────────────────────────────────────────────────

def test_register_persists_secret_in_a_0600_file(store_path):
    _register()
    assert store_path.exists()
    mode = stat.S_IMODE(os.stat(store_path).st_mode)
    assert mode == 0o600, oct(mode)
    data = json.loads(store_path.read_text())
    assert data["devices"][PHONE_IP]["secret"] == SECRET


def test_register_returns_public_row_without_the_secret():
    row = _register()
    assert row["tailnet_ip"] == PHONE_IP
    assert row["device_id"] == "android-id-1"
    assert "secret" not in row
    assert SECRET not in json.dumps(row)


def test_public_rows_never_carry_secrets():
    _register()
    _register(device_id="android-id-2", tailnet_ip="100.88.0.8", secret=OTHER_SECRET)
    rows = pairing.public_rows()
    assert {r["tailnet_ip"] for r in rows} == {PHONE_IP, "100.88.0.8"}
    blob = json.dumps(rows)
    assert SECRET not in blob and OTHER_SECRET not in blob


def test_reregistration_replaces_the_secret():
    _register()
    _register(secret=OTHER_SECRET)
    assert pairing.secret_for_ip(PHONE_IP) == OTHER_SECRET


def test_device_that_moved_ip_drops_its_stale_row():
    _register()
    _register(tailnet_ip="100.88.0.99")
    assert pairing.secret_for_ip(PHONE_IP) is None
    assert pairing.secret_for_ip("100.88.0.99") == SECRET


def test_no_temp_files_left_behind(store_path):
    _register()
    _register(secret=OTHER_SECRET)
    leftovers = [p.name for p in store_path.parent.iterdir() if p.name != store_path.name]
    assert leftovers == []


def test_corrupt_store_reads_as_empty_and_is_quarantined(store_path):
    store_path.parent.mkdir(parents=True)
    store_path.write_text("{not json")
    assert pairing.secret_for_ip(PHONE_IP) is None
    _register()  # a corrupt file must never block a fresh pairing
    assert pairing.secret_for_ip(PHONE_IP) == SECRET
    assert any(p.name.startswith("device_pairing.json.corrupt-")
               for p in store_path.parent.iterdir())


def test_fresh_box_has_no_pairings_and_no_file(store_path):
    assert pairing.public_rows() == []
    assert pairing.secret_for_ip(PHONE_IP) is None
    assert not store_path.exists()


def test_forget_removes_the_row():
    _register()
    assert pairing.forget(PHONE_IP) is True
    assert pairing.secret_for_ip(PHONE_IP) is None
    assert pairing.forget(PHONE_IP) is False


# ── input validation (hostile registrations) ──────────────────────────────────────

@pytest.mark.parametrize("ip", ["192.168.1.5", "8.8.8.8", "127.0.0.1", "100.128.0.1",
                                "not-an-ip", "", "100.88.0.7:8765"])
def test_register_refuses_non_tailnet_addresses(ip):
    with pytest.raises(ValueError):
        _register(tailnet_ip=ip)


@pytest.mark.parametrize("secret", ["", "short", "x" * 42, "a" * 300,
                                    "a1" * 31 + "zz!", "a1" * 32 + "\n"])
def test_register_refuses_weak_or_malformed_secrets(secret):
    with pytest.raises(ValueError):
        _register(secret=secret)


def test_validation_errors_never_echo_the_secret():
    bad = "Z" * 30 + "!" * 20
    with pytest.raises(ValueError) as exc:
        _register(secret=bad)
    assert bad not in str(exc.value)


def test_register_accepts_tailscale_ipv6():
    _register(tailnet_ip="fd7a:115c:a1e0::1234")
    assert pairing.secret_for_ip("fd7a:115c:a1e0::1234") == SECRET


# ── lookup by the node the box is about to call ───────────────────────────────────

def test_auth_headers_for_paired_node():
    _register()
    assert pairing.auth_headers_for_node(NODE) == {"Authorization": f"Bearer {SECRET}"}


def test_lookup_is_by_exact_ip_never_by_name():
    # A node that merely SHARES a hostname with the paired phone must not get its secret.
    _register()
    imposter = Node(hostname="test-phone", dns_name="test-phone.other.ts.net",
                    ip="100.88.0.66", online=True, os="android")
    with pytest.raises(pairing.DeviceNotPaired):
        pairing.auth_headers_for_node(imposter)


def test_unpaired_node_raises_actionable_error():
    with pytest.raises(pairing.DeviceNotPaired) as exc:
        pairing.auth_headers_for_node(NODE)
    msg = exc.value.message
    assert "test-phone.tailnet-abc.ts.net" in msg
    assert "Open the BlackBox app" in msg
    assert exc.value.kind == pairing.ERROR_KIND_NOT_PAIRED == "not_paired"


def test_is_pairing_rejection_recognises_401_from_both_http_clients():
    class _AioErr(Exception):
        status = 401

    class _Resp:
        status_code = 401

    class _HttpxErr(Exception):
        response = _Resp()

    class _Other(Exception):
        status = 403

    assert pairing.is_pairing_rejection(_AioErr())
    assert pairing.is_pairing_rejection(_HttpxErr())
    assert not pairing.is_pairing_rejection(_Other())
    assert not pairing.is_pairing_rejection(ValueError("x"))


# ── REST surface ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    import Orchestrator.routes.device_routes as dr
    import Orchestrator.device_registry.registry as reg_mod
    monkeypatch.setattr(reg_mod, "DEVICES_FILE", tmp_path / "devices.json")
    monkeypatch.setattr(reg_mod, "_LEGACY_DEVICES_FILE", tmp_path / "legacy-devices.json")
    monkeypatch.setattr(reg_mod, "_registry", None)
    monkeypatch.setattr(dr, "_get_tailnet_nodes", lambda: [NODE])
    monkeypatch.setattr(dr, "_live_operators", lambda: ["Alice"])
    app = FastAPI()
    app.include_router(dr.router)
    return TestClient(app)


def _body(**overrides):
    body = {"device_id": "android-id-1", "tailnet_ip": PHONE_IP, "secret": SECRET,
            "display_name": "Test Phone", "device_kind": "android"}
    body.update(overrides)
    return body


def test_post_pairing_registers_and_never_echoes_the_secret(client):
    r = client.post("/devices/pairing", json=_body())
    assert r.status_code == 200, r.text
    assert SECRET not in r.text
    assert r.json()["device"]["tailnet_ip"] == PHONE_IP
    assert pairing.secret_for_ip(PHONE_IP) == SECRET


def test_post_pairing_rejects_a_lan_address(client):
    r = client.post("/devices/pairing", json=_body(tailnet_ip="192.168.1.5"))
    assert r.status_code == 422
    assert pairing.secret_for_ip("192.168.1.5") is None


def test_post_pairing_rejects_a_weak_secret(client):
    r = client.post("/devices/pairing", json=_body(secret="1234"))
    assert r.status_code == 422


def test_list_endpoints_never_return_secrets(client):
    client.post("/devices/pairing", json=_body())
    for path in ("/devices/pairing", "/devices/", "/devices/mesh"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)
        assert SECRET not in r.text, path


def test_get_pairing_lists_metadata(client):
    client.post("/devices/pairing", json=_body())
    rows = client.get("/devices/pairing").json()["devices"]
    assert rows[0]["tailnet_ip"] == PHONE_IP
    assert rows[0]["display_name"] == "Test Phone"


def test_delete_pairing_forgets_the_device(client):
    client.post("/devices/pairing", json=_body())
    r = client.delete(f"/devices/pairing/{PHONE_IP}")
    assert r.status_code == 200
    assert pairing.secret_for_ip(PHONE_IP) is None
    assert client.delete(f"/devices/pairing/{PHONE_IP}").status_code == 404
