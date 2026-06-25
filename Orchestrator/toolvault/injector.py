"""
ToolVault Dynamic Injector (v2) — module-driven semantic tool injection.

On every user prompt, this module:
  1. Collects the meta-tool + all Tier 1 tools in the consumer group
  2. Embeds the prompt and ranks Tier 2 tools via the embeddings.json store
  3. Resolves each selected schema's ``x-source`` markers (e.g. live operators)
  4. Converts to the target provider format (Anthropic/OpenAI/Gemini/Grok/MCP)
  5. Returns a ready-to-use tools array + a system-prompt instructions string

This is the LIVE per-prompt injection path (``TOOLVAULT_ENABLED=true``). The
source of truth is the module registry (``ToolVault/tools/<name>/schema.json``)
plus the hash-keyed embeddings store — NOT the old byte-offset volume/manifest.

Token economics:
  Old: ~7,450 tokens (all schemas, every request)
  New: ~800-1,500 tokens (meta-tool + Tier 1 + a few relevant Tier 2 tools)
"""

from typing import Dict, List, Optional, Tuple

from Orchestrator.toolvault.meta_tool import META_TOOL_SCHEMA
from Orchestrator.toolvault import registry
from Orchestrator.toolvault import availability
from Orchestrator.toolvault.resolvers import resolve_schema
from Orchestrator.toolvault.context import ToolContext
from Orchestrator.toolvault.embeddings import (
    load_embeddings_store,
    hybrid_search_store,
)
from Orchestrator.toolvault.config import (
    TIER_1,
    SIMILARITY_THRESHOLD,
)

# Reuse the proven format converters from tool_registry.py
from Orchestrator.tools.tool_registry import (
    _clean_params,
    _strip_for_gemini,
)


# ---------------------------------------------------------------------------
# Provider format registry
# ---------------------------------------------------------------------------

# Maps provider name → canonical format key used by ``_format_tools``.
PROVIDER_FORMATS = {
    "anthropic": "anthropic",
    "openai": "openai_rest",
    "openai_rest": "openai_rest",
    "openai_realtime": "openai_realtime",
    "google": "gemini_rest",     # "google" is an alias for gemini (DEFAULT_PROVIDER)
    "gemini": "gemini_rest",
    "gemini_rest": "gemini_rest",
    "gemini_live": "gemini_live",
    "grok": "openai_rest",       # Grok REST uses OpenAI format
    "grok_live": "openai_realtime",  # Grok Live uses OpenAI Realtime format
    "mcp": "mcp",
}

# Provider → default consumer group
PROVIDER_DEFAULT_GROUP = {
    "anthropic": "chat",
    "openai": "chat",
    "openai_rest": "chat",
    "openai_realtime": "realtime",
    "google": "chat",            # "google" is an alias for gemini (DEFAULT_PROVIDER)
    "gemini": "chat",
    "gemini_rest": "chat",
    "gemini_live": "gemini_live",
    "grok": "chat",
    "grok_live": "grok_live",
    "mcp": "mcp",
}


# ---------------------------------------------------------------------------
# Format converters (mirror tool_registry.py exactly)
# ---------------------------------------------------------------------------

def _to_anthropic(tool: Dict) -> Dict:
    return {
        "name": tool["name"],
        "description": tool["description"],
        "input_schema": _clean_params(tool["parameters"]),
    }


def _to_openai_rest(tool: Dict) -> Dict:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": _clean_params(tool["parameters"]),
        },
    }


def _to_openai_realtime(tool: Dict) -> Dict:
    return {
        "type": "function",
        "name": tool["name"],
        "description": tool["description"],
        "parameters": _clean_params(tool["parameters"]),
    }


def _to_gemini_decl(tool: Dict) -> Dict:
    """Single tool → Gemini function declaration (used inside the wrapper)."""
    return {
        "name": tool["name"],
        "description": tool["description"],
        "parameters": _strip_for_gemini(tool["parameters"]),
    }


