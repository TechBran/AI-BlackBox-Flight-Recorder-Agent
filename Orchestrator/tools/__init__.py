"""
BlackBox Tools Package

Unified tool definitions and executors for all AI backends.
Tool definitions live in tool_registry.py (single source of truth).

The names below are resolved on first access (PEP 562), not at package
import, so ``import Orchestrator.tools.tool_registry`` stays light: the MCP
server's lean venv imports the registry converters only, and an eager
``from .blackbox_tools import ...`` here pulled the tool executor and the
contacts store into that process (audit O20-mcp-server-17).
"""

import importlib

# name -> the submodule that defines it.
_EXPORTS = {
    # Legacy exports (from blackbox_tools.py)
    "BLACKBOX_TOOLS_ANTHROPIC": "blackbox_tools",
    "BLACKBOX_TOOLS_OPENAI": "blackbox_tools",
    "BLACKBOX_TOOLS_GEMINI": "blackbox_tools",
    "BlackBoxToolExecutor": "blackbox_tools",
    "ToolResult": "blackbox_tools",
    "get_tools_for_backend": "blackbox_tools",
    "execute_tool": "blackbox_tools",
    # Registry exports (from tool_registry.py)
    "TOOL_DEFINITIONS": "tool_registry",
    "get_tools_by_group": "tool_registry",
    "get_tool_by_name": "tool_registry",
    "resolve_alias": "tool_registry",
    "resolve_executor_name": "tool_registry",
    "get_anthropic_tools": "tool_registry",
    "get_openai_rest_tools": "tool_registry",
    "get_openai_realtime_tools": "tool_registry",
    "get_gemini_rest_tools": "tool_registry",
    "get_gemini_live_tools": "tool_registry",
    "get_mcp_tools": "tool_registry",
    "get_all_tool_names": "tool_registry",
    "get_group_tool_names": "tool_registry",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    if name != "TOOL_DEFINITIONS":  # tool_registry materializes it lazily too
        globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
