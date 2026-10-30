"""Enrollment (spec §3.1) through the real app: the bind handshake, E1-E7,
the bind going ONLY to the resolved address, a nonce mismatch, a planted
pairing secret, replacing a source with the old phone online or offline,
"Use for health" (awaiting_phone), and owner_changed on ingest."""
from __future__ import annotations

import json

import pytest

from Orchestrator.tests.wellness_support import (OPERATOR, PHONE_IP, FakePhone, add_device,
                                                 close_box, fixtures, make_box, write_record)
from Orchestrator.wellness import enroll, paths

FX = fixtures()
OTHER = "other-human"


@pytest.fixture
def box(isolated_stores, toolvault_embed_sync, monkeypatch):
    b = make_box(isolated_stores, monkeypatch)
    yield b
    close_box(b)


def _batch(enr, epoch, i=0):
    b = dict(FX.load("days_10")["batches"][i])
    return dict(b, enrollment_id=enr, epoch=epoch)


def _keys():
    return json.loads(paths.keys_path().read_text())


# ------------------------------------------------------------------ the handshake

def test_enroll_binds_and_persists(box):
    r = box.enroll()
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"enrollment_id", "epoch", "state", "op_key_hint", "sync_interval_min",
                         "policy_version"}
    assert body["state"] == "active" and body["epoch"] == 1 and body["op_key_hint"] is None
    assert body["sync_interval_min"] == 60 and body["policy_version"] == "2026-10-29"
    assert box.phone.enrollment_id == body["enrollment_id"] and box.phone.key
    assert _keys()[body["enrollment_id"]]["k"] == box.phone.key
    rec = enroll.get_enrollment(OPERATOR)
    assert rec["state"] == "active" and rec["tailnet_ip"] == PHONE_IP
    assert rec["device_id"] == "fixture-phone" and rec["source_label"] == \
        "Fixture phone (SM-TEST1)"
    assert paths.db_path(rec["op_key"]).exists()
    assert box.dialed == [PHONE_IP]
    bind = [c for c in box.phone.calls if c[1] == "/wellness/bind"][0][2]
    assert set(bind) == {"operator", "enrollment_id", "epoch", "nonce", "k"}
    assert bind["operator"] == OPERATOR


def test_bind_goes_only_to_the_resolved_address(box):
    """The body's address list is never where the key is sent."""
    r = box.enroll(tailnet_ips=["100.100.9.9", "100.100.9.10"])
    assert r.status_code == 200
    assert box.dialed == [PHONE_IP]


def test_nonce_mismatch_persists_nothing(box):
    box.phone.mint_nonce()
    r = box.post("/wellness/enroll", {"operator": OPERATOR, "install_id": "i",
                                      "tailnet_ips": [PHONE_IP], "nonce": "ab" * 16})
    assert r.status_code == 502 and r.json() == {"error": "bind_failed"}
    assert enroll.get_enrollment(OPERATOR) is None
    assert not paths.keys_path().exists() or _keys() == {}


def test_a_planted_pairing_secret_cannot_receive_the_bind(box):
    """A tailnet peer can overwrite the pairing row for the phone's address,
    but the bind still goes to the real phone, which refuses the bearer."""
    from Orchestrator.device_registry import pairing
    pairing.register("attacker", PHONE_IP, "f" * 64, "evil", "phone")
    r = box.enroll()
    assert r.status_code == 502 and enroll.get_enrollment(OPERATOR) is None
    assert box.phone.key is None


def test_not_paired_is_424_with_the_fix(box):
    from Orchestrator.device_registry import pairing
    pairing.forget(PHONE_IP)
    r = box.enroll()
    assert r.status_code == 424
    assert r.json() == {"error": "not_paired", "fix": "Open the BlackBox app once on this phone"}


def test_unknown_address_and_bare_loopback(box):
    r = box.enroll(ip="100.100.3.3")
    assert r.status_code == 404 and r.json()["error"] == "unknown_device"
    bare = box.client.post("/wellness/enroll", json={"operator": OPERATOR, "install_id": "i",
                                                     "nonce": "ab" * 16})
    assert bare.status_code == 404                     # 127.0.0.1 itself is never a phone


def test_x_forwarded_for_counts_only_behind_the_local_proxy(box):
    from fastapi.testclient import TestClient
    from Orchestrator.app import app
    with TestClient(app, client=("100.100.5.5", 40000)) as other:
        r = other.post("/wellness/enroll",
                       json={"operator": OPERATOR, "install_id": "i", "nonce": "ab" * 16},
                       headers={"X-Forwarded-For": PHONE_IP})
    assert r.status_code == 404                        # the peer's own address, no device


