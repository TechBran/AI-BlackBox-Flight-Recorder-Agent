"""Shared fossil-context helpers for agent transports.

Consolidates duplicated retrieval + composition logic across:
  - /ws/agent           (Claude Code CLI WebSocket)        agent_routes.py
  - /ws/gemini-agent    (Gemini CLI WebSocket)             gemini_agent_routes.py
  - Gemini CU SSE loop  (computer-use agent)               gemini_cu/agent_loop.py

This module is pure data-massaging: no WebSocket, no SSE, no subprocesses.
Each caller keeps its own emission mechanism (`_safe_ws_send`, `send_json`,
`yield`) and decides whether to prepend (CLI prompt) or append (system slot).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from Orchestrator.config import current_default
from Orchestrator.context_builder import build_fossil_context

# Fence delimiters — single source of truth so all agent paths inject
# BlackBox fossil context with the same recognizable markers. A downstream
# parser that ever wants to strip these back out can rely on the constants.
FOSSIL_FENCE_OPEN = "=== BLACKBOX FOSSIL CONTEXT (auto-injected for this session) ==="
FOSSIL_FENCE_CLOSE = "=== END FOSSIL CONTEXT ==="

# Provenance dict has exactly these four keys; see Orchestrator/context_builder.py.
_PROVENANCE_KEYS = ("recent", "keyword", "semantic", "checkpoint")


def empty_provenance() -> Dict[str, List[str]]:
    """Return the normalized 4-key empty provenance dict."""
    return {k: [] for k in _PROVENANCE_KEYS}


def normalize_provenance(prov: Optional[Dict]) -> Dict[str, List[str]]:
    """Ensure provenance dict has all 4 keys, each a list.

    Defensive against partial returns from the retrieval layer (e.g. a
    future build_fossil_context that drops a key, or a mock that returns
    {}). Always returns fresh lists so callers can mutate safely.
    """
    if not prov:
        return empty_provenance()
    return {k: list(prov.get(k, []) or []) for k in _PROVENANCE_KEYS}


def resolve_operator(raw: Optional[str], log_prefix: str) -> str:
    """Strip + fallback to the live default operator with a loud warning if empty."""
    op = (raw or "").strip()
    if not op:
        default = current_default()
        print(f"{log_prefix} WARNING: empty operator, falling back to default operator '{default}'")
        return default
    return op


def retrieve_for_agent(
    user_text: str,
    operator: str,
    log_prefix: str,
) -> Tuple[str, Dict[str, List[str]]]:
    """Fetch fossil context for an agent session.

    Always returns a tuple with a normalized 4-key provenance, even on error
    — agent transports treat any retrieval failure as a degrade-to-empty
    (with warning) rather than fatal. The retrieval layer's own [CONTEXT]
    log prefix is set from `log_prefix` so each transport's logs are tagged.
    """
    try:
        fossil_text, prov = build_fossil_context(
            user_text=user_text or "",
            operator=operator,
            log_prefix=f"{log_prefix} [CONTEXT]",
        )
        return fossil_text, normalize_provenance(prov)
    except ValueError as e:
        # build_fossil_context only raises ValueError for empty operator —
        # callers should have run resolve_operator first, but defend anyway.
        print(f"{log_prefix} WARNING: build_fossil_context rejected input: {e}")
        return "", empty_provenance()
    except Exception as e:
        print(f"{log_prefix} WARNING: fossil retrieval failed: {e}")
        return "", empty_provenance()


def compose_with_fossils(fossil_text: str, user_prompt: str) -> str:
    """Prepend fossil context as a fenced block before the user's prompt.

    Used by CLI-subprocess agents (Claude, Gemini) that have no separate
    system-prompt slot. If fossil_text is empty, returns user_prompt unchanged.
    """
    if not fossil_text:
        return user_prompt
    return (
        f"{FOSSIL_FENCE_OPEN}\n"
        f"{fossil_text}\n"
        f"{FOSSIL_FENCE_CLOSE}\n\n"
        f"User request:\n{user_prompt}"
    )


def append_fossils_to_system(system_instruction: str, fossil_text: str) -> str:
    """Append fossil context to a system_instruction.

    Used by SDK-based agents (Gemini CU) that have a real system slot. If
    fossil_text is empty, returns the system_instruction unchanged.
    """
    if not fossil_text:
        return system_instruction
    return (
        f"{system_instruction}\n\n"
        f"{FOSSIL_FENCE_OPEN}\n"
        f"{fossil_text}\n"
        f"{FOSSIL_FENCE_CLOSE}"
    )
