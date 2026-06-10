"""Regression test (Task 6.5): gateway-returning endpoints NEVER leak secrets.

Every endpoint that returns a gateway dict must pass it through
``gateway_manager.redact_gateway`` so the HTTP/AMI credentials are replaced with
``has_password`` / ``has_secret`` booleans and the raw secret (or its encrypted
``enc:`` form) never reaches a client.

This locks the redaction in place: if a future change returns a raw gateway from
any of these endpoints, these assertions fail.

Uses FastAPI ``TestClient`` (matching ``test_wizard_routes.py``). The handlers do
in-function imports, so we patch the SOURCE module (``gateway_manager``) — the
names the handlers import at call time resolve to the patched objects. No
network, no real Asterisk.
"""

from unittest.mock import patch, AsyncMock

import pytest
from fastapi.testclient import TestClient


GW_ID = "leak1234"

# Known-secret sentinels. If any of these strings appears in a response body the
# redaction is broken.
LEAK_PW = "LEAKPW"
LEAK_SEK = "LEAKSEK"


def _gateway_with_secrets():
    """A gateway carrying raw secrets in the nested http/ami blocks."""
    return {
        "id": GW_ID,
        "name": "Leaky TG200",
        "model": "TG200",
        "ip": "192.168.5.151",
        "enabled": True,
        "sip_port": 5060,
        "http_port": 80,
        "codec": "g722",
        "trunk_name": "tg-leak-tg200",
        "http": {"user": "admin", "password": LEAK_PW},
        "ami": {"port": 5038, "user": "blackbox", "secret": LEAK_SEK},
        "ports": [
            {"span": 2, "slot": 0, "phone_number": "+15551112222", "enabled": True},
        ],
    }


def _fake_status():
    return {
        "id": GW_ID,
        "name": "Leaky TG200",
        "ip": "192.168.5.151",
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


def _assert_leak_free(body_text: str):
    """No raw secret, no encrypted-secret prefix anywhere in the response."""
    assert LEAK_PW not in body_text, "raw http.password leaked"
    assert LEAK_SEK not in body_text, "raw ami.secret leaked"
    assert "enc:" not in body_text, "encrypted secret blob leaked"


def _assert_redacted_shape(gw: dict):
    """A returned gateway must expose has_* booleans, never the raw secret keys."""
    http = gw.get("http", {})
    ami = gw.get("ami", {})
    assert "password" not in http, "http.password must be stripped"
    assert "secret" not in ami, "ami.secret must be stripped"
    assert http.get("has_password") is True
    assert ami.get("has_secret") is True


# ---------------------------------------------------------------------------
# GET /asterisk/gateways  (list)
# ---------------------------------------------------------------------------
def test_list_gateways_never_leaks(client):
    with patch("Orchestrator.asterisk.gateway_manager.load_gateways",
               return_value=[_gateway_with_secrets()]), \
         patch("Orchestrator.asterisk.gateway_manager.check_gateway_status",
               new=AsyncMock(return_value=_fake_status())):
        resp = client.get("/asterisk/gateways")
    assert resp.status_code == 200
    _assert_leak_free(resp.text)
    gws = resp.json()["gateways"]
    assert gws
    _assert_redacted_shape(gws[0])


# ---------------------------------------------------------------------------
# GET /asterisk/gateways/{id}/status
# ---------------------------------------------------------------------------
def test_gateway_status_never_leaks(client):
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway",
               return_value=_gateway_with_secrets()), \
         patch("Orchestrator.asterisk.gateway_manager.check_gateway_status",
               new=AsyncMock(return_value=_fake_status())):
        resp = client.get(f"/asterisk/gateways/{GW_ID}/status")
    assert resp.status_code == 200
    _assert_leak_free(resp.text)
    _assert_redacted_shape(resp.json()["gateway"])


# ---------------------------------------------------------------------------
# POST /asterisk/gateways  (add)
# ---------------------------------------------------------------------------
def test_add_gateway_never_leaks(client):
    """The add response echoes the new gateway; it must be redacted."""
    def _fake_new_gateway(**kwargs):
        gw = _gateway_with_secrets()
        # _new_gateway builds the http block from http_password kwarg.
        gw["http"] = {"user": kwargs.get("http_user", "admin"),
                      "password": kwargs.get("http_password", LEAK_PW)}
        return gw

    with patch("Orchestrator.asterisk.gateway_manager._new_gateway",
               side_effect=_fake_new_gateway), \
         patch("Orchestrator.asterisk.gateway_manager.add_gateway",
               return_value=None), \
         patch("Orchestrator.asterisk.gateway_manager.merge_ports",
               return_value=None):
        resp = client.post("/asterisk/gateways", json={
            "name": "Leaky TG200",
            "ip": "192.168.5.151",
            "http_user": "admin",
            "http_password": LEAK_PW,
            "ami_user": "blackbox",
            "ami_secret": LEAK_SEK,
        })
    assert resp.status_code == 200
    _assert_leak_free(resp.text)
    _assert_redacted_shape(resp.json()["gateway"])


# ---------------------------------------------------------------------------
# PUT /asterisk/gateways/{id}  (update)
# ---------------------------------------------------------------------------
def test_update_gateway_never_leaks(client):
    """The update response echoes the merged gateway; it must be redacted."""
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway",
               return_value=_gateway_with_secrets()), \
         patch("Orchestrator.asterisk.gateway_manager.update_gateway",
               return_value=_gateway_with_secrets()):
        resp = client.put(f"/asterisk/gateways/{GW_ID}", json={
            "http_password": LEAK_PW,
            "ami_secret": LEAK_SEK,
        })
    assert resp.status_code == 200
    _assert_leak_free(resp.text)
    _assert_redacted_shape(resp.json()["gateway"])
