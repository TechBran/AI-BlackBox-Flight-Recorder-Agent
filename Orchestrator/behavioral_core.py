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
    return default
