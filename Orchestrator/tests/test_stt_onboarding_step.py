"""Onboarding registration tests for the new STT 'transcription' wizard step.

The step must be a first-class member of ALL_STEPS (so /onboarding/step/complete
and /step/skip don't 500 with a ValueError) and must sit between
optional_integrations and pair_phone — matching the frontend STEPS order.
"""
import pytest

from Orchestrator.onboarding import state as st


def test_transcription_in_all_steps():
    assert "transcription" in st.ALL_STEPS
    # positioned between optional_integrations and pair_phone
    i = st.ALL_STEPS.index("transcription")
    assert st.ALL_STEPS[i - 1] == "optional_integrations"
    assert st.ALL_STEPS[i + 1] == "pair_phone"


def test_step_complete_accepts_transcription(tmp_path, monkeypatch):
    """record completion for the new step must NOT raise.

    OnboardingState persists to a module-level STATE_FILE path. We redirect that
    to a tmp file so the test is hermetic and doesn't touch the real
    .onboarding_state.json. The mutating methods (mark_step_complete /
    mark_step_skipped / set_current) all validate `step in ALL_STEPS` and raise
    ValueError otherwise — so a clean call here proves the step is registered.
    """
    monkeypatch.setattr(st, "STATE_FILE", tmp_path / ".onboarding_state.json")
    monkeypatch.setattr(st, "COMPLETE_SENTINEL", tmp_path / ".onboarding_complete")

    s = st.OnboardingState()  # fresh instance bound to the tmp paths

    # None of these should raise for the new step.
    s.set_current("transcription")
    s.mark_step_complete("transcription")
    s.mark_step_skipped("transcription")

    snap = s.snapshot()
    assert "transcription" in snap["all_steps"]
    assert "transcription" in snap["skipped_steps"]
    assert "transcription" not in snap["completed_steps"]  # skip removed it


def test_unknown_step_still_rejected(tmp_path, monkeypatch):
    """Guard against the validation being accidentally loosened to a no-op."""
    monkeypatch.setattr(st, "STATE_FILE", tmp_path / ".onboarding_state.json")
    monkeypatch.setattr(st, "COMPLETE_SENTINEL", tmp_path / ".onboarding_complete")
    s = st.OnboardingState()
    with pytest.raises(ValueError):
        s.mark_step_complete("not_a_real_step")
