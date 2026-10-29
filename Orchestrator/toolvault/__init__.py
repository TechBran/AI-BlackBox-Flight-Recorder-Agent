"""
ToolVault v2 — modules are the source of truth.

Each tool is a self-describing module folder under ``ToolVault/tools/<name>/``
(a ``schema.json`` plus an optional ``executor.py`` ``execute`` callable). The registry loads,
validates, and caches those modules; embeddings live in a hash-keyed store
(``ToolVault/embeddings.json``) synced from the modules; the injector renders
the registry into per-provider tool arrays and selects which tools to surface
for a given prompt.

  Modules (schema + execute)  →  Registry (load/validate/cache)
                                       ↓
  embeddings.json (semantic store)  →  Injector (select + render per provider)

The v1 byte-offset machinery (volume/manifest/migrate) has been removed.

Live API (convenience re-exports, resolved lazily):
  load_canonical()   - All valid canonical tool schemas (optionally per group)
  get_tool()         - Canonical schema for one tool by name
  get_executor()     - Resolved execute callable for one tool by name
  inject_for_prompt()- Select + render tools into a provider tool array
  ToolContext        - Per-request execution context
  ToolResult         - Uniform executor result envelope

The re-exports are resolved on first access (PEP 562), not at package import:
the MCP server's lean venv imports ``Orchestrator.toolvault.registry`` /
``resolvers`` / ``availability`` only, and an eager ``import injector`` here
pulled the embeddings layer and ``Orchestrator.tools`` (the tool executor and
its contacts store) into that process (audit O20-mcp-server-17).
"""

import importlib

# name -> the submodule that defines it.
_EXPORTS = {
    "load_canonical": "registry",
    "get_tool": "registry",
    "get_executor": "registry",
    "inject_for_prompt": "injector",
    "ToolContext": "context",
    "ToolResult": "context",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
