"""
tool_registry.py - Single Source of Truth for all BlackBox tool definitions.

Define each tool ONCE in a canonical (provider-agnostic) format.
Format converters generate correct schemas for any AI provider:
  - Anthropic (input_schema)
  - OpenAI REST (type: function, function: {name, parameters})
  - OpenAI Realtime / Grok Live (type: function, name, parameters)
  - Gemini REST (function_declarations, snake_case)
  - Gemini Live (functionDeclarations, camelCase)
  - MCP (Tool objects with inputSchema)

Groups control which tools appear for each consumer. Membership is declared
per-tool in ToolVault/tools/<name>/schema.json "groups" arrays; counts drift
as tools land, so none are baked in here (each of the three voice groups
carried 56 tools as of 2026-07-11 — NOT the ~21 this docstring once claimed):
  chat         - REST chat handlers (all providers)
  chat_cu      - Computer Use agent (chat minus use_computer itself)
  realtime     - OpenAI Realtime voice WebSocket
  gemini_live  - Gemini Live voice WebSocket
  grok_live    - Grok Live voice WebSocket
  phone        - Phone bridge / blackbox_tools.py
  mcp          - MCP server for Claude Code
"""

from typing import Dict, List, Optional, Any
import copy


# =============================================================================
# Canonical Tool Definitions
# =============================================================================
#
# ToolVault v2 cutover: TOOL_DEFINITIONS is no longer a giant in-file literal.
# The per-tool ``ToolVault/tools/<name>/schema.json`` modules are now the single
# source of truth; ``registry.load_canonical()`` loads + validates them into the
# canonical list (48 non-ugv tools; UGV is intentionally absent from the module
# registry). The provider-format converters below STAY here as a shared library.
#
# ``TOOL_DEFINITIONS`` and ``_TOOL_INDEX`` are materialized lazily on first
# access (import-time snapshot, built ONCE — same static-at-startup behavior as
# the old literal). The lazy init exists to break an import cycle: the eager
# chain ``Orchestrator.tools.__init__ → blackbox_tools → tool_registry →
# toolvault.registry → toolvault.resolvers → toolvault.context →
# blackbox_tools.ToolResult`` would hit a partially-initialized blackbox_tools.
# Deferring the toolvault import + load_canonical() call until first use (after
# all modules finish importing) sidesteps it without touching the toolvault
# package. load_canonical() is itself mtime-cached, so a process restart picks
# up on-disk schema edits.

_TOOL_DEFINITIONS: Optional[List[Dict[str, Any]]] = None
_TOOL_INDEX_CACHE: Optional[Dict[str, Dict]] = None


def _load_tool_definitions() -> List[Dict[str, Any]]:
    # Function-local import: keeps toolvault.registry OUT of the import chain so
    # the cycle described above never closes during module import.
    from Orchestrator.toolvault import registry as _tv_registry
    return _tv_registry.load_canonical()


def _ensure_loaded() -> None:
    """Materialize the import-time snapshot once (idempotent)."""
    global _TOOL_DEFINITIONS, _TOOL_INDEX_CACHE
    if _TOOL_DEFINITIONS is None:
        _TOOL_DEFINITIONS = _load_tool_definitions()
        _TOOL_INDEX_CACHE = {t["name"]: t for t in _TOOL_DEFINITIONS}


def reset_cache() -> None:
    """Drop the materialized snapshot so the next access reloads from the registry.

    Called by POST /toolvault/reload so registry-derived calls (get_*_tools,
    get_tools_by_group, get_tool_by_name, get_mcp_tools) reflect on-disk schema
    edits without a process restart. NOTE: import-time module constants built from
    these — blackbox_tools.BLACKBOX_TOOLS_* (phone group) and chat_routes.CHAT_TOOLS_*
    (the TOOLVAULT_ENABLED=false fallback) — are frozen snapshots and still require
    a restart. The live chat injector reads load_canonical() fresh and is unaffected.
    """
    global _TOOL_DEFINITIONS, _TOOL_INDEX_CACHE
    _TOOL_DEFINITIONS = None
    _TOOL_INDEX_CACHE = None