# ------------------------------------------------------------------ E1-E7

def test_e3_owner_mismatch(box):
    from Orchestrator.device_registry import get_registry
    get_registry().update_device("fixture-phone", owner=OTHER)
    r = box.enroll()
    assert r.status_code == 409 and r.json()["error"] == "owner_mismatch"


def test_e3_unowned_needs_assign_to_me(box):
    from Orchestrator.device_registry import get_registry
    get_registry().update_device("fixture-phone", owner="")
    r = box.enroll()
    assert r.status_code == 409 and r.json()["error"] == "owner_mismatch"
    assert "Assign this phone to me" in r.json()["detail"]
    r = box.enroll(assign_to_me=True)
    assert r.status_code == 200
    assert get_registry().get_device("fixture-phone").owner == OPERATOR


@pytest.mark.parametrize("who,status", [("system", 403), ("Flight Recorder", 403),
                                        ("not-on-roster", 403), ("../etc", 422), ("", 422)])
def test_e5_only_a_live_human(box, who, status):
    r = box.enroll(operator=who)
    assert r.status_code == status


def test_e1_one_source_per_operator(box):
    assert box.enroll().status_code == 200
    r = box.enroll()
    assert r.status_code == 409 and r.json() == {"error": "already_enrolled"}


def test_e2_one_operator_per_device(box):
    write_record(OTHER, op_key="1" * 16, enrollment_id="en_00000099", device_id="fixture-phone")
    r = box.enroll()
    assert r.status_code == 409 and r.json() == {"error": "device_in_use"}


def test_kill_switch_refuses_enrollment(box):
    from Orchestrator.utils.config_ini import update_config_ini

    def off(p):
        if not p.has_section("wellness"):
            p.add_section("wellness")
        p.set("wellness", "enabled", "false")
    update_config_ini(off)
    r = box.enroll()
    assert r.status_code == 503 and r.json()["error"] == "wellness_disabled"
    assert r.headers["Retry-After"]


# ------------------------------------------------------------------ re-enroll and replace

def test_reenroll_same_phone_bumps_the_epoch(box):
    first = box.enroll().json()
    old_key, old_enr = box.phone.key, box.phone.enrollment_id
    second = box.enroll(replace=True)
    assert second.status_code == 200 and second.json()["epoch"] == 2
    assert enroll.get_enrollment(OPERATOR)["op_key"]           # same store, kept
    assert enroll.tombstone_reason(first["enrollment_id"]) == "revoked"
    box.phone.key, box.phone.enrollment_id = old_key, old_enr
    r = box.push(_batch(old_enr, 1))
    assert r.status_code == 410 and r.json() == {"error": "revoked"}


def _second_phone(box, monkeypatch):
    from Orchestrator.device_registry import pairing
    new = FakePhone()
    add_device("fixture-phone-2", "100.100.7.9", OPERATOR)
    pairing.register("fixture-phone-2", "100.100.7.9", new.secret, "Phone two", "phone")
    box.ports["100.100.7.9"] = new.port
    return new


def _enroll_from(box, phone, ip, **over):
    old = box.phone
    box.phone = phone
    try:
        return box.enroll(ip=ip, **over)
    finally:
        box.phone = old


def test_replace_with_the_old_phone_offline(box, monkeypatch):
    assert box.enroll().status_code == 200
    old = box.phone
    new = _second_phone(box, monkeypatch)
    old.down = True
    r = _enroll_from(box, new, "100.100.7.9", replace=True)
    assert r.status_code == 200 and r.json()["state"] == "active" and r.json()["epoch"] == 2
    assert enroll.get_enrollment(OPERATOR)["device_id"] == "fixture-phone-2"
    assert any(n[2] == enroll.NOTICE_MOVED[0] for n in box.notices)
    old.down = False
    r = box.push(_batch(old.enrollment_id, 1))
    assert r.status_code == 410 and r.json() == {"error": "source_moved"}
    new.close()


def test_replace_with_the_old_phone_online_needs_its_tap(box, monkeypatch):
    assert box.enroll().status_code == 200
    old = box.phone
    new = _second_phone(box, monkeypatch)
    r = _enroll_from(box, new, "100.100.7.9", replace=True)
    assert r.status_code == 200 and r.json()["state"] == "replace_pending"
    assert any(n[2] == enroll.NOTICE_REPLACE_ASK[0] for n in box.notices)
    # The new phone waits; the old one keeps working until it answers.
    box.phone = new
    r = box.push(_batch(new.enrollment_id, 2), ip="100.100.7.9")
    assert r.status_code == 503 and r.json() == {"error": "replace_pending"}
    box.phone = old
    assert box.push(_batch(old.enrollment_id, 1)).status_code == 200
    r = box.post("/wellness/replace-approve", {"operator": OPERATOR, "approve": True},
                 signed=True)
    assert r.status_code == 200, r.text
    assert box.push(_batch(old.enrollment_id, 1, i=1)).json() == {"error": "source_moved"}
    box.phone = new
    ok = box.push(_batch(new.enrollment_id, 2, i=1), ip="100.100.7.9")
    assert ok.status_code == 200 and ok.json()["accepted"] > 0
    new.close()


