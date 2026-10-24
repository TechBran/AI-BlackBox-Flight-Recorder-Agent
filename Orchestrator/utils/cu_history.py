"""History hygiene shared by every computer-use driver.

One policy for the two shapes a CU conversation comes in:

  * Anthropic messages (dicts): assistant `tool_use` blocks answered by
    `tool_result` blocks in the next user message — the browser session's
    history and the frontier AnthropicDriver.
  * google-genai `Content` objects: model `function_call` parts answered by
    `function_response` parts in the next user content — the Gemini CU session
    and the frontier GeminiMobileDriver.

Two jobs:

  * SCREENSHOT BUDGET — a CU loop re-sends its whole history every step and
    appends one screenshot per step, so an unbudgeted run dies on the provider's
    request caps (Anthropic 413 ~100 images / ~32MB; Gemini's 131,072-token
    input window at ~step 20). Only the newest few images are kept; older ones
    become a one-line placeholder in place, so pairing is untouched.
  * TRIM — a persistent session caps its history length, and the cut must fall
    between whole tool rounds: a slice that opens on an orphaned tool_result, or
    leaves a tool_use unanswered, is rejected outright (audit
    O13-computer-use-05: the old head+tail slice 400'd every session past 200
    messages).

Editing an earlier turn invalidates every later signed thinking block on the
models that bind thinking to the conversation prefix (Claude Opus/Sonnet 5.5,
Fable 5.1). So whenever a budget or trim actually changes earlier turns, the
thinking blocks are stripped from the Anthropic history — the documented
recovery that keeps the request valid on every model — and an untouched
history keeps them verbatim.

Dependency-light on purpose: no Orchestrator.config import, google.genai only
imported when a genai image is replaced.
"""
from __future__ import annotations

from typing import Any, List

ANTHROPIC_ELIDED_SCREENSHOT = ("[Earlier screenshot elided to keep the request under "
                               "the API size caps]")
GENAI_ELIDED_SCREENSHOT = "[earlier screenshot elided to fit the input window]"
TRIMMED_NOTE = ("[Earlier steps of this conversation were trimmed to fit the context "
                "window; continue from the current screen.]")
UNANSWERED_RESULT = "Not executed: the run ended before this action's result was recorded."

_THINKING_TYPES = ("thinking", "redacted_thinking")


# ── Anthropic message shape ─────────────────────────────────────────────────

def _blocks(msg: dict) -> list:
    content = msg.get("content")
    return content if isinstance(content, list) else []


def _tool_use_ids(msg: dict) -> List[str]:
    if msg.get("role") != "assistant":
        return []
    return [b.get("id", "") for b in _blocks(msg) if b.get("type") == "tool_use"]


def _has_tool_results(msg: dict) -> bool:
    return any(b.get("type") == "tool_result" for b in _blocks(msg))


def _is_turn_start(msg: dict) -> bool:
    """A user message that opens a new turn: a prompt, not a tool round's answer."""
    return msg.get("role") == "user" and not _has_tool_results(msg)


def budget_screenshots_in_history(history: list, keep_images: int = 3) -> list:
    """Return a copy of an Anthropic-format history with only the most recent
    ``keep_images`` image blocks intact; every older screenshot becomes a
    one-line text placeholder in the same position (tool_use/tool_result
    pairing preserved). Non-mutating and idempotent; returns ``history`` itself
    when nothing needs eliding.

    THE 413 guard (2026-07-23): the CU send loop re-sends the whole history
    every iteration, so without a per-turn budget a long run accumulates up to
    CU_MAX_ITERATIONS full-res PNGs and dies on Anthropic's per-request caps
    (~100 images / ~32MB — surfaced as "request_too_large" at step ~10 on a
    native 3440x1440 capture). Call this at the top of EVERY send.
    """
    def _count(msgs) -> int:
        n = 0
        for msg in msgs:
            for block in _blocks(msg):
                if block.get("type") == "image":
                    n += 1
                elif (block.get("type") == "tool_result"
                      and isinstance(block.get("content"), list)):
                    n += sum(1 for item in block["content"]
                             if item.get("type") == "image")
        return n

    total = _count(history)
    if total <= keep_images:
        return history

    cutoff = total - keep_images  # image ordinals 1..cutoff get elided
    seen = 0
    out = []
    for msg in history:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        new_content = []
        changed = False
        for block in content:
            if block.get("type") == "image":
                seen += 1
                if seen <= cutoff:
                    new_content.append({"type": "text", "text": ANTHROPIC_ELIDED_SCREENSHOT})
                    changed = True
                else:
                    new_content.append(block)
            elif (block.get("type") == "tool_result"
                  and isinstance(block.get("content"), list)):
                inner_new = []
                inner_changed = False
                for item in block["content"]:
                    if item.get("type") == "image":
                        seen += 1
                        if seen <= cutoff:
                            inner_new.append({"type": "text",
                                              "text": ANTHROPIC_ELIDED_SCREENSHOT})
                            inner_changed = True
                        else:
                            inner_new.append(item)
                    else:
                        inner_new.append(item)
                if inner_changed:
                    new_content.append({**block, "content": inner_new})
                    changed = True
                else:
                    new_content.append(block)
            else:
                new_content.append(block)
        out.append({**msg, "content": new_content} if changed else msg)
    return out


