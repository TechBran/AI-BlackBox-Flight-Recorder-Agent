"""Anthropic capability sets must track new model families (model-rot guard).

2026-06-11: claude-fable-5 returned 400 "`temperature` is deprecated for this
model" in chat because ANTHROPIC_NO_SAMPLING_MODELS predated the Claude 5
tier. Fable/Mythos 5 share the Opus 4.7+ request surface: sampling params
removed, adaptive-only thinking, display="summarized" needed for visible
thinking text. When Anthropic ships a new family, extend the sets in
Orchestrator/config.py and these assertions together.
"""
from Orchestrator.config import (
    ANTHROPIC_EFFORT_MAP,
    ANTHROPIC_NO_SAMPLING_MODELS,
    ANTHROPIC_THINKING_DISPLAY_MODELS,
    ANTHROPIC_THINKING_MODELS,
)


def test_claude5_tier_in_every_capability_set():
    for model in ("claude-fable-5", "claude-mythos-5"):
        assert model in ANTHROPIC_NO_SAMPLING_MODELS, (
            f"{model} accepts no sampling params — chat would 400 on temperature")
        assert model in ANTHROPIC_THINKING_MODELS
        assert model in ANTHROPIC_THINKING_DISPLAY_MODELS
        assert model in ANTHROPIC_EFFORT_MAP


def test_display_gated_models_also_reject_sampling():
    # The display="omitted"-by-default surface (4.7+) is the same surface that
    # removed temperature/top_p/top_k — these sets must move together.
    assert ANTHROPIC_THINKING_DISPLAY_MODELS <= ANTHROPIC_NO_SAMPLING_MODELS
