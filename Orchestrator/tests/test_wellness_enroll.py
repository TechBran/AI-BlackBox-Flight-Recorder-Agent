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


def test_an_unanswered_replacement_expires(box, monkeypatch):
    import time
    assert box.enroll().status_code == 200
    new = _second_phone(box, monkeypatch)
    r = _enroll_from(box, new, "100.100.7.9", replace=True)
    pending_enr = r.json()["enrollment_id"]
    later = int(time.time() * 1000) + (enroll.REPLACE_PENDING_TTL_S + 60) * 1000
    with pytest.raises(enroll.EnrollError) as e:
        enroll.approve_replace(OPERATOR, True, now_ms=later)
    assert e.value.code == "no_pending_replace"
    assert enroll.get_enrollment(OPERATOR)["pending"] is None
    assert enroll.tombstone_reason(pending_enr) == "revoked"
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


# ------------------------------------------------------------------ hostile enrollment
#
# Every /devices route that touches the registry or the pairing rows is open
# inside the perimeter, so none of it may decide which phone holds an
# operator's history. These are the reviewed takeover paths; each must end
# with the operator's history still bound to the phone that last received a
# bind, and no new key able to read it.

ATT_IP = "100.100.8.8"
MINIMAL = {"state", "source_label", "last_sync", "enrolled"}


def _node(box, ip=ATT_IP, *, device="att-dev", owner=""):
    """Another tailnet node: its own listener and pairing row (POST
    /devices/pairing is open), and optionally a registry row of its own."""
    from Orchestrator.device_registry import pairing
    phone = FakePhone()
    box.ports[ip] = phone.port
    pairing.register(device or "att", ip, phone.secret, "Another phone", "phone")
    if device:
        add_device(device, ip, owner)
    return phone


def _as(box, phone, fn, *a, **kw):
    old = box.phone
    box.phone = phone
    try:
        return fn(*a, **kw)
    finally:
        box.phone = old


@pytest.mark.parametrize("update", [{"metadata": {"tailnet_ips": [ATT_IP]}},
                                    {"tailscale_ip": ATT_IP}])
def test_a_rewritten_registry_address_is_not_the_same_phone(box, update):
    """PUT /devices/{id} can point the source phone's row at any address, so
    a re-enroll from another address is a replacement: the source phone,
    which answers, must approve it, and until then the new key gets nothing."""
    assert box.enroll().status_code == 200
    victim = box.phone
    att = _node(box, device=None)
    assert box.client.put("/devices/fixture-phone", json=update).status_code == 200
    r = _enroll_from(box, att, ATT_IP, replace=True)
    assert r.status_code == 200 and r.json()["state"] == "replace_pending"
    rec = enroll.get_enrollment(OPERATOR)
    assert rec["enrollment_id"] == victim.enrollment_id and rec["tailnet_ip"] == PHONE_IP
    assert ("GET", "/healthz", None) in victim.calls
    assert any(n[2] == enroll.NOTICE_REPLACE_ASK[0] for n in box.notices)
    assert box.push(_batch(victim.enrollment_id, 1)).status_code == 200
    status = _as(box, att, box.get, f"/wellness/status?operator={OPERATOR}", signed=True)
    assert set(status.json()) == MINIMAL
    widen = _as(box, att, box.post, "/wellness/settings",
                {"operator": OPERATOR, "categories": {"blood_pressure": "proactive"},
                 "providers_allowed": ["anthropic"]}, signed=True, method="PUT")
    assert widen.status_code == 403
    assert enroll.get_enrollment(OPERATOR)["categories"]["blood_pressure"] == "off"
    att.close()


def test_a_deleted_pairing_row_does_not_make_the_source_look_offline(box):
    """DELETE /devices/pairing/{ip} is open; the probe needs no pairing (the
    phone serves GET /healthz without it), so the online phone still answers
    and the replacement waits for its tap."""
    from Orchestrator.device_registry import pairing
    assert box.enroll().status_code == 200
    victim = box.phone
    att = _node(box)
    assert pairing.forget(PHONE_IP)
    r = _enroll_from(box, att, ATT_IP, replace=True, assign_to_me=True)
    assert r.status_code == 200 and r.json()["state"] == "replace_pending"
    assert ("GET", "/healthz", None) in victim.calls
    assert enroll.get_enrollment(OPERATOR)["device_id"] == "fixture-phone"
    att.close()


def test_kept_history_never_moves_to_another_phone_without_a_live_key(box):
    """After a keep-history disconnect (unsigned, from any surface) no phone
    holds a key that could approve a move, so another phone starts EMPTY and
    the kept store stays detached until erase removes it too."""
    assert box.enroll().status_code == 200
    victim = box.phone
    assert box.push(_batch(victim.enrollment_id, 1)).status_code == 200
    kept = enroll.get_enrollment(OPERATOR)["op_key"]
    att = _node(box)
    r = box.post("/wellness/disconnect", {"operator": OPERATOR, "keep_history": True})
    assert r.status_code == 200
    r = _enroll_from(box, att, ATT_IP, assign_to_me=True)
    assert r.status_code == 200 and r.json()["state"] == "active"
    rec = enroll.get_enrollment(OPERATOR)
    assert rec["device_id"] == "att-dev" and rec["op_key"] != kept
    assert rec["detached_op_keys"] == [kept] and paths.db_path(kept).exists()
    status = _as(box, att, box.get, f"/wellness/status?operator={OPERATOR}", signed=True).json()
    assert status["sync"]["outbox_hint"]["batches_received"] == 0
    assert _as(box, att, box.post, "/wellness/erase", {"operator": OPERATOR},
               signed=True).status_code == 200
    assert not paths.db_path(kept).exists() and not paths.db_path(rec["op_key"]).exists()
    att.close()


