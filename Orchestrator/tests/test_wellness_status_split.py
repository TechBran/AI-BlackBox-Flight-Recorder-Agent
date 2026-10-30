"""The status split (spec §4.1, rule W17): without the enrolled phone's
signature GET /wellness/status answers ONLY {state, source_label, last_sync,
enrolled}; signed by that operator's phone it adds the settings view; a
signature never unlocks anything about ANOTHER operator."""
from __future__ import annotations

import pytest

from Orchestrator.tests.wellness_support import OPERATOR, close_box, make_box, write_record
from Orchestrator.wellness import catalog

MINIMAL = {"state", "source_label", "last_sync", "enrolled"}
FULL_EXTRA = {"troubles", "categories", "local_only", "providers_allowed", "wear", "sync",
              "instinct_prompt", "policy_version", "disclosures_count"}
OTHER = "other-human"


@pytest.fixture
def box(isolated_stores, toolvault_embed_sync, monkeypatch):
    b = make_box(isolated_stores, monkeypatch)
    assert b.enroll().status_code == 200
    yield b
    close_box(b)


def test_uncredentialed_status_is_four_fields(box):
    r = box.get(f"/wellness/status?operator={OPERATOR}")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == MINIMAL
    assert body["state"] in ("active", "stale") and body["enrolled"] is True
    assert body["source_label"] == "Fixture phone (SM-TEST1)"


def test_not_enrolled_has_the_same_shape(box):
    body = box.get(f"/wellness/status?operator={OTHER}").json()
    assert body == {"state": "not_set_up", "source_label": None, "last_sync": None,
                    "enrolled": False}


def test_signed_status_is_the_full_view(box):
    r = box.get(f"/wellness/status?operator={OPERATOR}", signed=True)
    body = r.json()
    assert MINIMAL | FULL_EXTRA <= set(body)
    assert body["categories"]["blood_pressure"] == "off"
    assert body["local_only"]["blood_pressure"] is True
    assert body["wear"] == {"hr_mode": None, "nights_worn_7": 0}
    assert body["instinct_prompt"] is None
    assert body["instinct_prompt_default"] == catalog.DEFAULT_INSTINCT_PROMPT
    assert body["policy_version"] == "2026-10-29"


def test_a_signature_never_unlocks_another_operator(box):
    write_record(OTHER, op_key="2" * 16, enrollment_id="en_00000022", device_id="x",
                 phone_troubles=["permission_revoked:sleep"])
    body = box.get(f"/wellness/status?operator={OTHER}", signed=True).json()
    assert set(body) == MINIMAL


def test_troubles_and_settings_never_leak_uncredentialed(box):
    from Orchestrator.wellness import enroll
    with enroll.editing() as data:
        data[OPERATOR]["phone_troubles"] = ["permission_revoked:sleep", "foreground_only"]
        data[OPERATOR]["providers_allowed"] = ["anthropic"]
    body = box.get(f"/wellness/status?operator={OPERATOR}").json()
    assert set(body) == MINIMAL and body["state"] == "needs_attention"
    text = str(body)
    for word in ("permission", "anthropic", "foreground", "categories"):
        assert word not in text


def test_an_unsigned_settings_change_never_returns_the_settings_view(box):
    """A narrowing PUT works from any surface, so its answer must be no more
    than the uncredentialed status (else a no-op PUT would read it all)."""
    r = box.post("/wellness/settings", {"operator": OPERATOR}, method="PUT")
    assert r.status_code == 200 and set(r.json()) == MINIMAL
    r = box.post("/wellness/settings", {"operator": OPERATOR, "categories": {"sleep": "off"}},
                 method="PUT")
    assert set(r.json()) == MINIMAL
    signed = box.post("/wellness/settings", {"operator": OPERATOR}, method="PUT", signed=True)
    assert MINIMAL | FULL_EXTRA <= set(signed.json())


def test_a_bad_signature_is_refused_not_downgraded(box):
    r = box.get(f"/wellness/status?operator={OPERATOR}",
                headers={"X-BlackBox-Origin": "v1;enr=en_00000000;ts=1;nonce=" + "a" * 32 +
                         ";sig=" + "0" * 64})
    assert r.status_code == 401