def __getattr__(name: str):
    """Module-level lazy attributes: ``TOOL_DEFINITIONS`` and ``_TOOL_INDEX``.

    PEP 562 hook — fires only for names not found as real module globals, so
    external ``tool_registry.TOOL_DEFINITIONS`` / ``._TOOL_INDEX`` access works
    exactly as before, but materialization is deferred to first reference.
    """
    if name == "TOOL_DEFINITIONS":
        _ensure_loaded()
        return _TOOL_DEFINITIONS
    if name == "_TOOL_INDEX":
        _ensure_loaded()
        return _TOOL_INDEX_CACHE
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Alias → canonical name (backward compatibility)
# Maps old/variant tool names to the canonical name in the registry.
_ALIASES: Dict[str, str] = {
    "search_memory": "search_snapshots",
    "get_recent_snapshots": "list_recent_snapshots",
}

# Canonical name → executor method name (for BlackBoxToolExecutor dispatch)
# Only needed when the canonical name differs from the executor method.
_EXECUTOR_NAMES: Dict[str, str] = {
    "search_snapshots": "search_memory",
}


def get_tools_by_group(group: str) -> List[Dict]:
    """Return canonical tool definitions belonging to a group.

    Filtered through ``availability.filter_available`` so a tool carrying an
    ``x-availability`` gate is dropped when its provider key is absent or the
    provider is not enabled. ``availability`` is imported lazily (stdlib-only
    module) to mirror the lazy ``_tv_registry`` import and avoid any import
    cycle; it is a no-op on today's ungated catalog.
    """
    from Orchestrator.toolvault import availability
    _ensure_loaded()
    return availability.filter_available(
        [t for t in _TOOL_DEFINITIONS if group in t.get("groups", [])]
    )


def get_tool_by_name(name: str) -> Optional[Dict]:
    """Look up a tool by name or alias."""
    _ensure_loaded()
    if name in _TOOL_INDEX_CACHE:
        return _TOOL_INDEX_CACHE[name]
    canonical = _ALIASES.get(name)
    return _TOOL_INDEX_CACHE.get(canonical) if canonical else None


def resolve_alias(name: str) -> str:
    """Resolve a tool alias to its canonical name. Returns input if not an alias."""
    return _ALIASES.get(name, name)


def resolve_executor_name(name: str) -> str:
    """Resolve a canonical tool name to its executor method name.

    Most tools use the same name. Only a few need remapping
    (e.g., search_snapshots → search_memory for the executor).
    """
    canonical = resolve_alias(name)
    return _EXECUTOR_NAMES.get(canonical, canonical)


# =============================================================================
# Format Converters
# =============================================================================

def _clean_params(params: Dict) -> Dict:
    """Deep copy parameters, stripping registry-only fields."""
    return copy.deepcopy(params)


def _strip_for_gemini(params: Dict) -> Dict:
    """Clean params for Gemini compatibility.

    Gemini restrictions:
    - No 'default' keys in properties
    - Enum values must be strings
    - Enum is only allowed on STRING type properties
    - No 'minimum'/'maximum' constraints
    """
    params = copy.deepcopy(params)
    for prop in params.get("properties", {}).values():
        prop.pop("default", None)
        prop.pop("minimum", None)
        prop.pop("maximum", None)
        prop.pop("maxItems", None)
        # Gemini only allows enum on STRING type — convert if needed
        if "enum" in prop:
            prop["enum"] = [str(v) for v in prop["enum"]]
            prop["type"] = "string"
    return params


def to_anthropic(tool: Dict) -> Dict:
    """Canonical → Anthropic format (input_schema wrapper)."""
    return {
        "name": tool["name"],
        "description": tool["description"],
        "input_schema": _clean_params(tool["parameters"]),
    }