def test_the_same_phone_gets_its_kept_history_back(box):
    assert box.enroll().status_code == 200
    assert box.push(_batch(box.phone.enrollment_id, 1)).status_code == 200
    kept = enroll.get_enrollment(OPERATOR)["op_key"]
    assert box.post("/wellness/disconnect", {"operator": OPERATOR,
                                             "keep_history": True}).status_code == 200
    r = box.enroll()
    assert r.status_code == 200 and r.json()["epoch"] == 2
    rec = enroll.get_enrollment(OPERATOR)
    assert rec["op_key"] == kept and not rec.get("detached_op_keys")


def test_a_rehomed_source_never_hands_its_history_to_another_node(box):
    """Unassign (open) flips the enrollment to ownership_changed; claiming
    another node for the operator (open) and enrolling it gets an empty
    store, and the old phone's key now answers 410 source_moved."""
    assert box.enroll().status_code == 200
    victim = box.phone
    assert box.push(_batch(victim.enrollment_id, 1)).status_code == 200
    kept = enroll.get_enrollment(OPERATOR)["op_key"]
    att = _node(box)
    assert box.client.post("/devices/fixture-phone/unassign",
                           json={"operator": OTHER}).status_code == 200
    assert box.push(_batch(victim.enrollment_id, 1, i=1)).status_code == 409
    assert box.client.post("/devices/att-dev/operator",
                           json={"operator": OPERATOR}).status_code == 200
    r = _enroll_from(box, att, ATT_IP)
    assert r.status_code == 200 and r.json()["state"] == "active"
    rec = enroll.get_enrollment(OPERATOR)
    assert rec["device_id"] == "att-dev" and rec["op_key"] != kept
    assert box.push(_batch(victim.enrollment_id, 1, i=1)).json() == {"error": "source_moved"}
    att.close()


def test_reenrolling_from_ownership_changed_revokes_the_old_key(box):
    assert box.enroll().status_code == 200
    old_enr, old_key = box.phone.enrollment_id, box.phone.key
    box.client.post("/devices/fixture-phone/unassign", json={"operator": OTHER})
    assert box.push(_batch(old_enr, 1)).status_code == 409
    box.client.post("/devices/fixture-phone/operator", json={"operator": OPERATOR})
    r = box.enroll()
    assert r.status_code == 200 and r.json()["epoch"] == 2
    assert old_enr not in _keys() and enroll.tombstone_reason(old_enr) == "revoked"
    box.phone.key, box.phone.enrollment_id = old_key, old_enr
    assert box.push(_batch(old_enr, 1, i=1)).json() == {"error": "revoked"}


def test_a_superseded_replacement_is_revoked(box, monkeypatch):
    assert box.enroll().status_code == 200
    new = _second_phone(box, monkeypatch)
    pend = _enroll_from(box, new, "100.100.7.9", replace=True).json()
    assert pend["state"] == "replace_pending"
    r = box.enroll(replace=True)                  # the source phone re-enrolls itself
    assert r.status_code == 200 and r.json()["state"] == "active"
    assert enroll.get_enrollment(OPERATOR)["pending"] is None
    assert enroll.tombstone_reason(pend["enrollment_id"]) == "revoked"
    box.phone = new
    assert box.push(_batch(new.enrollment_id, pend["epoch"])).json() == {"error": "revoked"}
    new.close()


def test_a_pending_replacement_leaves_the_source_alone_until_approved(box, monkeypatch):
    import copy
    assert box.enroll().status_code == 200
    rec = enroll.get_enrollment(OPERATOR)
    label = rec["source_label"]
    new = _second_phone(box, monkeypatch)
    report = copy.deepcopy(FX.load("continuous_120")["capabilities"])
    report["phone"].update({"device_name": "Phone two", "model": "SM-TEST2"})
    r = _enroll_from(box, new, "100.100.7.9", replace=True, capability_report=report)
    assert r.json()["state"] == "replace_pending"
    st = enroll.store_for(enroll.get_enrollment(OPERATOR), create=False)
    assert enroll.get_enrollment(OPERATOR)["source_label"] == label
    assert st.meta_get("capabilities")["phone"]["model"] == "SM-TEST1"
    assert box.post("/wellness/replace-approve", {"operator": OPERATOR, "approve": True},
                    signed=True).status_code == 200
    rec = enroll.get_enrollment(OPERATOR)
    assert rec["source_label"] == "Phone two (SM-TEST2)"
    assert rec["last_source"]["tailnet_ip"] == "100.100.7.9"
    assert st.meta_get("capabilities")["phone"]["model"] == "SM-TEST2"
    new.close()


