"""Backend ↔ frontend onboarding step-list parity guard (Task 13).

The wizard frontend hardcodes its own STEPS array (Portal/onboarding/
onboarding.js, with an adjacent STEP_LABELS map) in deliberate parallel to
the backend's ALL_STEPS (Orchestrator/onboarding/state.py). There is no JS
test infra, so this test parses the JS source with a regex and asserts the
two lists match EXACTLY (same names, same order). It guards BOTH lists
forever: add/move/remove a step on either side without the other and this
fails.

NOTE: intentionally a source-text test, not a behavioral one — keep the
`const STEPS = [...]` literal in onboarding.js greppable (no computed
construction) or update the regex here alongside.
"""
import re
from pathlib import Path

from Orchestrator.onboarding.state import ALL_STEPS

ONBOARDING_JS = (
    Path(__file__).resolve().parents[2] / "Portal" / "onboarding" / "onboarding.js"
)

STEPS_DIR = (
    Path(__file__).resolve().parents[2] / "Portal" / "onboarding" / "steps"
)


def _frontend_steps() -> list[str]:
    src = ONBOARDING_JS.read_text(encoding="utf-8")
    m = re.search(r"const STEPS\s*=\s*\[(.*?)\];", src, re.DOTALL)
    assert m, "could not find `const STEPS = [...]` in Portal/onboarding/onboarding.js"
    return re.findall(r'"([a-z0-9_]+)"', m.group(1))


def test_frontend_steps_match_backend_all_steps():
    steps = _frontend_steps()
    assert steps == list(ALL_STEPS), (
        "Portal/onboarding/onboarding.js STEPS and "
        "Orchestrator/onboarding/state.py ALL_STEPS have drifted apart:\n"
        f"  frontend: {steps}\n"
        f"  backend:  {list(ALL_STEPS)}"
    )


def test_frontend_step_labels_cover_every_step():
    """Every STEPS entry needs a STEP_LABELS entry (onboarding.js's own
    comment demands they stay in sync — enforce it)."""
    src = ONBOARDING_JS.read_text(encoding="utf-8")
    m = re.search(r"const STEP_LABELS\s*=\s*\{(.*?)\};", src, re.DOTALL)
    assert m, "could not find `const STEP_LABELS = {...}` in onboarding.js"
    label_keys = re.findall(r"^\s*([a-z0-9_]+)\s*:", m.group(1), re.MULTILINE)
    assert set(label_keys) == set(ALL_STEPS), (
        f"STEP_LABELS keys {sorted(label_keys)} != ALL_STEPS {sorted(ALL_STEPS)}"
    )


def test_every_dynamically_imported_step_has_a_module_file():
    """The wizard renders EVERY step via `await import("./steps/<step>.js")`
    (onboarding.js renderStep) — there is no inline/special-cased step (welcome
    and done both ship as modules). A step listed in STEPS without a matching
    Portal/onboarding/steps/<step>.js module is a dead-end: the import throws and
    the user is stuck on a "coming soon" placeholder with no way to advance.
    This is exactly how the `image` step shipped broken (C1). Guard it: assert a
    module file exists for every step the wizard imports.
    """
    missing = [s for s in _frontend_steps() if not (STEPS_DIR / f"{s}.js").is_file()]
    assert not missing, (
        "Onboarding steps listed in onboarding.js STEPS have NO module under "
        f"Portal/onboarding/steps/: {missing}. The wizard import() will throw and "
        "the user gets stuck on a placeholder with no next/skip. Create the "
        "missing steps/<step>.js module(s)."
    )


def test_status_rollup_sections_match_all_steps_minus_welcome_done():
    """The hub status rollup (Orchestrator/onboarding/status_rollup.py SECTIONS)
    enumerates exactly ALL_STEPS minus welcome/done, in order. Add/move/remove a
    step on either side without the other and this fails — the rollup's section
    list can never silently drift from the canonical step list."""
    from Orchestrator.onboarding.status_rollup import SECTIONS
    section_keys = [s["key"] for s in SECTIONS]
    expected = [s for s in ALL_STEPS if s not in ("welcome", "done")]
    assert section_keys == expected, (
        "status_rollup.SECTIONS drifted from state.ALL_STEPS:\n"
        f"  sections: {section_keys}\n"
        f"  expected: {expected}"
    )


def test_status_rollup_every_section_step_equals_key():
    """Each section's `step` must equal its `key` (the hub links ?step=<key>)."""
    from Orchestrator.onboarding.status_rollup import SECTIONS
    for s in SECTIONS:
        assert s["step"] == s["key"], f"section {s['key']!r}: step != key"
