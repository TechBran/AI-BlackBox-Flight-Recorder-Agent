"""Bridge the local_models wizard-step behavior test into the pytest suite.

Task 8.6 review item 5: the backend endpoints had tests, but nothing exercised
the wizard step's decision/formatting logic against a realistic
GET /local-models/status payload — which is exactly how the isActive() TypeError
(routing[cap] is an object, not a string) reached production. There is no
browser JS test infra and jsdom is not installed, so the real test is a DOM-free
`node --test` file (Portal/onboarding/steps/local_models.render.test.mjs). This
wrapper runs it under `python -m pytest` so the milestone's done-check covers it,
and skips cleanly on a box without a `node` binary (fail-open, house rule)."""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STEP_TEST = REPO_ROOT / "Portal" / "onboarding" / "steps" / "local_models.render.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_local_models_step_behavior_node():
    assert STEP_TEST.is_file(), f"missing step behavior test: {STEP_TEST}"
    proc = subprocess.run(
        ["node", "--test", str(STEP_TEST)],
        capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, (
        "local_models wizard-step behavior test failed — the step has drifted "
        "from the GET /local-models/status contract (routing[cap] object / "
        "status.hardware / disk.free_mb / models[].model+download).\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
