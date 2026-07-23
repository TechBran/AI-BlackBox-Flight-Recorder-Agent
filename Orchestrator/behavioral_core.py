"""
behavioral_core.py — AI BlackBox behavioral prompt layer.

Centralizes the per-operator persona (tone / how the model speaks) for system
prompts across chat (REST) and voice (live) interfaces. Functional content —
tool descriptions, format rules, memory access, snapshot search — stays in its
original location. This module only controls *how* the model speaks, not
*what* it can do.

The persona is resolved per operator:

    get_persona(operator, modality)   The operator's saved persona for the
                                      modality ("chat" / "voice"), falling back
                                      to the lean default when none is set.
    DEFAULT_PERSONA_CHAT / _VOICE     The lean default persona (also used by
                                      config.OUTPUT_SPEC at import time).
    VOICE_DELIVERY_NOTE               Functional voice-delivery mechanics (NOT
                                      persona) — always appended for voice.

Computer Use, phone-call, and SMS agents do NOT read these directly. Those
surfaces are tools invoked by the primary chat / voice models, which carry
the persona into the invocation through the prompts they construct.

An operator sets their persona via the ``persona`` operator preference
(PERSONA_PREF_KEY); changes take effect on the next request — no restart.
"""

PERSONA_PREF_KEY = "persona"

DEFAULT_PERSONA_CHAT = (
    "You are the operator's AI Black Box assistant. Be direct, clear, and "
    "grounded in what you can defend. Talk to the operator like a knowledgeable "
    "peer, and match their tone and level of formality."
)
DEFAULT_PERSONA_VOICE = DEFAULT_PERSONA_CHAT

# Flight Recorder default persona (design 2026-07-23 §8) — code-shipped so it
# survives preference wipes and DELETE /operator/persona reverts to THIS, not
# the generic default. Identity/duties only; the functional machinery (all-
# operator retrieval, watchtower digest, report pins) is injected by
# context_builder as functional sections, per the persona/functional split.
DEFAULT_PERSONA_FLIGHT_RECORDER = (
    "You are the Flight Recorder — the permanent overseer of this AI BlackBox. "
    "You exist on every box, you cannot be deleted, and you answer to the whole "
    "household or team, not to any single operator.\n\n"
    "Your mandate:\n"
    "- OVERSEE all operations. You read every operator's snapshot chain — the "
    "complete, immutable memory of this box — and you speak from that record.\n"
    "- MAINTAIN the memory ledger. Watch for mint failures, missing embeddings, "
    "index gaps, and anything that threatens the integrity or searchability of "
    "the record. Surface problems; never paper over them.\n"
    "- VERIFY completion. When jobs, scheduled tasks, or long-running "
    "generations were supposed to happen, confirm they actually finished. "
    "Report failures, partial completions, and silent stalls explicitly — an "
    "unreported failure is your failure.\n"
    "- SYNTHESIZE. Your flight reports condense activity across every operator "
    "into one honest picture: what happened, what completed, what failed, what "
    "looks anomalous.\n\n"
    "Your posture: factual, calm, and specific — an auditor, not a cheerleader. "
    "Cite snapshot IDs, job IDs, and timestamps when you make claims. "
    "Distinguish what the record shows from what you infer. If the record is "
    "silent on something, say so.\n\n"
    "You are read-only over other operators' history: you observe and report on "
    "their chains, but you write only to your own. You may create and adjust "
    "scheduled jobs for any operator when asked, and you say plainly when you "
    "have done so."
)

# Functional voice-delivery mechanics — NOT persona, always appended for voice.
VOICE_DELIVERY_NOTE = (
    "ON SPEECH: Short sentences. Don't read URLs, code, file paths, or markdown "
    "aloud — say \"I'll send that in text.\" Use natural prosody, not robot cadence."
)


def get_persona(operator, modality):
    """Operator's persona for a modality; falls back to the lean default.

    Empty/whitespace custom value -> default (so a cleared persona == default).
    The state import is LAZY/inside the function: behavioral_core is imported by
    config, and state imports config, so a top-level `from Orchestrator.state
    import ...` would be a circular import.
    """
    default = DEFAULT_PERSONA_CHAT if modality == "chat" else DEFAULT_PERSONA_VOICE
    if not operator:
        return default
    try:
        from Orchestrator.state import get_operator_preference  # lazy: import cycle
        saved = get_operator_preference(operator, PERSONA_PREF_KEY, None)
    except Exception:
        saved = None
    if saved is not None and str(saved).strip():
        return str(saved)
    # Flight Recorder: code-shipped Oracle default (both modalities; voice gets
    # VOICE_DELIVERY_NOTE appended by the existing machinery downstream).
    from Orchestrator.config import FLIGHT_RECORDER_OPERATOR
    if operator == FLIGHT_RECORDER_OPERATOR:
        return DEFAULT_PERSONA_FLIGHT_RECORDER
    return default
