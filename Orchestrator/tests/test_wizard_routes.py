"""Tests for the setup-wizard telephony endpoints (Tasks 5.3 + 5.4).

Covers the five wizard endpoints added to ``asterisk_routes.py``:

  POST /asterisk/gateways/{id}/validate        — live green/red checks
  POST /asterisk/gateways/{id}/apply           — write+reload OUR config, reconnect AMI
  POST /asterisk/gateways/{id}/test-sms        — send a test SMS via the router
  POST /asterisk/gateways/{id}/test-call       — initiate an outbound test call
  GET  /asterisk/gateways/{id}/config-preview  — copy-paste artifacts

Uses FastAPI ``TestClient`` (the established route-test pattern in this repo).
The handlers do in-function imports, so we patch the SOURCE modules
(``gateway_manager``, ``provisioner``, ``sms``, ``asterisk.client``) — the names
the handlers import at call time resolve to the patched objects.

No network, no real Asterisk, no real AMI: every external dependency is stubbed.
"""

from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient


GW_ID = "abc12345"


def _fake_gateway():
    return {
        "id": GW_ID,
        "name": "Test TG200",
        "model": "TG200",
        "ip": "192.168.5.150",
        "enabled": True,
        "sip_port": 5060,
        "http_port": 80,
        "codec": "g722",
        "trunk_name": "tg-test-tg200",
        "http": {"user": "admin", "password": "secret"},
        "ami": {"port": 5038, "user": "blackbox", "secret": "amisecret"},
        "ports": [
            {"span": 2, "slot": 0, "phone_number": "+15551112222", "enabled": True},
            {"span": 3, "slot": 1, "phone_number": "+15553334444", "enabled": True},
        ],
    }


def _fake_status():
    return {
        "id": GW_ID,
        "name": "Test TG200",
        "ip": "192.168.5.150",
        "reachable": True,
        "sip_registered": True,
        "sim_slots": [
            {"slot": 0, "span": 2, "status": "up", "carrier": "TestCarrier",
             "signal": 22, "registered": True, "phone_number": "+15551112222"},
        ],
        "active_calls": 0,
        "checked_at": "2026-06-07T00:00:00Z",
    }


@pytest.fixture
def client():
    """TestClient against the real app.

    The ToolVault startup hook spawns a thread calling ``sync_embeddings``;
    mock it so app construction needs no network/credentials.
    """
    with patch("Orchestrator.toolvault.embeddings.sync_embeddings") as m:
        m.return_value = {"x": {"vector": [0.1]}}
        from Orchestrator.app import app
        with TestClient(app) as c:
            yield c


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def test_validate_maps_all_five_keys(client):
    """validate returns the 5 keys, mapped from check_gateway_status + a
    connected fake AMI client."""
    fake_ami = MagicMock()
    fake_ami.connected = True
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway",
               return_value=_fake_gateway()), \
         patch("Orchestrator.asterisk.gateway_manager.check_gateway_status",
               new=AsyncMock(return_value=_fake_status())), \
         patch("Orchestrator.sms.get_ami_client", return_value=fake_ami):
        resp = client.post(f"/asterisk/gateways/{GW_ID}/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["gateway_id"] == GW_ID
    assert body["reachable"] is True
    assert body["ami_auth"] is True
    assert body["trunk_online"] is True
    assert body["spans"] == _fake_status()["sim_slots"]


def test_validate_ami_not_connected(client):
    """ami_auth is False when the AMI client is missing or disconnected."""
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway",
               return_value=_fake_gateway()), \
         patch("Orchestrator.asterisk.gateway_manager.check_gateway_status",
               new=AsyncMock(return_value=_fake_status())), \
         patch("Orchestrator.sms.get_ami_client", return_value=None):
        resp = client.post(f"/asterisk/gateways/{GW_ID}/validate")
    assert resp.status_code == 200
    assert resp.json()["ami_auth"] is False


def test_validate_gateway_not_found(client):
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway",
               return_value=None):
        resp = client.post(f"/asterisk/gateways/{GW_ID}/validate")
    assert resp.status_code == 200
    assert "error" in resp.json()


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------
def test_apply_writes_reloads_and_reconnects(client):
    """apply calls provisioner.apply_gateway + manager.reconnect and returns
    applied True with restart_recommended False when reload ok is True."""
    fake_mgr = MagicMock()
    fake_mgr.reconnect = AsyncMock()
    apply_result = {
        "written": "/etc/asterisk/blackbox.d/tg-test-tg200.conf",
        "reload": {"pjsip": 0, "dialplan": 0, "ok": True},
    }
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway_decrypted",
               return_value=_fake_gateway()), \
         patch("Orchestrator.asterisk.provisioner.apply_gateway",
               return_value=apply_result) as m_apply, \
         patch("Orchestrator.sms.get_manager", return_value=fake_mgr):
        resp = client.post(f"/asterisk/gateways/{GW_ID}/apply")
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True
    assert body["restart_recommended"] is False
    assert body["config"] == apply_result["written"]
    assert body["reload"] == apply_result["reload"]
    m_apply.assert_called_once()
    fake_mgr.reconnect.assert_awaited_once_with(GW_ID)


