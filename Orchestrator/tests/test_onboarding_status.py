"""Backend /onboarding/status rollup + SSE live re-validation (M1).

The rollup canonicalizes the provider->step join + attention-derivation rules
server-side so the hub frontend stays presentational. SECTIONS is the 10-section
catalog (welcome/done are the hub itself, NOT sections); it must never drift from
state.ALL_STEPS. Hermeticity mirrors test_onboarding_web_search.py's tmp_env.
"""
from Orchestrator.onboarding import status_rollup as sr
from Orchestrator.onboarding.state import ALL_STEPS


# Steps that are the hub itself, not status sections (design: welcome->hub
# basis, done->summary model).
_NON_SECTION_STEPS = {"welcome", "done"}


def test_sections_keys_are_all_steps_minus_welcome_and_done():
    section_keys = [s["key"] for s in sr.SECTIONS]
    expected = [s for s in ALL_STEPS if s not in _NON_SECTION_STEPS]
    assert section_keys == expected, (
        "status_rollup.SECTIONS drifted from state.ALL_STEPS:\n"
        f"  sections: {section_keys}\n"
        f"  expected: {expected}"
    )


def test_every_section_step_equals_its_key():
    # Contract: step == key for every section (the hub links ?step=<key>).
    for s in sr.SECTIONS:
        assert s["step"] == s["key"], f"{s['key']}: step != key"


def test_sections_have_required_shape():
    valid_groups = {"network", "keys", "capabilities", "identity"}
    for s in sr.SECTIONS:
        assert set(s) >= {"key", "group", "label", "required", "step"}
        assert s["group"] in valid_groups
        assert isinstance(s["required"], bool)


def _empty_inputs():
    """Minimal snapshot inputs for build_status (all unconfigured)."""
    return dict(
        env={},
        state={"completed_steps": [], "skipped_steps": [], "validated_at": {}},
        embeddings={"active": None, "health": {"state": "ok"}, "stores": [], "models": []},
        cli={"providers": {}, "ready": False},
        web_search={"enabled": [], "providers": {}, "default": ""},
        image={"enabled": [], "providers": {}, "default": ""},
        paired=[],
        operators=[],
        restart={"needs_restart": False, "drifted_keys": []},
    )


def _section(rollup, key):
    return next(s for s in rollup["sections"] if s["key"] == key)


def test_api_keys_attention_when_no_keys_present():
    rollup = sr.build_status(**_empty_inputs())
    sec = _section(rollup, "api_keys")
    # required + unsatisfied -> attention (NOT optional)
    assert sec["state"] == sr.ATTENTION
    assert sec["required"] is True


def test_api_keys_ready_when_a_key_present_and_validated():
    inp = _empty_inputs()
    inp["env"] = {"OPENAI_API_KEY": "sk-xxx"}
    inp["state"]["validated_at"] = {"openai": 1.0}
    rollup = sr.build_status(**inp)
    assert _section(rollup, "api_keys")["state"] == sr.READY


def test_api_keys_attention_when_present_but_never_validated():
    inp = _empty_inputs()
    inp["env"] = {"OPENAI_API_KEY": "sk-xxx"}  # present, validated_at empty
    rollup = sr.build_status(**inp)
    sec = _section(rollup, "api_keys")
    assert sec["state"] == sr.ATTENTION
    assert any(a["section"] == "api_keys" and a["severity"] == "warn"
               for a in rollup["attention"])


def test_operator_required_attention_when_none():
    rollup = sr.build_status(**_empty_inputs())
    assert _section(rollup, "operator")["state"] == sr.ATTENTION


def test_operator_ready_when_present():
    inp = _empty_inputs()
    inp["operators"] = ["Brandon"]
    rollup = sr.build_status(**inp)
    assert _section(rollup, "operator")["state"] == sr.READY


def test_rollup_top_level_shape():
    rollup = sr.build_status(**_empty_inputs())
    assert set(rollup) >= {"ready_count", "total", "is_complete", "sections", "attention"}
    assert rollup["total"] == len(sr.SECTIONS)
    assert isinstance(rollup["ready_count"], int)
    for sec in rollup["sections"]:
        assert set(sec) >= {"key", "group", "label", "state", "required",
                            "summary", "step", "skipped", "items"}
