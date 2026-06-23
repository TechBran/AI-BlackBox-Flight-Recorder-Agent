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
