"""Pieces every provider adapter shares (audit rank 74).

ProviderEnv is what an adapter reads from routes/chat_routes on each call: the
provider key and URL, the tool lists, the upload loaders and the tool-context
builder. chat_routes builds it from its own module namespace per call, so a
test (or the onboarding wizard's key reload) that changes chat_routes.<name>
reaches the adapter exactly as it reached the inline loops.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional


@dataclass(frozen=True)
class ProviderEnv:
    """One provider call's view of chat_routes (read per call, never cached)."""
    api_key: str                              # the provider key ("" when unset)
    url: str                                  # endpoint (Gemini: the models base URL)
    get_tools: Callable                       # _get_tools(provider, prompt, group="chat")
    load_attachment: Callable                 # _load_attachment(url, exts, max_bytes=None)
    unreadable_parts: Callable                # _unreadable_attachment_parts(part, pdf_block=False)
    tool_context: Callable                    # _tool_context(operator, loop)
    inline_max_bytes: Optional[int] = None    # Gemini: GEMINI_INLINE_MAX_BYTES


# The part a user-attachment loader adds when reading an upload FAILED (its
# `except Exception` branch). These loaders used to only print, so an error
# there (W04: an UnboundLocalError on every non-stream turn) dropped every
# attachment and the model answered as if nothing had been sent.
_ATTACHMENT_ERROR_NOTE = "[Note: an attached file could not be loaded.]"

# The tool loops stop after this many rounds; the turn then ends with a note.
_TOOL_CAP_NOTE = ("(Stopped after {cap} rounds of tool calls without a final answer. "
                  "Ask me to continue if the task is not finished.)")


def _with_note(text: str, note: str) -> str:
    """``text`` with ``note`` appended as its own paragraph."""
    return f"{text}\n\n{note}" if text else note


def _tool_cap_events(content: str, reasoning_key: str, reasoning: str, cap: int) -> List[Dict]:
    """The content + done events that close a stream whose tool loop used all
    ``cap`` rounds: the turn still ends, with its partial text and a note."""
    note = _TOOL_CAP_NOTE.format(cap=cap)
    return [
        {"type": "content", "data": ("\n\n" if content else "") + note},
        {"type": "done", "data": {reasoning_key: reasoning, "content": _with_note(content, note)}},
    ]


class DeclinedReply(str):
    """A non-stream reply that is a provider's refusal or safety block, as a
    parenthesized notice. It is still the notice str, so the chat worker shows
    it unchanged; a caller that needs an answer (the Flight Recorder's provider
    chain, oversight._synthesize) checks for this type and tries its next
    provider instead of minting the notice as the report."""


def _last_user_msg(messages) -> str:
    """Extract text from the last user message (for ToolVault prompt injection).

    Returns the FULL last user message — NO char cap. Truncating to the first
    500 chars dropped trailing instructions (e.g. a cron job's appended
    ``DELIVERY: send an SMS to …`` line) before they reached the semantic tool
    embedder, so the matching tool (``send_sms``) scored below threshold and was
    never injected. The genuine ceiling is the embedding layer's token-aware
    clamp (per-model ``max_input_tokens`` budgets via ``tokenization.py``,
    enforced downstream in ``Orchestrator/embeddings/providers.py`` — clamps,
    never raises) — far above any realistic prompt; no app-level cap is needed
    here.
    """
    for m in reversed(messages if isinstance(messages, list) else []):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and "text" in p
                )
    return ""


def _usage_into(total: Dict, prompt_tokens: int, completion_tokens: int) -> None:
    """Add one round's tokens to a non-stream call's running usage."""
    total["prompt_tokens"] += prompt_tokens
    total["completion_tokens"] += completion_tokens
    total["total_tokens"] = total["prompt_tokens"] + total["completion_tokens"]