def strip_thinking_blocks(history: list) -> list:
    """Drop every thinking / redacted_thinking block (text and tool_use stay).
    An assistant message left with nothing is dropped. Returns ``history``
    itself when it carries no thinking."""
    if not any(b.get("type") in _THINKING_TYPES for m in history for b in _blocks(m)):
        return history
    out = []
    for msg in history:
        blocks = _blocks(msg)
        if not any(b.get("type") in _THINKING_TYPES for b in blocks):
            out.append(msg)
            continue
        kept = [b for b in blocks if b.get("type") not in _THINKING_TYPES]
        if kept:
            out.append({**msg, "content": kept})
    return out


def budget_anthropic_request(history: list, keep_images: int = 3) -> list:
    """The per-request budget for a driver that replays signed thinking: elide
    old screenshots and, only when that changed an earlier turn, strip the
    thinking blocks the edit invalidated. Returns ``history`` itself when no
    image needed eliding."""
    budgeted = budget_screenshots_in_history(history, keep_images=keep_images)
    if budgeted is history:
        return history
    return strip_thinking_blocks(budgeted)


def repair_anthropic_pairing(history: list) -> list:
    """Make every tool_result answer a tool_use in the assistant message right
    before it, and every tool_use get an answer in the user message right after.

    Orphaned results are dropped (and a user message left empty with them); an
    unanswered tool_use gets an explicit error result (echoing toolset_name, as
    the computer toolset requires), inserted ahead of any other content in the
    answering message. Returns ``history`` itself when it is already valid."""
    out: list = []
    changed = False
    for msg in history:
        if msg.get("role") == "user" and _has_tool_results(msg):
            prev_ids = set(_tool_use_ids(out[-1])) if out else set()
            kept = [b for b in _blocks(msg)
                    if b.get("type") != "tool_result" or b.get("tool_use_id") in prev_ids]
            if len(kept) != len(_blocks(msg)):
                changed = True
                if not kept:
                    continue
                msg = {**msg, "content": kept}
        out.append(msg)

    final: list = []
    for idx, msg in enumerate(out):
        final.append(msg)
        uses = [b for b in _blocks(msg) if b.get("type") == "tool_use"] \
            if msg.get("role") == "assistant" else []
        if not uses:
            continue
        nxt = out[idx + 1] if idx + 1 < len(out) else None
        answered = {b.get("tool_use_id") for b in _blocks(nxt)} if (
            nxt is not None and nxt.get("role") == "user") else set()
        missing = []
        for use in uses:
            if use.get("id") in answered:
                continue
            result = {"type": "tool_result", "tool_use_id": use.get("id", ""),
                      "content": UNANSWERED_RESULT, "is_error": True}
            if use.get("toolset_name"):
                result["toolset_name"] = use["toolset_name"]
            missing.append(result)
        if not missing:
            continue
        changed = True
        if nxt is not None and nxt.get("role") == "user":
            blocks = _blocks(nxt)
            results = [b for b in blocks if b.get("type") == "tool_result"]
            rest = [b for b in blocks if b.get("type") != "tool_result"]
            out[idx + 1] = {**nxt, "content": results + missing + rest}
        else:
            final.append({"role": "user", "content": missing})
    return final if changed else history


