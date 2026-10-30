"""PUT /wellness/settings and the widening rule (spec W21): anything that
WIDENS access needs the enrolled phone's signature; narrowing and the health
instinct prompt are accepted from any surface for that operator. Every change
writes a consent_event."""
from __future__ import annotations

import pytest

from Orchestrator.tests.wellness_support import OPERATOR, close_box, make_box
from Orchestrator.wellness import catalog, enroll


@pytest.fixture
def box(isolated_stores, toolvault_embed_sync, monkeypatch):
    b = make_box(isolated_stores, monkeypatch)
    assert b.enroll().status_code == 200
    yield b
    close_box(b)


def _put(box, body, signed=False):
    return box.post("/wellness/settings", dict({"operator": OPERATOR}, **body), signed=signed,
                    method="PUT")


@pytest.mark.parametrize("body", [
    {"categories": {"blood_pressure": "ask"}},
    {"categories": {"sleep": "proactive"}},
    {"providers_allowed": ["anthropic"]},
    {"local_only": {"blood_pressure": False}},
    {"google_paid_key_confirmed": True},
    {"surfaces_mode": "household"},
])
def test_widening_needs_the_signature(box, body):
    r = _put(box, body)
    assert r.status_code == 403 and r.json()["error"] == "signature_required"
    assert "on your phone" in r.json()["fix"]
    signed = _put(box, body, signed=True)
    assert signed.status_code == 200, signed.text


@pytest.mark.parametrize("body,check", [
    ({"categories": {"sleep": "off"}}, lambda r: r["categories"]["sleep"] == "off"),
    ({"local_only": {"sleep": True}}, lambda r: r["local_only"]["sleep"] is True),
    ({"surfaces_mode": "credentialed"}, lambda r: r["surfaces_mode"] == "credentialed"),
])
def test_narrowing_works_from_any_surface(box, body, check):
    r = _put(box, body)
    assert r.status_code == 200, r.text
    assert check(enroll.get_enrollment(OPERATOR))


def test_removing_a_provider_is_narrowing(box):
    assert _put(box, {"providers_allowed": ["anthropic", "openai"]}, signed=True).status_code \
        == 200
    r = _put(box, {"providers_allowed": ["openai"]})
    assert r.status_code == 200
    assert enroll.get_enrollment(OPERATOR)["providers_allowed"] == ["openai"]


def test_the_phone_gets_the_full_status_back(box):
    body = _put(box, {"categories": {"activity": "off"}}, signed=True).json()
    assert body["categories"]["activity"] == "off" and "troubles" in body
    unsigned = _put(box, {"categories": {"heart": "off"}}).json()
    assert set(unsigned) == {"state", "source_label", "last_sync", "enrolled"}


def test_instinct_prompt_from_any_surface(box):
    r = _put(box, {"instinct_prompt": "Check my sleep first.\x07 Keep it short.\n\tThanks"})
    assert r.status_code == 200
    assert enroll.get_enrollment(OPERATOR)["instinct_prompt"] == \
        "Check my sleep first. Keep it short.\n Thanks"
    assert enroll.instinct_prompt(OPERATOR).startswith("Check my sleep first.")
    assert _put(box, {"instinct_prompt": None}, signed=True).json()["instinct_prompt"] is None
    assert enroll.instinct_prompt(OPERATOR) == catalog.DEFAULT_INSTINCT_PROMPT
    long = _put(box, {"instinct_prompt": "x" * (catalog.INSTINCT_PROMPT_MAX + 1)})
    assert long.status_code == 422


def test_malformed_settings_are_422(box):
    assert _put(box, {"categories": {"sleep": "always"}}).status_code == 422
    assert _put(box, {"categoreis": {"sleep": "off"}}).status_code == 422   # a typo never passes
    assert _put(box, {"providers_allowed": ["nowhere-ai"]}, signed=True).status_code == 422


def test_settings_for_an_operator_without_health(box):
    r = box.post("/wellness/settings", {"operator": "other-human",
                                        "categories": {"sleep": "off"}}, method="PUT")
    assert r.status_code == 409 and r.json() == {"error": "not_enrolled"}


def test_every_change_is_a_consent_event(box):
    _put(box, {"categories": {"sleep": "off"}})
    _put(box, {"providers_allowed": ["anthropic"]}, signed=True)
    st = enroll.store_for(enroll.get_enrollment(OPERATOR))
    with st.read() as conn:
        rows = conn.execute("SELECT change, surface, policy_version FROM consent_event "
                            "ORDER BY at").fetchall()
    surfaces = [r[1] for r in rows]
    assert surfaces[-2:] == ["any", "android"]
    assert all(r[2] == "2026-10-29" for r in rows)


def test_the_kill_switch_still_allows_narrowing(box):
    from Orchestrator.utils.config_ini import update_config_ini

    def off(p):
        if not p.has_section("wellness"):
            p.add_section("wellness")
        p.set("wellness", "enabled", "false")
    update_config_ini(off)
    assert _put(box, {"categories": {"sleep": "off"}}).status_code == 200
    assert box.post("/wellness/pause", {"operator": OPERATOR}).status_code == 200
    assert _put(box, {"categories": {"blood_pressure": "ask"}}, signed=True).status_code == 503