def test_enroll_refuses_a_device_that_is_not_android(box):
    from Orchestrator.device_registry import pairing
    add_device("a-laptop", "100.100.7.21", OPERATOR, kind="linux")
    pairing.register("a-laptop", "100.100.7.21", "c" * 64, "Laptop", "laptop")
    r = box.enroll(ip="100.100.7.21")
    assert r.status_code == 422 and r.json()["error"] == "not_android"
    assert "100.100.7.21" not in box.dialed


def test_owner_spelling_is_compared_like_the_device_routes_do(box):
    from Orchestrator.device_registry import get_registry
    get_registry().update_device("fixture-phone", owner=OPERATOR.upper())
    r = box.post("/wellness/select", {"operator": OPERATOR, "device_id": "fixture-phone"})
    assert r.status_code == 200
    body = box.enroll().json()
    assert body["state"] == "active"
    assert box.push(_batch(body["enrollment_id"], 1)).status_code == 200
    assert enroll.get_enrollment(OPERATOR)["state"] == "active"


# ------------------------------------------------------------------ slow phones

def _drip_listener(seconds=6.0, step=0.25):
    """A listener that answers one header byte every `step` seconds: no
    single read ever times out, so only a total deadline stops it."""
    import socket
    import threading
    import time
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)

    def run():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            try:
                c.recv(65536)
                c.sendall(b"HTTP/1.1 200 OK\r\nX-Pad: ")
                for _ in range(int(seconds / step)):
                    time.sleep(step)
                    c.sendall(b"a")
                c.sendall(b"\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            finally:
                c.close()
    threading.Thread(target=run, daemon=True).start()
    return srv


def test_a_phone_call_has_a_total_deadline(box, monkeypatch):
    import time
    from Orchestrator.device_registry import pairing
    srv = _drip_listener()
    box.ports[ATT_IP] = srv.getsockname()[1]
    pairing.register("att", ATT_IP, "d" * 64, "Slow", "phone")
    t0 = time.monotonic()
    assert enroll.call_phone(ATT_IP, "POST", "/wellness/sync", {}, timeout=1.0) is None
    assert time.monotonic() - t0 < 2.5
    srv.close()


def test_a_slow_phone_holds_up_nobody_else(box, monkeypatch):
    """The bind runs outside the enrollment lock: a phone that drips its
    answer delays only its own enroll (cut off at the deadline), never
    another operator's pause, and a second enroll for the same operator is
    told one is in progress instead of queueing behind it."""
    import threading
    import time
    from Orchestrator.device_registry import pairing
    monkeypatch.setattr(enroll, "BIND_TIMEOUT_S", 1.5)
    srv = _drip_listener()
    box.ports[ATT_IP] = srv.getsockname()[1]
    add_device("att-dev", ATT_IP, "")
    pairing.register("att-dev", ATT_IP, "e" * 64, "Slow", "phone")
    write_record(OTHER, op_key="a" * 16, enrollment_id="en_aaaaaaaa", device_id="x",
                 tailnet_ip="100.100.7.30")
    out = {}

    def others():
        time.sleep(0.4)
        t = time.monotonic()
        enroll.pause(OTHER)
        out["pause_s"] = time.monotonic() - t
        out["second"] = box.enroll()             # the same operator, meanwhile
    th = threading.Thread(target=others)
    th.start()
    t0 = time.monotonic()
    r = box.post("/wellness/enroll", {"operator": OPERATOR, "install_id": "i",
                                      "nonce": "ab" * 16, "assign_to_me": True}, ip=ATT_IP)
    took = time.monotonic() - t0
    th.join()
    assert r.status_code == 502 and took < 3.5
    assert out["pause_s"] < 0.5
    assert out["second"].status_code == 409
    assert out["second"].json()["error"] == "enroll_in_progress"
    assert out["second"].headers["Retry-After"]
    assert enroll.get_enrollment(OPERATOR) is None
    srv.close()


def test_a_record_that_changed_during_the_bind_persists_nothing(box, monkeypatch):
    """The record is re-read before anything is written: a pause (or an
    erase, a select, another phone's push flipping ownership) that landed
    while the bind was out means a 409, and the old key stays the key."""
    first = box.enroll().json()
    real = enroll.call_phone

    def bind_then_pause(ip, method, path, body, *, timeout):
        status = real(ip, method, path, body, timeout=timeout)
        if path == "/wellness/bind":
            enroll.pause(OPERATOR)
        return status
    monkeypatch.setattr(enroll, "call_phone", bind_then_pause)
    r = box.enroll(replace=True)
    assert r.status_code == 409 and r.json()["error"] == "enroll_conflict"
    rec = enroll.get_enrollment(OPERATOR)
    assert rec["enrollment_id"] == first["enrollment_id"] and rec["state"] == "paused"
    assert list(_keys()) == [first["enrollment_id"]]
    assert OPERATOR not in enroll._INFLIGHT
