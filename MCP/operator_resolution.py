"""Operator resolution for the BlackBox MCP server (single source of truth helper).

Pure decision logic lives here so it is dependency-free and unit-testable without
the `mcp`/`httpx` packages. The async fetch wrapper lives in the MCP server.
"""
from typing import List, Optional, Tuple


def choose_operator(
    provided: Optional[str],
    operators: List[str],
    default: str,
) -> Tuple[str, bool]:
    """Resolve which operator to use.

    Returns (resolved, needs_selection):
      - provided (non-blank)  -> (provided.strip(), False)
      - exactly one operator  -> (that, False)
      - multiple operators    -> (default or first, True)   # caller may prompt
      - no operators          -> (default or "Operator", False)
    needs_selection=True signals an interactive caller (agent) SHOULD prompt the
    user to choose; non-interactive callers (the MCP server) just use `resolved`.
    """
    if provided and provided.strip():
        return provided.strip(), False
    if len(operators) == 1:
        return operators[0], False
    if len(operators) > 1:
        return (default or operators[0]), True
    return (default or "Operator"), False
