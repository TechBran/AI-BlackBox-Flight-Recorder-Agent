#!/usr/bin/env python3
"""
ivr_prompts.py - IVR Menu Prompt Definitions

Defines text prompts for the IVR system that will be converted to speech
using the existing TTS infrastructure.
"""

from typing import Dict

# =============================================================================
# IVR Prompt Text Definitions
# =============================================================================

# Initial greeting (played once at call start, before PIN)
IVR_GREETING = "Thank you for calling AI BlackBox."

# PIN prompts (the entry prompt states the configured PIN's length)
IVR_PIN_ACCEPTED = "Access granted."
IVR_PIN_RETRY = "That code was incorrect. Please try again."
IVR_PIN_FAILED = "Too many incorrect attempts. Goodbye."
IVR_PIN_LOCKED = "Too many incorrect attempts. Please try again later. Goodbye."
IVR_PIN_UNAVAILABLE = "This line is not accepting calls right now. Goodbye."


def get_pin_prompt(length: int) -> str:
    """PIN entry prompt for a ``length``-digit PIN."""
    return f"Please enter your {length}-digit access code."

# Backend selection: main menu
IVR_WELCOME = (
    "Select your AI assistant. "
    "Press 1 for Claude. "
    "Press 2 for Gemini. "
    "Press 3 for GPT. "
    "Press 4 for Grok."
)

# Backend selection: retry after timeout
IVR_RETRY = (
    "I didn't catch that. "
    "Press 1 for Claude. "
    "Press 2 for Gemini. "
    "Press 3 for GPT. "
    "Press 4 for Grok."
)

# Invalid selection prompt
IVR_INVALID = "That's not a valid option. Press 1, 2, 3, or 4."

# Timeout prompt (defaulting to GPT)
IVR_TIMEOUT = "No selection made. Connecting you to the default assistant."

# Selection confirmation prompts
IVR_CONFIRM_CLAUDE = "Connecting you to Claude. One moment."
IVR_CONFIRM_GEMINI = "Connecting you to Gemini. One moment."
IVR_CONFIRM_GPT = "Connecting you to GPT. One moment."
IVR_CONFIRM_GROK = "Connecting you to Grok. One moment."

# Backend selection map
IVR_CONFIRMATION_MAP: Dict[int, str] = {
    1: IVR_CONFIRM_CLAUDE,
    2: IVR_CONFIRM_GEMINI,
    3: IVR_CONFIRM_GPT,
    4: IVR_CONFIRM_GROK,
}

# Error prompts
IVR_ERROR_CONNECTION = "I'm sorry, I couldn't connect to the AI assistant. Please try again later."
IVR_ERROR_GENERIC = "I'm sorry, something went wrong. Please try again."

# Goodbye prompt
IVR_GOODBYE = "Thank you for calling AI BlackBox. Goodbye."

# Hold prompts (for when bridging takes time)
IVR_HOLD_CONNECTING = "One moment while I connect you."
IVR_HOLD_TRANSFERRING = "Transferring your call now."

# Outbound call prompts
OUTBOUND_GREETING = "Hello, this is a call from AI BlackBox."
OUTBOUND_AI_INTRO = "I'm your AI assistant. How can I help you today?"

# =============================================================================
# TTS Voice Settings for IVR
# =============================================================================

# Default TTS voice for IVR prompts
IVR_TTS_VOICE = "onyx"  # Deep, authoritative OpenAI voice
IVR_TTS_MODEL = "tts-1-hd"  # High-quality model for IVR

# Backend-specific greeting voices
BACKEND_GREETING_VOICES: Dict[str, str] = {
    "claude_code": "onyx",
    "gemini_live": "Charon",  # Gemini native voice
    "openai_realtime": "ash",  # OpenAI Realtime voice
    "grok_live": "Ara",  # Grok native voice
}


def get_confirmation_prompt(selection: int) -> str:
    """Get the confirmation prompt for a given IVR selection."""
    return IVR_CONFIRMATION_MAP.get(selection, IVR_CONFIRM_GPT)


def get_backend_name(selection: int) -> str:
    """Get the human-readable backend name for a given IVR selection."""
    names = {
        1: "Claude Code",
        2: "Gemini",
        3: "GPT",
        4: "Grok",
    }
    return names.get(selection, "GPT")


def get_backend_id(selection: int) -> str:
    """Get the backend ID for a given IVR selection."""
    backends = {
        1: "claude_code",
        2: "gemini_live",
        3: "openai_realtime",
        4: "grok_live",
    }
    return backends.get(selection, "openai_realtime")


# =============================================================================
# Operator Selection IVR
# =============================================================================

def build_operator_selection_prompt(operators: list) -> str:
    """
    Build a dynamic operator selection prompt from the live operator list.

    Args:
        operators: List of operator names from config

    Returns:
        TTS prompt text listing all operators with their selection numbers
    """
    if not operators:
        return "No operators configured. Proceeding as guest."

    lines = ["Please identify yourself."]

    # Limit to 9 operators for single-digit selection
    for i, op in enumerate(operators[:9], 1):
        lines.append(f"Press {i} for {op}.")

    if len(operators) > 9:
        lines.append("Press 0 for other.")

    return "\n".join(lines)


def build_operator_retry_prompt(operators: list) -> str:
    """Build retry prompt for operator selection."""
    if not operators:
        return "Please try again."

    lines = ["I didn't catch that. Please select your identity."]
    for i, op in enumerate(operators[:9], 1):
        lines.append(f"Press {i} for {op}.")

    return "\n".join(lines)


def get_operator_confirmation(operator_name: str, backend_name: str) -> str:
    """Get confirmation prompt after operator selection."""
    return f"Welcome, {operator_name}. Connecting you to {backend_name}."


# Default operator prompts
IVR_OPERATOR_TIMEOUT = "No selection received. Proceeding as guest caller."
IVR_OPERATOR_INVALID = "That's not a valid selection. Please try again."
