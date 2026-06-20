"""ToolVault v2 — x-source resolver registry + schema resolution.

A schema property may carry a marker ``"x-source": "<name>"`` meaning "fill this
field from a live source at injection time" (e.g. the current operator list).
This module owns the registry of those resolvers and the function that walks a
schema, applies them, and strips the marker so the resolved schema is clean for
LLM providers (Gemini/OpenAI reject unknown JSON-Schema keys).

Resolvers take a :class:`ToolContext` and return a dict of property overrides
(e.g. ``{"enum": [...], "default": ...}``) that get merged into the property.

The backend operator list is lazy-imported inside ``_list_operators`` so that
importing this module is cheap/side-effect-free, and so tests can monkeypatch
``_list_operators`` without touching the real backend.
"""

import copy
from typing import Optional

from .context import ToolContext


def _list_operators(ctx: ToolContext) -> list[str]:
    """Return the live list of operator names.

    Prefers the canonical backend source ``Orchestrator.config.USERS_LIST`` (same
    list as ``GET /operators``). But ``config.py`` drags heavy web deps
    (fastapi/google/httpx/pydantic) that the LEAN MCP-server venv
    (``MCP/blackbox_mcp_server.py`` lists tools for MCP clients) does NOT have, so
    importing it there raises ModuleNotFoundError and the whole tool-list build
    fails — leaving an MCP client with zero BlackBox tools. So fall back to reading
    the operator list straight from ``config.ini`` with stdlib ``configparser``
    (identical parsing to ``config.USERS_LIST``, zero heavy deps). The import is
    deferred to call time (config-read side effects + lets tests monkeypatch this).
    """
    try:
        from Orchestrator.config import USERS_LIST
        return list(USERS_LIST)
    except Exception:
        # Lean context (e.g. the MCP venv): read config.ini directly. Repo root is
        # $BLACKBOX_ROOT (set by the MCP server) or three dirs up from this module
        # (Orchestrator/toolvault/resolvers.py -> repo root).
        import os
        import configparser
        root = os.environ.get("BLACKBOX_ROOT") or os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        cp = configparser.ConfigParser()
        cp.read(os.path.join(root, "config.ini"))
        raw = cp.get("users", "list", fallback="Brandon")
        return [u.strip() for u in raw.split(",") if u.strip()]


def _resolve_operators(ctx: ToolContext) -> dict:
    """Resolver for ``x-source: operators`` → constrain the field to live operators."""
    operators = _list_operators(ctx)
    return {"enum": operators}


# source-name → resolver fn. Each resolver: (ctx) -> dict of property overrides.
RESOLVERS = {
    "operators": _resolve_operators,
}

# Exposed so schema_spec / the registry can validate x-source markers at build time.
KNOWN_SOURCES = set(RESOLVERS)


def resolve_schema(schema: dict, ctx: Optional[ToolContext] = None) -> dict:
    """Return a deep copy of ``schema`` with all ``x-source`` markers resolved.

    ``ctx`` is optional: converter callers (MCP / static fallbacks) that have no
    per-call context may omit it, in which case a default :class:`ToolContext`
    (operator ``"system"``) is used. The injector still passes an explicit ctx.

    Walks ``parameters.properties``. For each property carrying ``"x-source"``:

    * Known source: call its resolver, merge the returned overrides into the
      property, and remove the ``"x-source"`` key from the output.
    * Unknown source: log a warning and leave the property as-is (marker
      included). Never raises — build-time validation already guards known
      sources; this is a defensive runtime path.

    The input ``schema`` is never mutated. A schema with no
    ``parameters``/``properties`` is returned as an unchanged deep copy.
    """
    ctx = ctx or ToolContext()
    out = copy.deepcopy(schema)

    params = out.get("parameters")
    if not isinstance(params, dict):
        return out

    properties = params.get("properties")
    if not isinstance(properties, dict):
        return out

    for prop_name, prop in properties.items():
        if not isinstance(prop, dict) or "x-source" not in prop:
            continue

        source = prop.get("x-source")
        resolver = RESOLVERS.get(source)
        if resolver is None:
            print(
                f"[toolvault.resolvers] WARNING: property {prop_name!r} has "
                f"unknown x-source {source!r}; leaving property unresolved"
            )
            continue

        overrides = resolver(ctx)
        if isinstance(overrides, dict):
            prop.update(overrides)
        prop.pop("x-source", None)

    return out