def test_replace_refused_by_the_old_phone(box, monkeypatch):
    assert box.enroll().status_code == 200
    old = box.phone
    new = _second_phone(box, monkeypatch)
    _enroll_from(box, new, "100.100.7.9", replace=True)
    r = box.post("/wellness/replace-approve", {"operator": OPERATOR, "approve": False},
                 signed=True)
    assert r.status_code == 200
    box.phone = new
    assert box.push(_batch(new.enrollment_id, 2)).json() == {"error": "revoked"}
    box.phone = old
    assert box.push(_batch(old.enrollment_id, 1)).status_code == 200
    new.close()


# ------------------------------------------------------------------ select (Portal)

def test_use_for_health_sets_awaiting_phone(box):
    r = box.post("/wellness/select", {"operator": OPERATOR, "device_id": "fixture-phone"})
    assert r.status_code == 200 and r.json() == {"state": "awaiting_phone"}
    assert box.notices == [(OPERATOR, "fixture-phone", enroll.NOTICE_SELECT[0])]
    assert enroll.get_enrollment(OPERATOR)["state"] == "awaiting_phone"
    status = box.get(f"/wellness/status?operator={OPERATOR}").json()
    assert status["state"] == "awaiting_phone" and status["enrolled"] is False
    r = box.enroll()
    assert r.status_code == 200 and r.json()["epoch"] == 1


def test_select_refusals(box):
    add_device("someone-elses", "100.100.7.20", OTHER)
    add_device("a-laptop", "100.100.7.21", OPERATOR, kind="linux")
    assert box.post("/wellness/select", {"operator": OPERATOR, "device_id": "someone-elses"}
                    ).status_code == 403
    assert box.post("/wellness/select", {"operator": OPERATOR, "device_id": "a-laptop"}
                    ).status_code == 422
    assert box.post("/wellness/select", {"operator": OPERATOR, "device_id": "nope"}
                    ).status_code == 404
    assert box.post("/wellness/select", {"operator": "system", "device_id": "fixture-phone"}
                    ).status_code == 403
    box.enroll()
    assert box.post("/wellness/select", {"operator": OPERATOR, "device_id": "fixture-phone"}
                    ).json() == {"error": "already_enrolled"}


# ------------------------------------------------------------------ E3 on every request

def test_rehomed_device_answers_owner_changed(box):
    body = box.enroll().json()
    from Orchestrator.device_registry import get_registry
    get_registry().update_device("fixture-phone", owner=OTHER)
    r = box.push(_batch(body["enrollment_id"], 1))
    assert r.status_code == 409 and r.json() == {"error": "owner_changed"}
    assert enroll.get_enrollment(OPERATOR)["state"] == "ownership_changed"
    assert box.get(f"/wellness/status?operator={OPERATOR}").json()["state"] == \
        "ownership_changed"
    r = box.push(_batch(body["enrollment_id"], 1, i=1))
    assert r.status_code == 409


# ------------------------------------------------------------------ the bus helper

def test_notify_device_reaches_one_device(monkeypatch):
    import asyncio

    from Orchestrator.notifications import bus
    sent = []

    async def fake_post(device, payload):
        sent.append((device, payload))
        return {"ok": True}

    monkeypatch.setattr(bus, "_post_to_device", fake_post)
    res = asyncio.run(bus.notify_device(OPERATOR, "fixture-phone", PHONE_IP,
                                        *enroll.NOTICE_SELECT, category="wellness"))
    assert res.delivered == ["fixture-phone"] and res.recorded is False
    device, payload = sent[0]
    assert device["node"]["ip"] == PHONE_IP and len(sent) == 1
    assert payload["category"] == "wellness" and payload["body"] == enroll.NOTICE_SELECT[1]


def test_notify_device_never_raises(monkeypatch):
    import asyncio

    from Orchestrator.notifications import bus

    async def dead(device, payload):
        raise OSError("unreachable")

    monkeypatch.setattr(bus, "_post_to_device", dead)
    res = asyncio.run(bus.notify_device(OPERATOR, "fixture-phone", PHONE_IP, "t", "b"))
    assert res.delivered == [] and res.unreachable == ["fixture-phone"]
