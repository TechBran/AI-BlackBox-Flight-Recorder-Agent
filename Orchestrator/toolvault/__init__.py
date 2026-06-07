"""
ToolVault v2 — modules are the source of truth.

Each tool is a self-describing Python module under ``Orchestrator/tools/``
exposing a canonical schema plus an ``execute`` callable. The registry loads,
validates, and caches those modules; embeddings live in a hash-keyed store
(``ToolVault/embeddings.json``) synced from the modules; the injector renders
the registry into per-provider tool arrays and selects which tools to surface
for a given prompt.

  Modules (schema + execute)  →  Registry (load/validate/cache)
                                       ↓
  embeddings.json (semantic store)  →  Injector (select + render per provider)

The v1 byte-offset machinery (volume/manifest/migrate) has been removed.

Live API (convenience re-exports):
  load_canonical()   - All valid canonical tool schemas (optionally per group)
  get_tool()         - Canonical schema for one tool by name
  get_executor()     - Resolved execute callable for one tool by name
  inject_for_prompt()- Select + render tools into a provider tool array
  ToolContext        - Per-request execution context
  ToolResult         - Uniform executor result envelope
"""

from Orchestrator.toolvault.registry import (
    load_canonical,
    get_tool,
    get_executor,
)
from Orchestrator.toolvault.injector import inject_for_prompt
from Orchestrator.toolvault.context import ToolContext, ToolResult

__all__ = [
    "load_canonical",
    "get_tool",
    "get_executor",
    "inject_for_prompt",
    "ToolContext",
    "ToolResult",
]
