"""Onboarding registration tests for the new "embeddings" wizard step (Task 13).

The step must be a first-class member of ALL_STEPS (so /onboarding/step/complete
and /step/skip don't 500 with a ValueError) and must sit immediately after
api_keys — matching the frontend STEPS order (guarded separately by
test_onboarding_steps_parity.py).

Mirrors the structure of test_stt_onboarding_step.py (the transcription step's
registration tests).
"""
import pytest

from Orchestrator.onboarding import state as st


def _hermetic_state(tmp_path, monkeypatch) -> st.OnboardingState:
    """Fresh OnboardingState bound to tmp paths — never touches the real
    .onboarding_state.json / .onboarding_complete files."""
    monkeypatch.setattr(st, "STATE_FILE", tmp_path / ".onboarding_state.json")
    monkeypatch.setattr(st, "COMPLETE_SENTINEL", tmp_path / ".onboarding_complete")
    return st.OnboardingState()


def test_embeddings_in_all_steps_after_api_keys():
    assert "embeddings" in st.ALL_STEPS
    i = st.ALL_STEPS.index("embeddings")
    assert st.ALL_STEPS[i - 1] == "api_keys"
    assert st.ALL_STEPS[i + 1] == "optional_integrations"


def test_step_complete_and_skip_accept_embeddings(tmp_path, monkeypatch):
    """Mutating methods validate `step in ALL_STEPS` and raise ValueError
    otherwise — clean calls here prove the step is registered."""
    s = _hermetic_state(tmp_path, monkeypatch)

    s.set_current("embeddings")
    s.mark_step_complete("embeddings")
    s.mark_step_skipped("embeddings")

    snap = s.snapshot()
    assert "embeddings" in snap["all_steps"]
    assert "embeddings" in snap["skipped_steps"]
    assert "embeddings" not in snap["completed_steps"]  # skip removed it


def test_advance_from_api_keys_lands_on_embeddings(tmp_path, monkeypatch):
    """The route-level auto-advance (POST /onboarding/step/complete) must move
    api_keys → embeddings, and completing embeddings must advance past it."""
    from Orchestrator.routes import onboarding_routes as ob

    s = _hermetic_state(tmp_path, monkeypatch)
    monkeypatch.setattr(ob, "_state", s)

    s.set_current("api_keys")
    s.mark_step_complete("api_keys")
    ob._advance_current_to_next("api_keys")
    assert s.snapshot()["current_step"] == "embeddings"

    # complete advances past embeddings...
    s.mark_step_complete("embeddings")
    ob._advance_current_to_next("embeddings")
    assert s.snapshot()["current_step"] == "optional_integrations"

    # ...and skip does too (skipping = keep current active model, never blocks)
    s.set_current("embeddings")
    s.mark_step_skipped("embeddings")
    ob._advance_current_to_next("embeddings")
    assert s.snapshot()["current_step"] == "optional_integrations"
