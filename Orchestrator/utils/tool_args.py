"""Numeric tool arguments as models send them (audit rank 74, O03-chat-routes-B-07).

Models stringify numbers: a tool call's limit arrives as "5", " 5 " or 5.0 as
often as 5, and min("5", 50), range("2") or len(text) > "5000" raise TypeError
deep inside a helper, which ends the whole chat turn (or Computer Use run) that
called it. Every helper that takes a model-supplied number reads it through
int_value, so each caller (the chat dispatcher, the Computer Use driver, the
live-voice routes, the ToolVault executors, MCP) is safe without its own guard.

Stdlib only, like utils.paths, so the lean MCP venv can import it.
"""
from __future__ import annotations

from typing import Optional


def int_value(raw, default: int, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
    """``raw`` as an int: an int, an integral float or a numeric string ("2",
    " 2 ", "2.0") all count, anything else (None, "", "two", 2.5, a bool, a
    list) is ``default``; then clamped to [lo, hi]."""
    value = default
    if isinstance(raw, bool):
        pass
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, float) and raw.is_integer():
        value = int(raw)
    elif isinstance(raw, str):
        try:
            number = float(raw.strip())
            if number.is_integer():
                value = int(number)
        except ValueError:
            pass
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value
