#!/usr/bin/env python3
"""
tool_injection.py — shared ToolVault discovery to callable-spec helper.

The on-device (local Gemma) model keeps only a handful of actuators resident and
pulls the rest of ToolVault on demand. Two call sites need the SAME "semantic
search -> per-hit read -> {name, description, parameters} spec" pipeline:

  - POST /local/tools/search   (Orchestrator/routes/local_routes.py) — the
    on-demand search the phone's search_tools meta-tool hits.
  - POST /local/turn/prepare   (Task 3) — assembles the per-turn package and
    injects the top-K relevant ToolVault tools as DIRECTLY-callable tool defs.

This module is the single source of that logic (DRY) so the two endpoints can
never drift. ``build_injected_tools`` is deliberately TOTAL: it NEVER raises —
any failure (meta_tool error, malformed result) yields an empty list — because
Task 3 calls it inline during turn assembly where an exception would break the
whole turn.

``meta_tool`` is imported at MODULE TOP so tests (and the live injector) can
monkeypatch ``tool_injection.meta_tool.execute`` on this module.
"""

from Orchestrator.toolvault import meta_tool


# Tools the on-device (local Gemma) model must NEVER be offered or discover: each
# DELEGATES device control to a (cloud or on-device) agent that targets THIS phone,
# so calling one from the on-device loop is a no-op recursion — control_phone
# literally wakes the on-device model itself. The model already has DIRECT phone
# actuators (open_app/tap/type/swipe/flashlight_on/...). Filtered out of BOTH
# /local/tools/search discovery AND /local/turn/prepare injection, since both flow
# through build_injected_tools.
ON_DEVICE_EXCLUDED_TOOLS = frozenset({
    "control_phone",
    "control_android_device",
    "use_computer",
})


def build_injected_tools(query: str, k: int = 5) -> list[dict]:
    """Semantic-search ToolVault for ``query`` and return up to ``k`` callable
    tool specs.

    Args:
        query: natural-language tool query. Blank/whitespace short-circuits to
            ``[]`` WITHOUT touching meta_tool.
        k: max number of specs to return (top-K by search relevance).

    Returns:
        A list (len <= k) of ``{"name", "description", "parameters"}`` dicts.
        ``parameters`` is the tool's JSON schema (meta_tool calls it "schema").
        Hits whose ``read`` fails (stale/renamed tool) are skipped — fault
        isolation, not a 500 for the whole batch. Returns ``[]`` on ANY error;
        this function never raises.
    """
    if not query or not query.strip():
        return []

    try:
        search = meta_tool.execute("search", query=query)
        matches = (search.data or {}).get("matches", []) if search.success else []
        # Drop the self-delegating device-control tools (recursion hazard) BEFORE the
        # top-k slice so they never crowd out usable tools. See ON_DEVICE_EXCLUDED_TOOLS.
        matches = [m for m in matches if m.get("name") not in ON_DEVICE_EXCLUDED_TOOLS]

        tools: list[dict] = []
        for m in matches[:k]:
            name = m.get("name")
            if not name:
                continue
            # The search result only carries {name, score}; pull the full schema
            # (and description) per hit via the meta-tool's read action.
            spec = meta_tool.execute("read", tool_name=name)
            # Fault isolation: a stale/renamed tool (read failure) is skipped, not
            # 500'd for the whole batch nor appended as an empty-schema garbage entry.
            if not spec.success:
                continue
            data = spec.data or {}
            tools.append({
                "name": name,
                "description": data.get("description", ""),
                # meta_tool calls it "schema"; expose as "parameters" for tool-def consumers
                "parameters": data.get("schema", {}),
            })

        return tools
    except Exception as e:
        # NEVER raise — Task 3 calls this inline during turn assembly.
        print(f"[LOCAL PROVIDER] build_injected_tools failed: {e}")
        return []
