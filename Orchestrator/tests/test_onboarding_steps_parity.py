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
