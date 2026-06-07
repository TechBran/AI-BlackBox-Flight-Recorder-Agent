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

    Lazy-imports the canonical backend source — ``Orchestrator.config.USERS_LIST``
    — which is the same list returned by the ``GET /operators`` route
    (``admin_routes.list_operators``). Importing ``config`` at module top-level
    would trigger its config-file read side effects, so the import is deferred to
    call time; this also lets tests monkeypatch this helper.
    """
    from Orchestrator.config import USERS_LIST

    return list(USERS_LIST)


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