def to_openai_rest(tool: Dict) -> Dict:
    """Canonical → OpenAI Chat Completions format (nested function wrapper)."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": _clean_params(tool["parameters"]),
        }
    }


def to_openai_realtime(tool: Dict) -> Dict:
    """Canonical → OpenAI Realtime / Grok Live format (flat, type=function)."""
    return {
        "type": "function",
        "name": tool["name"],
        "description": tool["description"],
        "parameters": _clean_params(tool["parameters"]),
    }


def to_gemini_rest(tools: List[Dict]) -> List[Dict]:
    """Canonical list → Gemini REST format: [{"function_declarations": [...]}]."""
    return [{
        "function_declarations": [
            {
                "name": t["name"],
                "description": t["description"],
                "parameters": _strip_for_gemini(t["parameters"]),
            }
            for t in tools
        ]
    }]


def to_gemini_live(tools: List[Dict]) -> List[Dict]:
    """Canonical list → Gemini Live format: [{"functionDeclarations": [...]}]."""
    return [{
        "functionDeclarations": [
            {
                "name": t["name"],
                "description": t["description"],
                "parameters": _strip_for_gemini(t["parameters"]),
            }
            for t in tools
        ]
    }]


def to_mcp(tool: Dict):
    """Canonical → MCP Tool() object. Lazy import to avoid MCP dependency."""
    from mcp.types import Tool
    return Tool(
        name=tool["name"],
        description=tool["description"],
        inputSchema=_clean_params(tool["parameters"]),
    )


# =============================================================================
# Convenience Getters (what consumers import)
# =============================================================================

def _resolved_group(group: str) -> List[Dict]:
    """Canonical tools for a group, with x-source markers resolved.

    Every non-injector consumer (MCP, static fallbacks, any direct converter
    use) runs each canonical schema through ``resolve_schema`` BEFORE conversion,
    so dynamic ``x-source`` fields resolve identically to the chat injector.
    ``resolve_schema`` is lazy-imported to keep ``toolvault.resolvers`` out of
    this module's import chain (avoids the cycle the lazy TOOL_DEFINITIONS init
    is designed to sidestep). With no module carrying ``x-source`` today this is
    a no-op (returns an equivalent dict), so converter output — and parity —
    is unchanged.
    """
    from Orchestrator.toolvault.resolvers import resolve_schema
    return [resolve_schema(t) for t in get_tools_by_group(group)]


def get_anthropic_tools(group: str = "chat") -> List[Dict]:
    """Get tools in Anthropic format for a group."""
    return [to_anthropic(t) for t in _resolved_group(group)]


def get_openai_rest_tools(group: str = "chat") -> List[Dict]:
    """Get tools in OpenAI Chat Completions format for a group."""
    return [to_openai_rest(t) for t in _resolved_group(group)]


def get_openai_realtime_tools(group: str = "realtime") -> List[Dict]:
    """Get tools in OpenAI Realtime (flat) format for a group."""
    return [to_openai_realtime(t) for t in _resolved_group(group)]


def get_gemini_rest_tools(group: str = "chat") -> List[Dict]:
    """Get tools in Gemini REST format for a group."""
    return to_gemini_rest(_resolved_group(group))


def get_gemini_live_tools(group: str = "gemini_live") -> List[Dict]:
    """Get tools in Gemini Live format for a group."""
    return to_gemini_live(_resolved_group(group))


def get_mcp_tools() -> list:
    """Get all MCP-group tools as MCP Tool objects.

    ``_resolved_group("mcp")`` already routes through ``get_tools_by_group``,
    which applies the ``availability`` presence-gate, so unavailable tools are
    excluded before conversion (no-op on today's ungated catalog).
    """
    return [to_mcp(t) for t in _resolved_group("mcp")]


# =============================================================================
# Utility
# =============================================================================

def get_all_tool_names() -> List[str]:
    """Return all canonical tool names."""
    _ensure_loaded()
    return [t["name"] for t in _TOOL_DEFINITIONS]


def get_group_tool_names(group: str) -> List[str]:
    """Return tool names for a specific group."""
    return [t["name"] for t in get_tools_by_group(group)]
