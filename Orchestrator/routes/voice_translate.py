"""
Translation voice mode — shared helpers (P6a, design doc
docs/plans/2026-07-11-voice-agent-upgrade-pass-design.md workstream 5).

/ws/realtime and /ws/gemini-live both accept mode=translate +
target_language=<BCP-47>. This module owns validation and the minimal
translation prompt. Grok has NO translate model — grok_live_routes must
not import this.

UI pickers hardcode a top-20 list + free-text "Other"; the backend
therefore validates SHAPE only (any well-formed BCP-47 tag passes),
never membership.
"""
import re
from typing import Optional, Tuple

TRANSLATE_MODE = "translate"

DEFAULT_TARGET_LANGUAGE = "en"

# Language-tag shape check: primary subtag (2-3 letters) + optional subtags.
# Deliberately permissive — membership is a UI concern.
_BCP47_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


def resolve_translate_params(
    mode: Optional[str],
    target_language: Optional[str],
    log_prefix: str = "[VOICE-TRANSLATE]",
) -> Tuple[bool, str]:
    """Validate client-supplied mode + target_language.

    Returns (is_translate, resolved_target_language). Any mode other than
    "translate" (incl. None/"") -> (False, DEFAULT_TARGET_LANGUAGE).
    Malformed/missing target_language under translate mode -> warn +
    DEFAULT_TARGET_LANGUAGE. Never raises — a voice session must not die
    on bad client input (matches the route-wide allowlist-warn pattern).
    """
    if mode != TRANSLATE_MODE:
        return False, DEFAULT_TARGET_LANGUAGE
    lang = (target_language or "").strip()
    if not lang or not _BCP47_RE.match(lang):
        print(f"{log_prefix} WARNING: target_language {target_language!r} is not "
              f"a valid BCP-47 tag; falling back to {DEFAULT_TARGET_LANGUAGE!r}")
        return True, DEFAULT_TARGET_LANGUAGE
    return True, lang


def build_translate_instructions(target_language: str) -> str:
    """Minimal system prompt for translation sessions.

    Deliberately tiny: no persona, no tool guidance, no snapshot context —
    the entire point of translate mode is fastest-possible session setup.
    """
    return (
        f"You are a real-time speech interpreter. Translate everything you "
        f"hear into the language with BCP-47 tag '{target_language}'. "
        f"Speak ONLY the translation — no commentary, no answers, no "
        f"questions, no explanations. Preserve the speaker's tone, register, "
        f"and intent. If an utterance is already in '{target_language}', "
        f"repeat it verbatim."
    )
