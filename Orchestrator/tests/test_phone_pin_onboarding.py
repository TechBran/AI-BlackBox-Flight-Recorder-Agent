"""Telephony onboarding sets the phone PIN (audit X03-security-backend-09).

The telephony wizard's Apply step (POST /asterisk/gateways/{id}/apply) is what
makes inbound calls reachable, so it generates a random PIN when none is set
and returns it exactly once. POST /phone/pin/generate does the same on its
own and never replaces an existing PIN; GET /phone/pin never reveals it.

Handlers are called directly with their dependencies patched: no TestClient
lifespan, no Asterisk, no AMI, and .env lives in tmp_path.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from Orchestrator import config
from Orchestrator.onboarding import secrets_writer
from Orchestrator.phone import pin_gate
from Orchestrator.routes import asterisk_routes, phone_routes

GW_ID = "abc12345"


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("PHONE_PIN_ENABLED=true\nPHONE_PIN_CODE=\n")
    monkeypatch.setattr(secrets_writer, "ENV_FILE", env_file)
    monkeypatch.setattr(config, "PHONE_PIN_ENABLED", True)
    monkeypatch.setattr(config, "PHONE_PIN_CODE", "")
    monkeypatch.setenv("PHONE_PIN_CODE", "")
    return env_file


def _apply():
    mgr = MagicMock()
    mgr.reconnect = AsyncMock()
    with patch("Orchestrator.asterisk.gateway_manager.get_gateway_decrypted",
               return_value={"id": GW_ID, "trunk_name": "tg-test"}), \
         patch("Orchestrator.asterisk.provisioner.apply_gateway",
               return_value={"written": "/x.conf", "reload": {"ok": True}}), \
         patch("Orchestrator.sms.get_manager", return_value=mgr):
        return asyncio.run(asterisk_routes.apply_gateway_endpoint(GW_ID))


def test_wizard_apply_generates_pin_once(env):
    first = _apply()
    assert first["applied"] is True
    pin = first["phone_pin"]["pin"]
    assert first["phone_pin"]["generated"] is True and pin_gate.PIN_RE.fullmatch(pin)
    assert f"PHONE_PIN_CODE={pin}\n" in env.read_text()
    assert pin_gate.gate_status() == "ready"
    second = _apply()
    assert second["phone_pin"]["generated"] is False
    assert "pin" not in second["phone_pin"]
    assert config.PHONE_PIN_CODE == pin


def test_wizard_apply_keeps_an_existing_pin(monkeypatch, env):
    monkeypatch.setattr(config, "PHONE_PIN_CODE", "73910482")
    body = _apply()
    assert body["phone_pin"] == {"enabled": True, "configured": True,
                                 "generated": False, "length": 8}
    assert "PHONE_PIN_CODE=\n" in env.read_text()


def test_wizard_apply_survives_unwritable_env(monkeypatch):
    def boom(_updates):
        raise OSError("read-only file system")

    monkeypatch.setattr(secrets_writer, "update_env", boom)
    body = _apply()
    assert body["applied"] is True
    assert body["phone_pin"]["configured"] is False and "error" in body["phone_pin"]
    assert pin_gate.gate_status() == "unconfigured"


def test_generate_endpoint_only_when_unconfigured(env):
    body = asyncio.run(phone_routes.phone_pin_generate())
    assert body["generated"] is True and pin_gate.PIN_RE.fullmatch(body["pin"])
    with pytest.raises(HTTPException) as exc:
        asyncio.run(phone_routes.phone_pin_generate())
    assert exc.value.status_code == 409
    assert config.PHONE_PIN_CODE == body["pin"]


def test_generate_endpoint_refuses_when_gate_disabled(monkeypatch):
    monkeypatch.setattr(config, "PHONE_PIN_ENABLED", False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(phone_routes.phone_pin_generate())
    assert exc.value.status_code == 409
    assert config.PHONE_PIN_CODE == ""


def test_status_endpoint_never_reveals_pin(monkeypatch):
    monkeypatch.setattr(config, "PHONE_PIN_CODE", "73910482")
    body = asyncio.run(phone_routes.phone_pin_status())
    assert body["configured"] is True and body["length"] == 8
    assert "73910482" not in repr(body)


def test_lockout_reset_endpoint_clears_locks(tmp_path, monkeypatch):
    """The operator's way out when guessers have tripped the global backstop."""
    monkeypatch.setattr(config, "PHONE_PIN_CODE", "73910482")
    monkeypatch.setattr(pin_gate, "LOCKOUT_PATH", tmp_path / "phone_pin_lockout.json")
    for _ in range(pin_gate.max_attempts()):
        pin_gate.record_failure("+15555550142")
    assert pin_gate.lockout_remaining("+15555550142") > 0
    body = asyncio.run(phone_routes.phone_pin_lockout_reset())
    assert body["cleared"] >= 1
    assert pin_gate.lockout_remaining("+15555550142") == 0
    assert "73910482" not in repr(body)
