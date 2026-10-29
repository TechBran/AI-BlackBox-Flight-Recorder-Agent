"""Golden frame fixtures of the live-voice bridges (audit rank 74, X05-architecture-13).

Each scenario drives a bridge end to end against a scripted provider socket
(Orchestrator/tests/voice_golden_harness.py) and compares everything it says
(client frames, upstream frames per dial, transcript saves, tool executor
calls, created tasks) with the golden recorded under golden/voice/. The
goldens were recorded before the three routes and the phone bridge were moved
onto one state machine; they pin that the move changed nothing a client or a
provider can observe.

A mismatch prints a unified diff. When a wire change is deliberate, re-record
the scenario (python -m Orchestrator.tests.voice_golden_harness <name>) and
say why in the commit.
"""
import difflib
import json

import pytest

from Orchestrator.tests import voice_golden_harness as harness


def _lines(data) -> list:
    return json.dumps(data, indent=1, sort_keys=True).splitlines()


@pytest.mark.parametrize("name", sorted(harness.SCENARIOS))
def test_bridge_matches_golden(name):
    want = harness.load_golden(name)
    got = harness.run(name)
    if got != want:
        diff = "\n".join(list(difflib.unified_diff(_lines(want), _lines(got), "golden", "now",
                                                   n=3))[:200])
        pytest.fail(f"{name} drifted from its golden:\n{diff}", pytrace=False)


def test_every_golden_has_a_scenario():
    """A golden whose scenario was dropped would silently stop guarding."""
    files = {p.stem for p in harness.GOLDEN_DIR.glob("*.json")}
    assert files == set(harness.SCENARIOS)