def _format_tools(canonical_tools: List[Dict], provider_format: str) -> list:
    """Convert a list of canonical tool dicts to the target provider format.

    Args:
        canonical_tools: List of {name, description, parameters} dicts
        provider_format: One of the format keys

    Returns:
        Provider-formatted tools array, ready for API payload.
    """
    if provider_format == "anthropic":
        return [_to_anthropic(t) for t in canonical_tools]

    elif provider_format == "openai_rest":
        return [_to_openai_rest(t) for t in canonical_tools]

    elif provider_format == "openai_realtime":
        return [_to_openai_realtime(t) for t in canonical_tools]

    elif provider_format == "gemini_rest":
        # Gemini wraps all tools in one function_declarations array
        return [{"function_declarations": [_to_gemini_decl(t) for t in canonical_tools]}]

    elif provider_format == "gemini_live":
        # Gemini Live uses camelCase key
        return [{"functionDeclarations": [_to_gemini_decl(t) for t in canonical_tools]}]

    elif provider_format == "mcp":
        # MCP uses Tool objects — lazy import to avoid dependency
        try:
            from mcp.types import Tool
            return [
                Tool(
                    name=t["name"],
                    description=t["description"],
                    inputSchema=_clean_params(t["parameters"]),
                )
                for t in canonical_tools
            ]
        except ImportError:
            # Fallback: return canonical format if mcp not available
            return canonical_tools

    else:
        # Unknown format — return canonical
        print(f"[TOOLVAULT-INJECT] Unknown format '{provider_format}', returning canonical")
        return canonical_tools


# ---------------------------------------------------------------------------
# Selection — registry + embeddings store
# ---------------------------------------------------------------------------

def _select_names(
    prompt: str,
    group: str,
    max_semantic_tools: int,
    similarity_threshold: float,
) -> List[Tuple[str, str]]:
    """Select tool names for a prompt, with a reason for each.

    Three-part selection:
      * ``toolvault`` meta-tool first (reason ``"meta"``).
      * every Tier 1 tool **in ``group``** (reason ``"tier1"``) — the always-on
        baseline; this is the ONLY place the group filter applies.
      * semantic ranking over the **ENTIRE catalog** (all groups, all tiers —
        tier-1, tier-2, AND tier-3 internal tools), MINUS whatever's already
        selected, via ``hybrid_search_store`` up to ``max_semantic_tools`` above
        ``similarity_threshold`` (reason ``"semantic(0.xx)"``). Only run when
        ``prompt.strip()``. Discovery is global by design — out-of-group and
        tier-3 tools surface here when semantically relevant.

    Returns an ordered list of ``(name, reason)`` with no duplicates.
    """
    selected: List[Tuple[str, str]] = [("toolvault", "meta")]
    seen = {"toolvault"}

    # --- Tier 1: always-on baseline, filtered by the requesting group ---
    #     ...and by availability (an x-availability gate drops a tool when its
    #     provider key is absent or the provider isn't enabled; no-op for the
    #     ungated catalog of today).
    for entry in availability.filter_available(registry.load_canonical(group)):
        if entry.get("tier") == TIER_1:
            name = entry.get("name")
            if name and name not in seen:
                selected.append((name, "tier1"))
                seen.add(name)

    # --- Semantic: GLOBAL pool = every canonical tool minus already-selected ---
    if prompt.strip():
        # ALL tools (all groups, all tiers), excluding what's already selected.
        searchable = {
            e.get("name"): e.get("description", "")
            for e in availability.filter_available(registry.load_canonical())
            if e.get("name") and e.get("name") not in seen
        }
        if searchable:
            store = load_embeddings_store()
            # Scope the store to the searchable names so already-selected tools
            # (e.g. a tier-1 tool that also has a vector) can't consume a
            # semantic slot.
            scoped = {n: store[n] for n in searchable if n in store}
            matches = hybrid_search_store(
                query=prompt,
                descriptions=searchable,
                store=scoped,
                limit=max_semantic_tools,
                threshold=similarity_threshold,
            )
            for name, score in matches:
                if name not in seen:  # defensive dedup
                    selected.append((name, f"semantic({score:.2f})"))
                    seen.add(name)

    return selected


