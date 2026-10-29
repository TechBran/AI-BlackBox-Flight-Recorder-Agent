"""The provider-loop golden contract (audit rank 74, M6 U38).

Each golden/providers/*.json file is one recorded scenario: a provider (OpenAI,
Anthropic, Gemini, xAI, a custom llama-swap server), a path (POST /chat/stream
or the non-stream call_<provider> twin) and a case (text with reasoning, one
parallel round of every tool branch, attachments, an HTTP error, a billing
refusal, the tool-round cap, and the provider's own retry/refusal/truncation
cases). The test replays the recorded inputs through the current code and
asserts the result is identical to the recording: the client-facing SSE byte
for byte, and the upstream request bodies, tool executor calls, created tasks
and native-branch helper calls value for value.

These are the contract the provider refactor is held to: they passed on the
code before the loops were collapsed and must pass unchanged after. A change
that alters any of them on purpose regenerates the files (see the harness
docstring in provider_goldens.py) so the diff shows the behaviour change.
"""
import os

import pytest

from Orchestrator.tests import provider_goldens as pg

_SPECS = {s["name"]: s for s in pg.scenario_specs()}


def test_every_scenario_has_a_golden_and_no_golden_is_orphaned():
    on_disk = {p.stem for p in pg.GOLDEN_DIR.glob("*.json")}
    if os.environ.get(pg.REGEN_ENV):
        return
    assert on_disk == set(_SPECS), (
        f"missing goldens: {sorted(set(_SPECS) - on_disk)}; "
        f"orphaned goldens: {sorted(on_disk - set(_SPECS))}")


def test_recorded_inputs_match_the_scenario_builder():
    """The recorded inputs are what the builder produces (so a regenerated
    golden and a replayed one describe the same scenario)."""
    if os.environ.get(pg.REGEN_ENV):
        return
    for name, spec in _SPECS.items():
        assert pg.read_golden(pg.golden_path(name))["scenario"] == spec, name


@pytest.mark.parametrize("name", sorted(_SPECS))
def test_provider_golden(name, monkeypatch, tmp_path):
    spec = _SPECS[name]
    if os.environ.get(pg.REGEN_ENV):
        pg.write_golden(spec, pg.run(spec, monkeypatch, tmp_path), os.environ[pg.REGEN_ENV])
        return
    recorded = pg.read_golden(pg.golden_path(name))
    observed = pg.run(recorded["scenario"], monkeypatch, tmp_path)
    expected = recorded["observed"]
    if "sse" in expected:
        # Byte-identical client-facing stream.
        assert observed["sse"] == expected["sse"]
    for key in expected:
        assert observed.get(key) == expected[key], f"{name}: {key} differs"
    assert set(observed) == set(expected)
