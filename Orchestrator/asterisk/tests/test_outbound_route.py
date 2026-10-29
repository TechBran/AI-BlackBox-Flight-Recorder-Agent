"""POST /asterisk/call dials the trunk of a gateway in the registry.

Audit O11-telephony-asterisk-cellular-07: the route used
`call_request.trunk or TG200_TRUNK_NAME` (env default 'tg200') and caller id
TG200_PHONE_NUMBER. Gateways added through the Portal, Android or discovery
get trunk 'tg-<name>' and the provisioner writes [tg-<name>]; no tool caller
passes a trunk, and .env does not set TG200_TRUNK_NAME. On a fresh box every
tool-initiated call originated PJSIP/+1...@tg200, ARI refused it and the
session was marked FAILED. It worked on the dev box only because its legacy
gateways.json record is hand-named 'tg200'.

The route now takes the trunk and caller id from the gateway registry (an
explicit trunk or gateway_id first), keeps the two env values as overrides,
and refuses with a clear 409 when nothing can place the call. The ARI client,
the audio subprocess and the call itself are fakes; nothing is dialled.
"""
import asyncio

import pytest

from Orchestrator.asterisk import audio_ipc
from Orchestrator.asterisk import client as ari_mod
from Orchestrator.asterisk import config as acfg
from Orchestrator.asterisk import gateway_manager as gm
from Orchestrator.routes import asterisk_routes as ar


class _ARI:
    is_connected = True

    def __init__(self, endpoints=()):
        self.endpoints = set(endpoints)

    async def get_endpoint_detail(self, tech, resource):
        return {"resource": resource, "state": "online"} if resource in self.endpoints else None


class _Running:
    is_running = True


@pytest.fixture
def box(tmp_path, monkeypatch):
    """An empty gateway registry, a connected ARI client and a live audio
    subprocess; the call itself is recorded, never placed."""
    monkeypatch.setattr(gm, "GATEWAYS_FILE", str(tmp_path / "gateways.json"))
    monkeypatch.setattr(acfg, "TG200_TRUNK_NAME", "")
    monkeypatch.setattr(acfg, "TG200_PHONE_NUMBER", "")
    monkeypatch.setattr(ar, "PHONE_SESSIONS", {})
    ari = _ARI()
    monkeypatch.setattr(ari_mod, "get_ari_client", lambda: ari)
    monkeypatch.setattr(audio_ipc, "get_ipc_client", lambda: _Running())
    placed = []

    async def record(session, to_number, trunk):
        placed.append({"to": to_number, "trunk": trunk, "caller_id": session.caller_id})

    monkeypatch.setattr(ar, "_handle_outbound_call", record)
    return {"ari": ari, "placed": placed}


def _gateway(name, numbers=(), enabled=True, ports_enabled=True):
    gw = gm._new_gateway(name=name, ip="192.0.2.10", phone_numbers=list(numbers))
    gw["enabled"] = enabled
    for port in gw["ports"]:
        port["enabled"] = ports_enabled
    gm.add_gateway(gw)
    return gw


def _call(**body):
    async def scenario():
        result = await ar.asterisk_outbound_call(ar.AsteriskCallRequest(to="5550100123", **body))
        await asyncio.sleep(0)  # let the recorded call task run
        return result

    return asyncio.run(scenario())


def _body(result):
    if hasattr(result, "status_code"):
        import json
        return result.status_code, json.loads(result.body)
    return 200, result


def test_a_tool_call_on_a_fresh_box_dials_the_provisioned_gateway(box):
    gw = _gateway("Office", numbers=["+15555550111"])
    status, body = _body(_call())
    assert status == 200 and body["status"] == "initiated"
    assert box["placed"] == [{"to": "+15550100123", "trunk": gw["trunk_name"],
                              "caller_id": "+15555550111"}]
    assert gw["trunk_name"] == "tg-office"
    assert body["from"] == "+15555550111"


def test_gateway_id_picks_that_gateway(box):
    _gateway("Office", numbers=["+15555550111"])
    second = _gateway("Shop", numbers=["", "+15555550122"])
    _call(gateway_id=second["id"])
    assert box["placed"][-1]["trunk"] == "tg-shop"
    assert box["placed"][-1]["caller_id"] == "+15555550122"


def test_disabled_gateways_and_ports_are_skipped(box):
    _gateway("Old", numbers=["+15555550133"], enabled=False)
    _gateway("Dark", numbers=["+15555550144"], ports_enabled=False)
    live = _gateway("Live", numbers=["+15555550155"])
    _call()
    assert box["placed"][-1]["trunk"] == live["trunk_name"]


def test_env_values_override_the_registry_default_only(box, monkeypatch):
    gw = _gateway("Office", numbers=["+15555550111"])
    monkeypatch.setattr(acfg, "TG200_TRUNK_NAME", "tg-custom")
    monkeypatch.setattr(acfg, "TG200_PHONE_NUMBER", "+15555550199")
    _call()
    assert box["placed"][-1] == {"to": "+15550100123", "trunk": "tg-custom",
                                 "caller_id": "+15555550199"}
    _call(gateway_id=gw["id"])  # an explicit gateway beats the env default
    assert box["placed"][-1]["trunk"] == "tg-office"


def test_an_explicit_trunk_is_dialled_as_given(box):
    _gateway("Office", numbers=["+15555550111"])
    _call(trunk="tg-office")
    assert box["placed"][-1]["trunk"] == "tg-office"
    assert box["placed"][-1]["caller_id"] == "+15555550111"


def test_no_gateway_is_a_clear_refusal_not_a_failed_session(box):
    status, body = _body(_call())
    assert status == 409
    assert "gateway" in body["error"].lower() and "wizard" in body["error"].lower()
    assert box["placed"] == [] and ar.PHONE_SESSIONS == {}


def test_a_hand_configured_legacy_trunk_still_works(box):
    """A box set up before the gateway wizard: no registry, but Asterisk has
    the historical [tg200] endpoint."""
    box["ari"].endpoints.add("tg200")
    status, body = _body(_call())
    assert status == 200 and box["placed"][-1]["trunk"] == "tg200"


def test_the_make_phone_call_tool_reports_the_refusal(box, monkeypatch):
    from Orchestrator.telephony import call_error
    status, body = _body(_call())
    assert "gateway" in call_error(body, status).lower()