def trim_anthropic_history(history: list, max_messages: int = 200) -> list:
    """Cap an Anthropic-format CU history at ``max_messages`` without ever
    splitting a tool round.

    Whole oldest turns go first (a turn opens on a user prompt, not on a
    tool_result). When the newest turn alone is longer than the cap, it keeps
    its opening prompt (with a one-line trimmed note) followed by the newest
    whole (assistant tool_use, user tool_result) rounds that fit. The result is
    then pairing-checked, and its thinking blocks stripped, because they were
    signed over the prefix this trim just removed. Returns ``history`` itself
    when it already fits.
    """
    if len(history) <= max_messages:
        return history
    max_messages = max(3, max_messages)
    starts = [i for i, m in enumerate(history) if _is_turn_start(m)]
    out = None
    for i in starts:
        if len(history) - i <= max_messages:
            out = history[i:]
            break
    if out is None:
        # The newest turn is longer than the cap on its own.
        last_start = starts[-1] if starts else None
        prompt = history[last_start] if last_start is not None else None
        floor = (last_start + 1) if last_start is not None else 0
        # Earliest assistant message whose suffix (plus the kept prompt) fits.
        j = next((k for k in range(max(floor, len(history) - (max_messages - 1)), len(history))
                  if history[k].get("role") == "assistant"), None)
        tail = history[j:] if j is not None else []
        note = {"type": "text", "text": TRIMMED_NOTE}
        if prompt is None:
            prompt = {"role": "user", "content": [note]}
        elif isinstance(prompt.get("content"), list):
            prompt = {**prompt, "content": prompt["content"] + [note]}
        else:
            prompt = {**prompt, "content": [
                {"type": "text", "text": str(prompt.get("content", ""))}, note]}
        out = [prompt] + tail
    return strip_thinking_blocks(repair_anthropic_pairing(out))


# ── google-genai Content shape ──────────────────────────────────────────────

def _parts(content: Any) -> list:
    return list(getattr(content, "parts", None) or [])


def _is_genai_image(part: Any) -> bool:
    inline = getattr(part, "inline_data", None)
    return inline is not None and (getattr(inline, "mime_type", "") or "").startswith("image/")


def _genai_calls(content: Any) -> int:
    return sum(1 for p in _parts(content) if getattr(p, "function_call", None))


def _genai_responses(content: Any) -> int:
    return sum(1 for p in _parts(content) if getattr(p, "function_response", None))


def budget_genai_images(contents: list, keep_images: int = 3) -> None:
    """Replace all but the most recent ``keep_images`` inline images in the
    request contents with small text parts, IN PLACE (function_call /
    function_response pairing untouched).

    The gemini twin of budget_screenshots_in_history (Anthropic 413 fix): the
    loop re-sends the whole contents list every step and appends one full-res
    screenshot per step, so an unbudgeted run blows Gemini's 131,072-token
    input cap at ~step 20 (Brandon's MS02 chess game died exactly this way,
    2026-07-23). Idempotent — elided parts are plain text on later passes.
    """
    total = sum(1 for c in contents for p in _parts(c) if _is_genai_image(p))
    if total <= keep_images:
        return
    from google.genai import types
    cutoff = total - keep_images
    seen = 0
    for c in contents:
        parts = c.parts or []
        for idx, p in enumerate(parts):
            if _is_genai_image(p):
                seen += 1
                if seen <= cutoff:
                    parts[idx] = types.Part.from_text(text=GENAI_ELIDED_SCREENSHOT)


def _genai_turn_start(content: Any) -> bool:
    return getattr(content, "role", None) == "user" and _genai_responses(content) == 0


def trim_genai_history(contents: list, max_messages: int = 200) -> list:
    """The genai twin of trim_anthropic_history: drop whole oldest turns, and
    when one turn is longer than the cap keep its opening prompt plus the newest
    whole (model function_call, user function_response) rounds. A user content
    whose function responses no longer follow their calls is dropped. Returns
    ``contents`` itself when it already fits."""
    if len(contents) <= max_messages:
        return contents
    max_messages = max(3, max_messages)
    starts = [i for i, c in enumerate(contents) if _genai_turn_start(c)]
    out = None
    for i in starts:
        if len(contents) - i <= max_messages:
            out = contents[i:]
            break
    if out is None:
        last_start = starts[-1] if starts else None
        floor = (last_start + 1) if last_start is not None else 0
        j = next((k for k in range(max(floor, len(contents) - (max_messages - 1)), len(contents))
                  if getattr(contents[k], "role", None) == "model"), None)
        tail = contents[j:] if j is not None else []
        if last_start is not None:
            out = [contents[last_start]] + tail
        else:
            from google.genai import types
            out = [types.Content(role="user", parts=[types.Part.from_text(text=TRIMMED_NOTE)])] + tail
    repaired: list = []
    for c in out:
        if _genai_responses(c):
            prev = repaired[-1] if repaired else None
            if (prev is None or getattr(prev, "role", None) != "model"
                    or _genai_calls(prev) != _genai_responses(c)):
                continue
        repaired.append(c)
    # A trailing model turn whose calls were never answered cannot be replayed.
    while repaired and getattr(repaired[-1], "role", None) == "model" and _genai_calls(repaired[-1]):
        repaired.pop()
    return repaired