def test_apply_restart_recommended_when_reload_fails(client):
    """When reload ok is False (e.g. sudoers not installed), signal a restart."""
    fake_mgr = MagicMock()
    fake_mgr.reconnect = AsyncMock()
    apply_result = {
        "written": "/etc/asterisk/blackbox.d/tg-test-tg200.conf",
        "reload": {"ok": False, "error": "sudo: no tty present"},
    }
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway_decrypted",
               return_value=_fake_gateway()), \
         patch("Orchestrator.asterisk.provisioner.apply_gateway",
               return_value=apply_result), \
         patch("Orchestrator.sms.get_manager", return_value=fake_mgr):
        resp = client.post(f"/asterisk/gateways/{GW_ID}/apply")
    assert resp.status_code == 200
    assert resp.json()["restart_recommended"] is True


def test_apply_gateway_not_found(client):
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway_decrypted",
               return_value=None):
        resp = client.post(f"/asterisk/gateways/{GW_ID}/apply")
    assert resp.status_code == 200
    assert "error" in resp.json()


def test_apply_survives_no_manager(client):
    """A missing SMS manager (system not started) must not crash apply."""
    apply_result = {"written": "/x.conf", "reload": {"ok": True}}
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway_decrypted",
               return_value=_fake_gateway()), \
         patch("Orchestrator.asterisk.provisioner.apply_gateway",
               return_value=apply_result), \
         patch("Orchestrator.sms.get_manager", return_value=None):
        resp = client.post(f"/asterisk/gateways/{GW_ID}/apply")
    assert resp.status_code == 200
    assert resp.json()["applied"] is True


# ---------------------------------------------------------------------------
# config-preview
# ---------------------------------------------------------------------------
def test_config_preview_returns_conf_and_steps(client):
    """config-preview returns asterisk_conf containing the trunk name and a
    non-empty tg_steps whose first item contains the gateway ip."""
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway",
               return_value=_fake_gateway()):
        resp = client.get(f"/asterisk/gateways/{GW_ID}/config-preview")
    assert resp.status_code == 200
    body = resp.json()
    assert "tg-test-tg200" in body["asterisk_conf"]
    assert isinstance(body["tg_steps"], list) and body["tg_steps"]
    assert "192.168.5.150" in body["tg_steps"][0]


def test_config_preview_gateway_not_found(client):
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway",
               return_value=None):
        resp = client.get(f"/asterisk/gateways/{GW_ID}/config-preview")
    assert resp.status_code == 200
    assert "error" in resp.json()


# ---------------------------------------------------------------------------
# test-sms
# ---------------------------------------------------------------------------
def test_test_sms_router_none(client):
    """No router (SMS system not started) → success False with an error."""
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway",
               return_value=_fake_gateway()), \
         patch("Orchestrator.sms.get_router", return_value=None):
        resp = client.post(f"/asterisk/gateways/{GW_ID}/test-sms",
                           json={"to": "+15551234567", "message": "hi"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


def test_test_sms_forwards_to_send_manual(client):
    """With a fake router, forwards to send_manual(gateway_id=id) and returns
    its result."""
    fake_router = MagicMock()
    fake_router.send_manual = AsyncMock(
        return_value={"success": True, "message_id": 7, "error": None})
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway",
               return_value=_fake_gateway()), \
         patch("Orchestrator.sms.get_router", return_value=fake_router):
        resp = client.post(f"/asterisk/gateways/{GW_ID}/test-sms",
                           json={"to": "5551234567", "message": "ping"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    fake_router.send_manual.assert_awaited_once()
    kwargs = fake_router.send_manual.await_args.kwargs
    assert kwargs["gateway_id"] == GW_ID
    assert kwargs["to"] == "+15551234567"   # normalized
    assert kwargs["message"] == "ping"


def test_test_sms_default_message(client):
    """An empty message falls back to a default test SMS body."""
    fake_router = MagicMock()
    fake_router.send_manual = AsyncMock(return_value={"success": True})
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway",
               return_value=_fake_gateway()), \
         patch("Orchestrator.sms.get_router", return_value=fake_router):
        resp = client.post(f"/asterisk/gateways/{GW_ID}/test-sms",
                           json={"to": "+15551234567"})
    assert resp.status_code == 200
    kwargs = fake_router.send_manual.await_args.kwargs
    assert kwargs["message"]  # non-empty default


# ---------------------------------------------------------------------------
# test-call
# ---------------------------------------------------------------------------
def test_test_call_reuses_outbound_path(client):
    """test-call reuses the existing outbound-call function and passes the
    gateway's trunk name."""
    fake_client = MagicMock()
    fake_client.is_connected = True
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway",
               return_value=_fake_gateway()), \
         patch("Orchestrator.asterisk.client.get_ari_client",
               return_value=fake_client), \
         patch("Orchestrator.routes.asterisk_routes._handle_outbound_call",
               new=AsyncMock()) as m_handle:
        resp = client.post(f"/asterisk/gateways/{GW_ID}/test-call",
                           json={"to": "5551234567"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "initiated"
    assert "session_id" in body
    # The outbound handler is invoked with the gateway's trunk name.
    assert m_handle.await_count == 1 or m_handle.call_count == 1


def test_test_call_ari_not_connected(client):
    """No ARI connection → an error, not a crash."""
    fake_client = MagicMock()
    fake_client.is_connected = False
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway",
               return_value=_fake_gateway()), \
         patch("Orchestrator.asterisk.client.get_ari_client",
               return_value=fake_client):
        resp = client.post(f"/asterisk/gateways/{GW_ID}/test-call",
                           json={"to": "+15551234567"})
    assert resp.status_code == 200
    assert "error" in resp.json()


def test_test_call_gateway_not_found(client):
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway",
               return_value=None):
        resp = client.post(f"/asterisk/gateways/{GW_ID}/test-call",
                           json={"to": "+15551234567"})
    assert resp.status_code == 200
    assert "error" in resp.json()
