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
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass
from Orchestrator.contacts import search_contacts as _search_contacts, upsert_contact

# Import from the unified registry (single source of truth)
from Orchestrator.tools.tool_registry import (
    get_anthropic_tools,
    get_openai_realtime_tools,
    get_gemini_live_tools,
    resolve_executor_name,
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

        MODULE-FIRST dispatch: ask the ToolVault registry for a per-tool
        ``executor.py`` (it resolves alias → canonical → ``executor.py``). If one
        exists, run it with a :class:`ToolContext`. Otherwise fall back to the
        legacy ``_execute_<name>`` method on this class.

        Until Task 6.2 migrates every executor into a module, NO ``executor.py``
        files ship, so ``get_executor`` always returns None and every call falls
        through to legacy — behavior is unchanged today; this just builds the rail.
        """
        from Orchestrator.toolvault import registry
        from Orchestrator.toolvault.context import ToolContext

        # Module-first: a per-tool executor.py wins (handles alias → canonical).
        ex = registry.get_executor(tool_name)
        if ex is not None:
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

        # Legacy fallback (until 6.2 migrates all executors).
        # Resolve aliases (e.g., search_snapshots → search_memory for executor method)
        legacy = resolve_executor_name(tool_name)

        handler = getattr(self, f"_execute_{legacy}", None)
        if handler is None:
            return ToolResult(
                success=False,
                result=f"Unknown tool: {legacy}"
            )

        try:
            return await handler(tool_input)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return ToolResult(
                success=False,
                result=f"Error executing {legacy}: {str(e)}"
            )

    async def _execute_toolvault(self, params: Dict[str, Any]) -> ToolResult:
        """Execute a ToolVault meta-tool action (search/read/list)."""
        from Orchestrator.toolvault.meta_tool import execute as tv_execute
        action = params.get("action", "")
        # Pass all params except 'action' to the executor
        action_params = {k: v for k, v in params.items() if k != "action"}
        result = tv_execute(action, **action_params)
        return ToolResult(
            success=result.success,
            result=result.result,
            data=result.data if result.data else None,
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