def _canonical_for(name: str, ctx: ToolContext) -> Optional[Dict]:
    """Build a resolved canonical ``{name, description, parameters}`` for a tool.

    The meta-tool has no x-source and is passed straight through. Every other
    tool is read from the registry and run through :func:`resolve_schema` so
    ``x-source`` markers are filled and stripped (provider-clean).
    """
    if name == "toolvault":
        return {
            "name": META_TOOL_SCHEMA["name"],
            "description": META_TOOL_SCHEMA["description"],
            "parameters": META_TOOL_SCHEMA["parameters"],
        }

    entry = registry.get_tool(name)
    if not entry:
        return None

    resolved = resolve_schema(entry, ctx)
    return {
        "name": resolved.get("name", name),
        "description": resolved.get("description", ""),
        "parameters": resolved.get("parameters", {"type": "object", "properties": {}}),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_injected_tool_names(
    prompt: str,
    group: str = "chat",
    max_semantic_tools: int = 8,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
) -> List[Tuple[str, str]]:
    """Preview which tools would be injected (without format conversion).

    Returns list of ``(tool_name, reason)`` tuples where reason is
    ``"meta"``, ``"tier1"``, or ``"semantic(0.xx)"``. Useful for testing/preview.
    """
    return _select_names(prompt, group, max_semantic_tools, similarity_threshold)


def get_tools_for_prompt(
    prompt: str,
    provider: str,
    group: Optional[str] = None,
    max_semantic_tools: int = 8,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
    *,
    ctx: Optional[ToolContext] = None,
) -> list:
    """Dynamically select and format tools for a specific user prompt.

    Returns the provider-formatted tools array only (no instructions). Selection
    matches :func:`inject_for_prompt`; this is kept for testing/preview parity.
    """
    ctx = ctx or ToolContext()
    provider_format = PROVIDER_FORMATS.get(provider, "openai_rest")
    if group is None:
        group = PROVIDER_DEFAULT_GROUP.get(provider, "chat")

    names = _select_names(prompt, group, max_semantic_tools, similarity_threshold)

    canonical_list = []
    for name, _reason in names:
        canonical = _canonical_for(name, ctx)
        if canonical:
            canonical_list.append(canonical)

    return _format_tools(canonical_list, provider_format)


def build_tool_instructions(
    tool_names: List[str],
    ctx: Optional[ToolContext] = None,
) -> str:
    """Generate the human-readable AVAILABLE TOOLS section for the system prompt.

    For each name (skipping the meta-tool ``toolvault``) read the canonical
    entry from the registry, resolve its ``x-source`` markers, and render the
    description + a human-readable parameter summary + optional example/notes.

    Args:
        tool_names: Tool names to describe (typically the injected set).
        ctx: Execution context for x-source resolution (default: ToolContext()).

    Returns:
        Formatted string for injection into the system prompt, or "" if empty.
    """
    if not tool_names:
        return ""

    ctx = ctx or ToolContext()
    sections = []

    for name in tool_names:
        # Skip meta-tool — it has its own fixed description.
        if name == "toolvault":
            continue

        entry = registry.get_tool(name)
        if not entry:
            continue

        resolved = resolve_schema(entry, ctx)
        desc = resolved.get("description", "")
        example = resolved.get("example", "")
        notes = resolved.get("notes", "")

        lines = [f"  Tool: {name}"]
        if desc:
            lines.append(f"  Description: {desc}")

        param_lines = _summarize_parameters(resolved.get("parameters"))
        if param_lines:
            lines.append("  Parameters:")
            lines.extend(f"    {pl}" for pl in param_lines)

        if example:
            lines.append(f"  Example: {example}")
        if notes:
            lines.append(f"  Notes: {notes}")

        sections.append("\n".join(lines))

    if not sections:
        return ""

    header = (
        "AVAILABLE TOOLS:\n"
        "You have access to the following tools. Call them by name with the required parameters.\n"
        "Use the toolvault tool to discover additional tools not listed here.\n"
    )

    result = header + "\n\n".join(sections) + "\n"
    # Append per-feature default-provider hints. Both may appear if both tool
    # types are injected (web hint stays byte-identical via the back-compat path).
    web_hint = availability.default_provider_hint(tool_names, "web_search")
    if web_hint:
        result += "\n" + web_hint + "\n"
    image_hint = availability.default_provider_hint(tool_names, "image")
    if image_hint:
        result += "\n" + image_hint + "\n"
    return result


def _summarize_parameters(parameters: Optional[Dict]) -> List[str]:
    """Render a JSON-Schema ``parameters`` object to human-readable param lines.

    One line per property: ``name (type, required): description [enum: ...]``.
    Reflects any resolved ``x-source`` enum (resolve_schema runs before this).
    """
    if not isinstance(parameters, dict):
        return []
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return []
    required = set(parameters.get("required") or [])

    out: List[str] = []
    for prop_name, prop in properties.items():
        if not isinstance(prop, dict):
            out.append(f"{prop_name}")
            continue
        ptype = prop.get("type", "any")
        req = "required" if prop_name in required else "optional"
        pdesc = prop.get("description", "")
        line = f"{prop_name} ({ptype}, {req})"
        if pdesc:
            line += f": {pdesc}"
        enum = prop.get("enum")
        if isinstance(enum, list) and enum:
            line += f" [enum: {', '.join(str(e) for e in enum)}]"
        out.append(line)
    return out


def inject_for_prompt(
    prompt: str,
    provider: str,
    group: Optional[str] = None,
    max_semantic_tools: int = 8,
    similarity_threshold: float = SIMILARITY_THRESHOLD,
    *,
    ctx: Optional[ToolContext] = None,
) -> Tuple[list, str]:
    """Single selection pass returning BOTH tool schemas AND prompt instructions.

    This is the efficient live entry point — one selection, two outputs:
      1. Tool schemas formatted for the provider's ``tools=[]`` array.
      2. Human-readable tool instructions for the system prompt.

    Args:
        prompt: User message text.
        provider: Target provider.
        group: Consumer group (default: inferred from provider).
        max_semantic_tools: Max Tier 2 tools to inject.
        similarity_threshold: Minimum score for semantic matches.
        ctx: Execution context for x-source resolution (default: ToolContext()).

    Returns:
        Tuple of ``(formatted_tools_array, tool_instructions_text)``.
    """
    ctx = ctx or ToolContext()
    provider_format = PROVIDER_FORMATS.get(provider, "openai_rest")
    if group is None:
        group = PROVIDER_DEFAULT_GROUP.get(provider, "chat")

    names = _select_names(prompt, group, max_semantic_tools, similarity_threshold)
    selected_names = [n for n, _ in names]

    # Build resolved canonical schemas for the API payload.
    canonical_list = []
    for name, _reason in names:
        canonical = _canonical_for(name, ctx)
        if canonical:
            canonical_list.append(canonical)

    formatted_tools = _format_tools(canonical_list, provider_format)

    # Human-readable instructions for the system prompt.
    tool_instructions = build_tool_instructions(selected_names, ctx)

    # Injection summary.
    Y = "\033[33m"
    R = "\033[0m"
    meta_count = sum(1 for _, r in names if r == "meta")
    tier1_count = sum(1 for _, r in names if r == "tier1")
    tier2_count = sum(1 for _, r in names if r.startswith("semantic("))
    print(f"{Y}[TOOLVAULT-INJECT] {provider}/{group} → {provider_format}: "
          f"{len(names)} tools + system prompt instructions "
          f"({meta_count} meta + {tier1_count} T1 + {tier2_count} T2){R}")

    return formatted_tools, tool_instructions
