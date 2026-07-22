"""Onboarding registration for the on-box 'local_models' wizard step (M8).

Mirrors test_stt_onboarding_step.py: the step must be a first-class member of
ALL_STEPS (so /onboarding/step/complete|skip don't 500 with a ValueError) and
must sit immediately after 'embeddings' — matching the frontend STEPS order.
Hermetic: OnboardingState persists to a module-level STATE_FILE we redirect to
a tmp file, so the real .onboarding_state.json is never touched.
"""
import pytest

from Orchestrator.onboarding import state as st


def test_local_models_in_all_steps_after_embeddings():
    assert "local_models" in st.ALL_STEPS
    i = st.ALL_STEPS.index("local_models")
    assert st.ALL_STEPS[i - 1] == "embeddings"
    assert st.ALL_STEPS[i + 1] == "optional_integrations"


def test_step_complete_accepts_local_models(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "STATE_FILE", tmp_path / ".onboarding_state.json")
    monkeypatch.setattr(st, "COMPLETE_SENTINEL", tmp_path / ".onboarding_complete")
    s = st.OnboardingState()
    # None of these validate-gated calls may raise for the new step.
    s.set_current("local_models")
    s.mark_step_complete("local_models")
    s.mark_step_skipped("local_models")
    snap = s.snapshot()
    assert "local_models" in snap["all_steps"]
    assert "local_models" in snap["skipped_steps"]
    assert "local_models" not in snap["completed_steps"]  # skip removed it


def test_unknown_step_still_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "STATE_FILE", tmp_path / ".onboarding_state.json")
    monkeypatch.setattr(st, "COMPLETE_SENTINEL", tmp_path / ".onboarding_complete")
    s = st.OnboardingState()
    with pytest.raises(ValueError):
        s.mark_step_complete("not_a_real_step")
