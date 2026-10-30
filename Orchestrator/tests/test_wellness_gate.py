"""The gate (spec §4.1): the six checks in order, default-deny. The full
matrix of operator x surface x provider x category x enrollment state through
the pure evaluate(), the turn contextvar, household mode, and the kill switch
read fresh from config.ini (no restart)."""
from __future__ import annotations

import contextvars
import itertools
import threading
from pathlib import Path

import pytest

from Orchestrator.tests.wellness_support import OPERATOR, roster, sandbox, write_record
from Orchestrator.wellness import gate, turn as turn_mod

OTHER = "other-human"
HUMANS = [OPERATOR, OTHER, "Flight Recorder"]
RESERVED = {"Flight Recorder", "system"}

ENROLLMENT = {"state": "active", "surfaces_mode": "auto",
              "categories": {"sleep": "ask", "heart": "proactive", "activity": "ask",
                             "blood_pressure": "off"},
              "local_only": {"sleep": False, "heart": False, "activity": False,
                             "blood_pressure": True},
              "providers_allowed": ["anthropic"], "google_paid_key_confirmed": False}


def _turn(**kw):
    base = {"operator": OPERATOR, "surface": turn_mod.SURFACE_ANDROID, "credential_ok": True,
            "provider": "anthropic"}
    base.update(kw)
    return turn_mod.WellnessTurn(**base)


def _eval(turn=None, *, enabled=True, roster_=HUMANS, operator=OPERATOR, enrollment=ENROLLMENT,
          category="sleep", dest=(True, None)):
    return gate.evaluate(enabled=enabled, roster=roster_, reserved=RESERVED, operator=operator,
                         turn=turn if turn is not None else _turn(), enrollment=enrollment,
                         category=category, destination_ok=lambda: dest)


def test_all_checks_pass():
    r = _eval()
    assert r.ok and r.code is None and r.paused is False


def test_unstamped_turn_is_denied():
    r = gate.evaluate(enabled=True, roster=HUMANS, reserved=RESERVED, operator=OPERATOR,
                      turn=None, enrollment=ENROLLMENT, category="sleep",
                      destination_ok=lambda: (True, None))
    assert (r.ok, r.code, r.reason) == (False, "not_allowed", "unstamped")


def test_checks_run_in_order():
    """Everything wrong at once: the FIRST check answers."""
    bad_turn = _turn(surface="sms", credential_ok=False)
    assert _eval(bad_turn, enabled=False, enrollment=None, category="blood_pressure",
                 dest=(False, "x")).code == "wellness_disabled"
    assert _eval(bad_turn, operator=OTHER, enrollment=None, category="blood_pressure",
                 dest=(False, "x")).code == "not_allowed"
    assert _eval(bad_turn, enrollment=None, category="blood_pressure",
                 dest=(False, "x")).code == "not_enrolled"
    assert _eval(bad_turn, category="blood_pressure", dest=(False, "x")).code == "category_off"
    assert _eval(bad_turn, dest=(False, "x")).code == "surface_not_allowed"
    assert _eval(dest=(False, "not_ticked")).code == "provider_not_allowed"


SURFACES = sorted(turn_mod.ALLOWED_SURFACES | turn_mod.NEVER_SURFACES | {"", "brand-new"})
STATES = ["active", "paused", "stale", "needs_attention", "awaiting_phone", "revoked",
          "ownership_changed", "replace_pending", None]
OPERATORS = [OPERATOR, OTHER, "system", "Flight Recorder", "", "not-on-roster"]
CATEGORIES = ["sleep", "heart", "activity", "blood_pressure", "made_up"]
DESTS = [(True, None), (False, "not_ticked")]


def _expected(op, surface, cred, state, category, dest):
    # The turn and the tool run for the same `op` here, so only who op IS
    # matters at check 2: a live human, never system / the Flight Recorder.
    if op not in (OPERATOR, OTHER):
        return "not_allowed"
    if state not in ("active", "paused", "stale", "needs_attention"):
        return "not_enrolled"
    if category not in ("sleep", "heart", "activity"):
        return "category_off"
    if surface not in turn_mod.ALLOWED_SURFACES or not cred:
        return "surface_not_allowed"
    if not dest[0]:
        return "provider_not_allowed"
    return None


def test_full_matrix():
    n = 0
    for op, surface, cred, state, category, dest in itertools.product(
            OPERATORS, SURFACES, (True, False), STATES, CATEGORIES, DESTS):
        t = turn_mod.WellnessTurn(operator=op, surface=surface,
                                  credential_ok=cred, provider="anthropic")
        enrollment = None if state is None else dict(ENROLLMENT, state=state)
        r = gate.evaluate(enabled=True, roster=HUMANS, reserved=RESERVED, operator=op,
                          turn=t, enrollment=enrollment, category=category,
                          destination_ok=lambda d=dest: d)
        assert r.code == _expected(op, surface, cred, state, category, dest), \
            (op, surface, cred, state, category, dest, r)
        assert r.ok is (r.code is None)
        n += 1
    assert n > 10_000


def test_turn_operator_must_equal_the_tool_operator():
    assert _eval(_turn(operator=OTHER)).code == "not_allowed"


