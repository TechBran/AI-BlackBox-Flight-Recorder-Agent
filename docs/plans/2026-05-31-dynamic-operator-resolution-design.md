# Dynamic Operator Resolution — Design

**Date:** 2026-05-31
**Problem:** The `/snapshot-dev` slash command and the BlackBox MCP tools hard-code/assume the operator `Brandon`. On a customer's BlackBox (their own operators), work should resolve to *their* operator automatically. This also lays the foundation for a future suite of "skills that code into the BlackBox."

## Decisions (from investigation + user)

| Question | Decision |
|---|---|
| Resolve rule | **1 operator → use it; multiple → present a dropdown** (Claude Code AskUserQuestion) to pick. `default` is the pre-selected/ fallback. |
| Mechanism | **MCP auto-resolves server-side** when `operator` omitted (the dropdown is agent-side; MCP can't prompt). |
| Scope | **Fix + reusable helper** — extract a canonical operator-resolution primitive future BlackBox skills reuse. |

## Key architectural split (why two layers)

The MCP server is a **stdio subprocess** — it cannot show the user a dropdown. Only the agent (Claude Code) can, via `AskUserQuestion`. So resolution is split:

- **MCP server (safety net, non-interactive):** when a tool is called without `operator`, resolve server-side: 1 operator → that; multiple → `default`. Guarantees every programmatic call works without the LLM hard-coding anyone.
- **Agent-side (interactive UX):** for flows like `/snapshot-dev`, call the new `get_current_operator` tool; if it reports `needs_selection` (multiple operators, none chosen), present an `AskUserQuestion` dropdown (pre-selecting `default`) and pass the chosen operator explicitly.

## Current state (findings)

- `GET /operators` (`Orchestrator/routes/admin_routes.py:368`) → `{"operators": USERS_LIST, "default": USERS_DEFAULT}`. `USERS_LIST` from config `[users] list` (fallback `"Brandon"`), `USERS_DEFAULT` from `[users] default` (fallback first in list). **Already the single source of truth — just unconsumed.**
- MCP server `MCP/blackbox_mcp_server.py`: `@server.call_tool()` dispatch. **14** handlers use `arguments["operator"]` (KeyError if missing → effectively required); 2 use `.get`. Already has `httpx` + `BLACKBOX_URL` (so it can call `/operators`).
- MCP schemas come from `Orchestrator/tools/tool_registry.py get_mcp_tools()`. `operator` is **already optional** there (not in `required`); descriptions are generic (don't name Brandon).
- Slash command `.claude/commands/snapshot-dev.md`: hard-codes `Brandon` in 5 spots.
- `CLAUDE.md`: "operator default Brandon" guidance.
- MCP `list_operators` tool currently aggregates operators from the *snapshot index* (who has snapshots), NOT from `/operators` (registered operators + default).

## Target design

### 1. Reusable resolution primitive (the foundation)
A pure decision function (unit-testable) + a fetch wrapper, in the MCP server (or a shared module):
```python
def choose_operator(provided, operators, default):
    # returns (resolved: str, needs_selection: bool)
    if provided: return provided, False
    if len(operators) == 1: return operators[0], False
    if len(operators) > 1: return (default or operators[0]), True   # MCP uses resolved; agent honors needs_selection
    return (default or "Operator"), False
```
`async def resolve_operator(provided)` → fetches `GET /operators` (cached per-process), applies `choose_operator`, returns the resolved string. Used by all MCP handlers.

### 2. MCP server changes
- Replace the 14 `arguments["operator"]` and 2 `.get` with `await resolve_operator(arguments.get("operator"))`.
- Add a **`get_current_operator`** MCP tool → `{resolved, operators, default, count, needs_selection}` (calls `/operators` + `choose_operator(None, ...)`). This is the primitive agents/skills call.
- Optionally enrich `list_operators` to merge `/operators` (registered) with the snapshot-index aggregation.

### 3. Tool schemas (`tool_registry.py`)
- Update each `operator` description to: "Optional — if omitted, resolves to the BlackBox's current operator (single operator auto-selected; otherwise the system default)." (No behavior change to `required` arrays; they're already correct.)

### 4. Agent-side reusable procedure
A short documented procedure (referenced by `snapshot-dev` and future skills): "Call `get_current_operator`. If `needs_selection` is true, `AskUserQuestion` a dropdown of `operators` (pre-select `default`); else use `resolved`." Lives where future BlackBox skills can reuse it (e.g. a `.claude/commands/` helper doc or a CLAUDE.md section).

### 5. `snapshot-dev.md`
- Remove all hard-coded `Brandon`. Procedure: if a slash-arg operator is given, use it; else run the agent-side resolution (single → auto; multiple → dropdown). Put the resolved operator into the `/chat/save` payload.

### 6. `CLAUDE.md`
- Replace "operator default Brandon" with "resolve the current operator dynamically (see operator-resolution procedure); never hard-code an operator."

## Non-goals / YAGNI
- No full "skills suite" build now — only the reusable resolution primitive + procedure that the suite will build on.
- No change to how snapshots are stored or to `/operators` itself.
- No per-CLI-session operator binding (out of scope; resolution is install-level).

## Success criteria
- No MCP tool requires the caller to know the operator; omitting it resolves correctly on any box.
- Single-operator box: silent auto-resolution (incl. fresh "Brandon"-seed box — backward compatible).
- Multi-operator box: `/snapshot-dev` (and resolution-aware flows) present a dropdown to pick.
- `get_current_operator` returns the list + default + needs_selection.
- No literal "Brandon" left in `snapshot-dev.md` or as a hard-coded operator default in the MCP path / CLAUDE.md.