@pytest.mark.parametrize("op", ["", "../x", "a,b"])
def test_invalid_operator_is_400(box, op):
    assert box.get(f"/wellness/status?operator={op}").status_code == 400


def test_privacy_policy_is_served_with_its_version(box):
    r = box.get("/wellness/privacy-policy")
    assert r.status_code == 200 and r.headers["X-Policy-Version"] == "2026-10-29"
    assert r.headers["content-type"].startswith("text/markdown")
    assert "2026-10-29" in r.text and "Health Connect" in r.text


def test_disclosures_and_refs_need_the_phone(box):
    from Orchestrator.wellness import api
    env = api.summary(OPERATOR, "today", delivery=api.Delivery("wt_1", "android", "local"))
    assert box.get("/wellness/disclosures").status_code == 401
    log = box.get("/wellness/disclosures", signed=True).json()["disclosures"]
    assert len(log) == 1 and log[0]["provider"] == "local" and log[0]["tool"] == \
        "wellness_summary"
    assert set(log[0]) == {"time", "surface", "surface_label", "provider", "model", "tool",
                           "categories"}
    assert (log[0]["surface"], log[0]["surface_label"]) == ("android", "BlackBox app")
    assert box.get(f"/wellness/ref/{env['ref']}").status_code == 401
    ref = box.get(f"/wellness/ref/{env['ref']}", signed=True).json()
    assert ref["payload"]["ref"] == env["ref"]
    assert box.get("/wellness/ref/w-20990101-0000", signed=True).status_code == 404


def test_troubles_are_codes_with_their_wording_alongside(box):
    """`troubles` is the code list the phone matches on (spec §3.5's
    vocabulary); `trouble_details` carries the wording for the same codes."""
    from Orchestrator.wellness import enroll
    with enroll.editing() as data:
        data[OPERATOR]["phone_troubles"] = ["permission_revoked:sleep", "foreground_only"]
    body = box.get(f"/wellness/status?operator={OPERATOR}", signed=True).json()
    assert body["troubles"][:2] == ["permission_revoked:sleep", "foreground_only"]
    assert all(isinstance(t, str) for t in body["troubles"])
    assert [t["code"] for t in body["trouble_details"]] == body["troubles"]
    assert {"code", "title", "body", "fix"} <= set(body["trouble_details"][0])


def test_a_replacement_awaiting_approval_reads_only_the_minimal_status(box):
    from Orchestrator.device_registry import pairing
    from Orchestrator.tests.wellness_support import FakePhone, add_device
    new = FakePhone()
    add_device("fixture-phone-2", "100.100.7.9", OPERATOR)
    pairing.register("fixture-phone-2", "100.100.7.9", new.secret, "Phone two", "phone")
    box.ports["100.100.7.9"] = new.port
    old = box.phone
    box.phone = new
    try:
        r = box.enroll(ip="100.100.7.9", replace=True)
        assert r.json()["state"] == "replace_pending"
        pending = box.get(f"/wellness/status?operator={OPERATOR}", signed=True)
        assert pending.status_code == 200 and set(pending.json()) == MINIMAL
    finally:
        box.phone = old
    assert MINIMAL | FULL_EXTRA <= set(box.get(f"/wellness/status?operator={OPERATOR}",
                                               signed=True).json())
    new.close()


def test_the_kill_switch_shows_in_the_status(box):
    """With [wellness] enabled=false every caller, the phone included, gets
    the four fields with state "disabled" (still enrolled as stored)."""
    from Orchestrator.utils.config_ini import update_config_ini

    def off(p):
        if not p.has_section("wellness"):
            p.add_section("wellness")
        p.set("wellness", "enabled", "false")
    update_config_ini(off)
    for signed in (False, True):
        body = box.get(f"/wellness/status?operator={OPERATOR}", signed=signed).json()
        assert set(body) == MINIMAL and body["state"] == "disabled" and body["enrolled"] is True
    assert box.get(f"/wellness/status?operator={OTHER}").json()["state"] == "disabled"
    assert box.get("/wellness/disclosures", signed=True).status_code == 503