def test_never_surfaces_ignore_credentials():
    for surface in turn_mod.NEVER_SURFACES:
        assert _eval(_turn(surface=surface, credential_ok=True)).code == "surface_not_allowed"


def test_household_mode():
    uncredentialed = _turn(surface=turn_mod.SURFACE_PORTAL, credential_ok=False)
    # auto: one human on the box means household; two means credentialed.
    assert _eval(uncredentialed, roster_=[OPERATOR, "Flight Recorder"]).ok
    assert _eval(uncredentialed).code == "surface_not_allowed"
    assert _eval(uncredentialed, enrollment=dict(ENROLLMENT, surfaces_mode="household")).ok
    assert _eval(uncredentialed, roster_=[OPERATOR],
                 enrollment=dict(ENROLLMENT, surfaces_mode="credentialed")).code == \
        "surface_not_allowed"
    # Household never opens the never-list.
    assert _eval(_turn(surface="sms", credential_ok=False), roster_=[OPERATOR]).code == \
        "surface_not_allowed"


def test_paused_answers_and_says_so():
    r = _eval(enrollment=dict(ENROLLMENT, state="paused"))
    assert r.ok and r.paused


def test_refusals_carry_a_fix():
    for code in gate.CODES:
        r = gate.GateResult(False, code)
        assert r.error() == {"ok": False, "error": code, "fix": gate.FIXES[code]}
        assert "Settings" in r.fix or code in ("wellness_disabled", "not_allowed",
                                               "surface_not_allowed")


# ------------------------------------------------------------------ the contextvar

def test_stamp_current_clear():
    assert turn_mod.current() is None
    token = turn_mod.stamp(_turn())
    try:
        assert turn_mod.current().operator == OPERATOR
    finally:
        turn_mod.clear(token)
    assert turn_mod.current() is None
    with pytest.raises(TypeError):
        turn_mod.stamp({"operator": OPERATOR})


def test_stamp_flows_into_worker_threads_via_copy_context():
    seen = []
    token = turn_mod.stamp(_turn(turn_key="wt_flow"))
    try:
        ctx = contextvars.copy_context()
        t = threading.Thread(target=lambda: seen.append(ctx.run(turn_mod.current)))
        t.start()
        t.join()
        bare = []
        t2 = threading.Thread(target=lambda: bare.append(turn_mod.current()))
        t2.start()
        t2.join()
    finally:
        turn_mod.clear(token)
    assert seen[0].turn_key == "wt_flow"
    assert bare == [None]          # a thread that was never handed the context gets nothing


def test_with_route_and_turn_keys():
    t = _turn().with_route("custom:lan", "lan::m")
    assert (t.provider, t.model, t.operator) == ("custom:lan", "lan::m", OPERATOR)
    assert turn_mod.new_turn_key() != turn_mod.new_turn_key()


# ------------------------------------------------------------------ check(): fresh reads

@pytest.fixture
def live(isolated_stores, tmp_path, monkeypatch):
    sandbox(tmp_path, monkeypatch)
    roster(monkeypatch, OPERATOR, OTHER, "Flight Recorder")
    write_record(categories=ENROLLMENT["categories"], providers_allowed=["anthropic"],
                 last_push_at="2099-01-01T00:00:00Z")
    return Path("config.ini")


def _set_enabled(path: Path, value: str):
    from Orchestrator.utils.config_ini import update_config_ini

    def edit(parser):
        if not parser.has_section("wellness"):
            parser.add_section("wellness")
        parser.set("wellness", "enabled", value)
    update_config_ini(edit, path)


def test_check_reads_everything_fresh(live):
    t = _turn()
    assert gate.check("sleep", turn=t).ok
    assert gate.check("blood_pressure", turn=t).code == "category_off"
    assert gate.check("sleep", turn=_turn(provider="openai")).code == "provider_not_allowed"
    assert gate.check("sleep", turn=t, operator=OTHER).code == "not_allowed"
    assert gate.allowed_categories(turn=t) == ["sleep", "heart", "activity"]


def test_kill_switch_takes_effect_without_a_restart(live):
    t = _turn()
    assert gate.check("sleep", turn=t).ok
    _set_enabled(live, "false")
    assert gate.check("sleep", turn=t).code == "wellness_disabled"
    _set_enabled(live, "true")
    assert gate.check("sleep", turn=t).ok


def test_unparseable_config_fails_closed(live):
    live.write_text("[wellness\nenabled = true\n")
    assert gate.check("sleep", turn=_turn()).code == "wellness_disabled"


def test_check_uses_the_stamped_turn(live):
    assert gate.check("sleep").code == "not_allowed"              # nothing stamped
    token = turn_mod.stamp(_turn())
    try:
        assert gate.check("sleep").ok
    finally:
        turn_mod.clear(token)


def test_category_revoked_on_the_next_turn(live):
    t = _turn()
    assert gate.check("sleep", turn=t).ok
    from Orchestrator.wellness import enroll
    with enroll.editing() as data:
        data[OPERATOR]["categories"]["sleep"] = "off"
    assert gate.check("sleep", turn=t).code == "category_off"
