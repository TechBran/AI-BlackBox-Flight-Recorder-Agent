"""Defensive unwrap for the legacy {"ui_reply", "snapshot_perspective"} envelope.

Phase 0 of the pure-production reply/snapshot parsing plan. Some providers (and
the non-stream worker) can leak a JSON reply envelope onto the text channel:
fenced (```json...```), triple-nested (ui_reply is itself an envelope string),
or malformed. This module unwraps it defensively at every snapshot WRITE site.

Hard guarantees:
  - Total function: never raises (risky parsing wrapped in try/except).
  - Never returns a parse-error sentinel ("(Could not parse...)" /
    "(Response was truncated...)"). On any failure it stores the reply AS-IS.
  - Stdlib-only (json, re). NO imports from chat_routes/tasks (avoid cycles).
"""

import json
import re

__all__ = ["unwrap_reply_envelope"]

# Cap recursion so a pathological self-referential input can't loop.
_MAX_DEPTH = 2

# Matches a whole-string code fence: a leading ```json / ``` line and a trailing
# ``` line, wrapping the ENTIRE (stripped) string. Inline fences are left alone.
_WHOLE_FENCE = re.compile(
    r"\A```[ \t]*[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)\r?\n?```\Z",
    re.DOTALL,
)


def _strip_whole_string_fence(text: str) -> str:
    """Strip a code fence ONLY when it wraps the entire string; else return as-is."""
    stripped = text.strip()
    m = _WHOLE_FENCE.match(stripped)
    if m:
        return m.group(1).strip()
    return text


def _extract_first_json_object(s: str):
    """Return the first brace-balanced top-level JSON object substring, or None.

    Tracks string literals + escapes so braces inside strings don't fool it.
    Self-contained (no cross-module dependency) to keep this module total.
    """
    if not isinstance(s, str):
        return None
    n = len(s)
    depth = 0
    start = -1
    in_str = False
    esc = False
    i = 0
    while i < n:
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start != -1:
                        return s[start:i + 1]
        i += 1
    return None


def _parse_object(text: str):
    """Try to parse a single top-level JSON object from text. Returns dict or None."""
    try:
        # Fast path: the whole (stripped) string is the object.
        data = json.loads(text.strip())
        if isinstance(data, dict):
            return data
    except (ValueError, TypeError):
        pass
    # Fallback: scan for the first brace-balanced object substring.
    candidate = _extract_first_json_object(text)
    if candidate is not None:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            pass
    return None


def unwrap_reply_envelope(text, _depth: int = 0):
    """Unwrap a leaked reply envelope, defensively.

    Returns (reply: str, perspective: str). Total function; never raises; never
    returns a parse-error sentinel. On any failure, returns (text, "") so the
    reply is stored AS-IS rather than poisoning memory.
    """
    # 1. Guard: not a non-empty str.
    if not isinstance(text, str) or not text:
        return (text if isinstance(text, str) else "") or "", ""

    try:
        # 2. Strip a whole-string code fence (inline fences left alone).
        unfenced = _strip_whole_string_fence(text)

        # 3. Locate + parse a single top-level JSON object.
        data = _parse_object(unfenced)

        # 4. Envelope shape: a dict with a "ui_reply" key.
        if isinstance(data, dict) and "ui_reply" in data:
            reply = str(data.get("ui_reply", "")).strip()
            perspective = str(data.get("snapshot_perspective", "")).strip()

            # Handle ONE level of nesting: ui_reply may itself be an envelope.
            if reply and _depth < _MAX_DEPTH:
                inner_reply, inner_persp = unwrap_reply_envelope(reply, _depth + 1)
                # Only adopt the inner result if it actually unwrapped to a
                # different (envelope) reply; otherwise keep this level's values.
                if inner_reply and inner_reply != reply:
                    reply = inner_reply
                    if inner_persp:
                        perspective = inner_persp

            # 4b. Empty reply -> fall back to original text (never lose content).
            if not reply:
                return text, perspective
            return reply, perspective
    except Exception:
        # 5. Any parse trouble -> store as-is. NEVER a sentinel.
        return text, ""

    # 5. No envelope / not a dict / no ui_reply -> store as-is.
    return text, ""
