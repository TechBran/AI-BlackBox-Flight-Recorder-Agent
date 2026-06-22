#!/usr/bin/env python3
"""
blackbox_tools.py - BlackBox Tool Executor + Legacy Schema Exports

Tool DEFINITIONS now live in tool_registry.py (single source of truth).
This file provides:
  - BlackBoxToolExecutor class (executes tools)
  - Legacy exports (BLACKBOX_TOOLS_ANTHROPIC/OPENAI/GEMINI) for backward compat
  - get_tools_for_backend() helper
  - execute_tool() convenience function
"""

import base64
import json
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass
from Orchestrator.contacts import search_contacts as _search_contacts, upsert_contact

# Import from the unified registry (single source of truth)
from Orchestrator.tools.tool_registry import (
    get_anthropic_tools,
    get_openai_realtime_tools,
    get_gemini_live_tools,
)

# =============================================================================
# Tool Definitions — Generated from tool_registry.py
# =============================================================================
# These are the "phone" group tools (used by phone bridge and live voice routes).
# chat_routes.py and other consumers import directly from tool_registry.

BLACKBOX_TOOLS_ANTHROPIC = get_anthropic_tools("phone")
BLACKBOX_TOOLS_OPENAI = get_openai_realtime_tools("phone")
BLACKBOX_TOOLS_GEMINI = get_gemini_live_tools("phone")

# =============================================================================
# Tool Executor
# =============================================================================

# ToolResult is defined canonically in toolvault.context and re-exported here so
# the toolvault package has no import-time dependency on this module (breaks the
# cycle now that tool_registry sources its definitions from the toolvault
# registry). Same class object — `blackbox_tools.ToolResult is context.ToolResult`.
from Orchestrator.toolvault.context import ToolResult  # noqa: E402


def _coerce_stringified_json_args(tool_name, tool_input):
    """Tolerate models that emit array/object params as JSON-encoded STRINGS.

    Some models (esp. for deeply-nested params like the Google batchUpdate
    `requests` array) send requests="[{...}]" instead of requests=[{...}]. The
    value arrives as a genuine str, so an executor's isinstance(..., list) check
    correctly rejects it. When the tool schema declares a param as array/object
    and the incoming value is a JSON string of that kind, parse it back.
    Conservative: only str values whose declared type is array/object and which
    parse to the matching type are touched; everything else is left as-is so
    normal validation still applies.
    """
    if not isinstance(tool_input, dict):
        return tool_input
    from Orchestrator.toolvault import registry
    spec = registry.get_tool(tool_name)
    if not spec:
        return tool_input
    props = (spec.get("parameters") or {}).get("properties") or {}
    coerced = None
    for key, val in tool_input.items():
        if not isinstance(val, str):
            continue
        ptype = (props.get(key) or {}).get("type")
        if ptype not in ("array", "object"):
            continue
        s = val.strip()
        if not s or (ptype == "array" and s[0] != "[") or (ptype == "object" and s[0] != "{"):
            continue
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError):
            continue
        if (ptype == "array" and isinstance(parsed, list)) or (ptype == "object" and isinstance(parsed, dict)):
            if coerced is None:
                coerced = dict(tool_input)
            coerced[key] = parsed
            print(f"[ARG-COERCE] {tool_name}.{key}: parsed stringified {ptype} -> native")
    return coerced if coerced is not None else tool_input


class BlackBoxToolExecutor:
    """
    Executes BlackBox tools with unified interface for all AI backends.

    Usage:
        executor = BlackBoxToolExecutor(operator="Brandon")
        result = await executor.execute("send_sms", {"phone_number": "+1555...", "message": "Hello"})
    """

    def __init__(self, operator: str = "system", base_url: str = "http://localhost:9091"):
        self.operator = operator
        self.base_url = base_url

    async def execute(self, tool_name: str, tool_input: Dict[str, Any]) -> ToolResult:
        """Execute a tool and return the result.

        MODULE-ONLY dispatch: ask the ToolVault registry for a per-tool
        ``executor.py`` (it resolves alias → canonical → ``executor.py``) and run
        it with a :class:`ToolContext`. Every tool — including ``toolvault`` —
        now ships an ``executor.py`` module, so there is no legacy fallback: an
        unknown tool (no module executor) is a hard error.
        """
        from Orchestrator.toolvault import registry
        from Orchestrator.toolvault.context import ToolContext

        ex = registry.get_executor(tool_name)
        if ex is None:
            return ToolResult(
                success=False,
                result=f"Unknown tool: {tool_name}"
            )

        # Models sometimes emit array/object params as JSON strings; parse back.
        tool_input = _coerce_stringified_json_args(tool_name, tool_input)

        try:
            return await ex(
                tool_input,
                ToolContext(operator=self.operator, base_url=self.base_url),
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            return ToolResult(
                success=False,
                result=f"Error executing {tool_name}: {str(e)}"
            )


# =============================================================================
# Helper Functions
# =============================================================================

def get_tools_for_backend(backend: str, group: str = "phone") -> List[Dict]:
    """Get tool definitions in the correct format for a backend.

    Uses the unified tool registry. The 'group' param controls which subset
    of tools to include (default: 'phone' for backward compat with voice routes).
    """
    from Orchestrator.tools.tool_registry import (
        get_anthropic_tools as _get_anthropic,
        get_openai_realtime_tools as _get_realtime,
        get_gemini_live_tools as _get_gemini_live,
    )
    if backend in ("openai", "openai_realtime", "grok", "grok_live"):
        return _get_realtime(group)
    elif backend in ("gemini", "gemini_live"):
        return _get_gemini_live(group)
    elif backend in ("anthropic", "claude", "sms"):
        return _get_anthropic(group)
    else:
        return _get_anthropic(group)  # Default


async def execute_tool(
    tool_name: str,
    tool_input: Dict[str, Any],
    operator: str = "system"
) -> ToolResult:
    """
    Convenience function to execute a tool.

    Usage:
        result = await execute_tool("send_sms", {"phone_number": "+1555...", "message": "Hello"}, "Brandon")
    """
    executor = BlackBoxToolExecutor(operator=operator)
    return await executor.execute(tool_name, tool_input)
