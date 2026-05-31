# Resolve Operator — shared procedure

The canonical way to determine which BlackBox operator an action belongs to.
**Never hard-code an operator name** (not "Brandon", not anything). This is the
foundation other BlackBox skills/commands build on — keep operator resolution here,
in one place.

## Procedure

1. **Explicit wins.** If the caller passed an operator (slash arg, parameter, or the
   user named one), use it verbatim. Done.

2. **Otherwise ask the system.** Call the `get_current_operator` MCP tool. It returns:
   ```json
   { "resolved": "...", "operators": ["..."], "default": "...",
     "count": <n>, "needs_selection": <bool> }
   ```
   - `needs_selection == false` (zero or one operator) → use `resolved`. No prompt.
   - `needs_selection == true` (multiple operators, none specified) → present an
     **AskUserQuestion dropdown** of `operators`, pre-selecting `default`, and use the
     chosen one. (The MCP server itself can't prompt — only the agent can, so the
     dropdown lives here.)

3. **Pass the resolved operator** to the BlackBox call.

## Notes

- For **write/identity** actions (minting a snapshot, generating media, "my context"),
  always resolve to a concrete operator via this procedure.
- For **read/search** tools (`search_snapshots`, `browse_index`), omitting the operator
  intentionally means **all operators on this box** — don't force-resolve those; only
  pass an operator to scope the results.
- If `get_current_operator` is unreachable, the MCP server still resolves write calls
  to a safe non-crashing fallback; prefer surfacing the degraded state over guessing a
  name.
