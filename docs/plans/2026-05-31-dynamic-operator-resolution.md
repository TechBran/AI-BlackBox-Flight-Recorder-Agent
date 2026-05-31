# Dynamic Operator Resolution Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop hard-coding the operator "Brandon" in the BlackBox MCP tools and the `/snapshot-dev` slash command; resolve the operator dynamically from the system (`GET /operators`) — single operator auto-resolves, multiple operators get a Claude Code dropdown — and extract a reusable resolution primitive for future BlackBox skills.

**Architecture:** A pure `choose_operator()` decision function (unit-tested) in a dependency-free `MCP/operator_resolution.py`; the MCP server uses it (via an async `resolve_operator()` that fetches `/operators`, cached) so any tool called without `operator` resolves server-side; a new `get_current_operator` MCP tool exposes `{resolved, operators, default, count, needs_selection}` as the primitive agents/skills use to drive the dropdown. The slash command + CLAUDE.md stop naming Brandon.

**Tech Stack:** Python (MCP server uses `MCP/venv`; pure module tested with `Orchestrator/venv` pytest), Markdown (slash command, CLAUDE.md), FastAPI backend `GET /operators` (unchanged).

**Design doc:** `docs/plans/2026-05-31-dynamic-operator-resolution-design.md`

---

## Working dirs & commands (repo root)
```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc"
```
- Pure-function tests (Orchestrator venv has pytest 9.0.3; MCP venv does NOT): `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_operator_resolution.py -v`
- MCP server runtime: `MCP/venv/bin/python` (has `mcp` + `httpx`). The Orchestrator venv has NO `mcp` package — never import `blackbox_mcp_server` from it.
- `/operators` smoke: `curl -s http://localhost:9091/operators | python3 -m json.tool`
- MCP server syntax check: `MCP/venv/bin/python -c "import ast; ast.parse(open('MCP/blackbox_mcp_server.py').read()); print('ok')"`

> **MCP reconnect note:** Claude Code spawns the MCP server per session. Edits to `blackbox_mcp_server.py` are picked up on the next session / MCP reconnect, not live mid-session. Verify logic via the pure tests + `/operators` + a standalone driver; full live verification is on next session.

> **Scope:** the MCP server path (CLI-agent sessions) + slash command + CLAUDE.md. The backend's own chat-tool execution path (`chat_routes.py`) already gets `operator` per-request and is OUT OF SCOPE.

---

## Task 0: Baseline
Run `Orchestrator/venv/bin/python -m pytest Orchestrator/tests -q` (note pre-existing state) and `curl -s http://localhost:9091/operators` (confirm shape `{"operators":[...],"default":"..."}`). If broken, STOP.

---

## Task 1: Pure `choose_operator()` + tests (TDD)

**Files:**
- Create: `MCP/operator_resolution.py`
- Create: `MCP/__init__.py` (empty — makes `MCP` importable as a package for tests)
- Test: `Orchestrator/tests/test_operator_resolution.py`

**Step 1: Write the failing test** — `Orchestrator/tests/test_operator_resolution.py`:
```python
"""Operator resolution decision logic (2026-05-31)."""
from MCP.operator_resolution import choose_operator

def test_explicit_operator_wins():
    assert choose_operator("Anna", ["Brandon", "Anna"], "Brandon") == ("Anna", False)

def test_single_operator_auto():
    assert choose_operator(None, ["Brandon"], "Brandon") == ("Brandon", False)

def test_single_operator_auto_ignores_blank():
    assert choose_operator("", ["Anna"], "Anna") == ("Anna", False)

def test_multiple_needs_selection_resolves_to_default():
    assert choose_operator(None, ["Brandon", "Anna", "Sam"], "Anna") == ("Anna", True)

def test_multiple_default_missing_falls_back_to_first():
    assert choose_operator(None, ["Brandon", "Anna"], "") == ("Brandon", True)

def test_empty_list_uses_default_or_operator():
    assert choose_operator(None, [], "") == ("Operator", False)
    assert choose_operator(None, [], "Zed") == ("Zed", False)
```

**Step 2: Run, verify fail:** `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_operator_resolution.py -v` → ImportError.

**Step 3: Implement** — `MCP/operator_resolution.py` (NO third-party imports — must import cleanly under any venv):
```python
"""Operator resolution for the BlackBox MCP server (single source of truth helper).

Pure decision logic lives here so it is dependency-free and unit-testable without
the `mcp`/`httpx` packages. The async fetch wrapper lives in the MCP server.
"""
from typing import List, Optional, Tuple


def choose_operator(
    provided: Optional[str],
    operators: List[str],
    default: str,
) -> Tuple[str, bool]:
    """Resolve which operator to use.

    Returns (resolved, needs_selection):
      - provided (non-blank)        -> (provided, False)
      - exactly one operator        -> (that, False)
      - multiple operators          -> (default or first, True)   # caller may prompt
      - no operators                -> (default or "Operator", False)
    needs_selection=True signals an interactive caller (agent) SHOULD prompt the
    user to choose; non-interactive callers (the MCP server) just use `resolved`.
    """
    if provided and provided.strip():
        return provided.strip(), False
    if len(operators) == 1:
        return operators[0], False
    if len(operators) > 1:
        return (default or operators[0]), True
    return (default or "Operator"), False
```

**Step 4: Run, verify pass** (6 tests).

**Step 5: Commit**
```bash
git add MCP/operator_resolution.py MCP/__init__.py Orchestrator/tests/test_operator_resolution.py
git commit -m "feat(mcp): pure choose_operator() resolution helper + tests"
```

---

## Task 2: MCP server `resolve_operator()` + `get_current_operator` tool

**Files:**
- Modify: `MCP/blackbox_mcp_server.py` (add helper near top after `BLACKBOX_URL`; add tool handler in `call_tool`)
- Modify: `Orchestrator/tools/tool_registry.py` (`get_mcp_tools()` — add the `get_current_operator` tool schema)

**Step 1: Add the async resolver** to `MCP/blackbox_mcp_server.py` (after `BLACKBOX_URL = ...`):
```python
from MCP.operator_resolution import choose_operator  # if import path fails as a script, use: from operator_resolution import choose_operator

_OPERATOR_CACHE = {"operators": None, "default": None}

async def _fetch_operators():
    if _OPERATOR_CACHE["operators"] is None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{BLACKBOX_URL}/operators")
                data = r.json()
                _OPERATOR_CACHE["operators"] = list(data.get("operators") or [])
                _OPERATOR_CACHE["default"] = data.get("default") or ""
        except Exception:
            _OPERATOR_CACHE["operators"] = []
            _OPERATOR_CACHE["default"] = ""
    return _OPERATOR_CACHE["operators"], _OPERATOR_CACHE["default"]

async def resolve_operator(provided):
    """Resolve the operator for a tool call. Server-side safety net: when omitted,
    single operator -> that; multiple -> system default."""
    operators, default = await _fetch_operators()
    resolved, _needs = choose_operator(provided, operators, default)
    return resolved
```
> NOTE: `MCP/blackbox_mcp_server.py` runs as a script (`MCP/venv/bin/python .../blackbox_mcp_server.py`), so `from MCP.operator_resolution import ...` may not resolve (no package root on path). Confirm during implementation: if it fails, add the server's dir to `sys.path` and use `from operator_resolution import choose_operator`, OR set `PYTHONPATH`/`BLACKBOX_ROOT` (already in `.mcp.json` env) on `sys.path`. Pick whichever imports cleanly when run the real way; verify with the syntax/import check below.

**Step 2: Add `get_current_operator` handler** in `call_tool()` (a new `elif name == "get_current_operator":`):
```python
elif name == "get_current_operator":
    operators, default = await _fetch_operators()
    resolved, needs_selection = choose_operator(None, operators, default)
    result = {
        "resolved": resolved,
        "operators": operators,
        "default": default,
        "count": len(operators),
        "needs_selection": needs_selection,
    }
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
```

**Step 3: Register the tool schema** in `Orchestrator/tools/tool_registry.py get_mcp_tools()` (add an entry; match the existing dict shape):
```python
{
    "name": "get_current_operator",
    "description": "Resolve the BlackBox's current operator. Returns {resolved, operators, default, count, needs_selection}. When needs_selection is true (multiple operators, none specified), the agent should ask the user to pick one (default pre-selected). Use this instead of hard-coding any operator name.",
    "inputSchema": {"type": "object", "properties": {}, "required": []},
    "groups": _ALL,  # match the grouping convention used by neighboring tools
},
```
(Confirm the exact key names/grouping var by reading a neighboring entry; e.g. `list_operators`.)

**Step 4: Verify**
```bash
MCP/venv/bin/python -c "import ast; ast.parse(open('MCP/blackbox_mcp_server.py').read()); print('server ok')"
Orchestrator/venv/bin/python -c "import ast; ast.parse(open('Orchestrator/tools/tool_registry.py').read()); print('registry ok')"
# Import the server the real way to prove the operator_resolution import resolves:
cd MCP && venv/bin/python -c "import blackbox_mcp_server; print('import ok')"; cd ..
```
Expected: all ok. (The third proves the chosen import path works at runtime.)

**Step 5: Commit**
```bash
git add MCP/blackbox_mcp_server.py Orchestrator/tools/tool_registry.py
git commit -m "feat(mcp): resolve_operator() + get_current_operator tool (dynamic operator)"
```

---

## Task 3: Use `resolve_operator()` in all handlers

**Files:** Modify `MCP/blackbox_mcp_server.py` (`call_tool` handlers)

**Step 1:** Replace every `operator = arguments["operator"]` (14) and `operator = arguments.get("operator", "")` / `arguments.get("operator")` (2) with:
```python
operator = await resolve_operator(arguments.get("operator"))
```
This makes `operator` optional at runtime everywhere and resolves it when omitted. Do NOT change any other behavior. (For tools where operator is genuinely irrelevant, leave as-is — but all current sites pass it onward to the backend, so resolve them all.)

**Step 2: Verify**
```bash
grep -c 'arguments\["operator"\]' MCP/blackbox_mcp_server.py   # expect 0
cd MCP && venv/bin/python -c "import blackbox_mcp_server; print('import ok')"; cd ..
```

**Step 3: Commit**
```bash
git add MCP/blackbox_mcp_server.py
git commit -m "fix(mcp): resolve operator dynamically in all tool handlers (no required operator)"
```

---

## Task 4: Tool schema operator descriptions (de-imply Brandon)

**Files:** Modify `Orchestrator/tools/tool_registry.py`

**Step 1:** For each `operator` property in `get_mcp_tools()`, set the description to:
```
"Optional. If omitted, resolves to the BlackBox's current operator (single operator auto-selected; otherwise the system default). Do not hard-code an operator name."
```
(Leave `required` arrays unchanged — operator is already absent from them.)

**Step 2: Verify** `Orchestrator/venv/bin/python -c "import ast; ast.parse(open('Orchestrator/tools/tool_registry.py').read()); print('ok')"`. Also confirm no operator description still says "Brandon": `grep -i brandon Orchestrator/tools/tool_registry.py` → none.

**Step 3: Commit**
```bash
git add Orchestrator/tools/tool_registry.py
git commit -m "docs(mcp): operator params describe dynamic resolution, not Brandon"
```

---

## Task 5: Reusable agent-side resolution procedure (the helper for future skills)

**Files:** Create `.claude/commands/resolve-operator.md`

**Step 1:** Write a concise, reusable procedure doc that other commands/skills reference:
```markdown
# Resolve Operator (shared procedure)

Use this whenever a BlackBox action needs an operator. NEVER hard-code an operator name.

1. If the caller passed an explicit operator (slash arg / parameter), use it.
2. Otherwise call the `get_current_operator` MCP tool.
   - If `needs_selection` is false → use `resolved`.
   - If `needs_selection` is true (multiple operators) → present an AskUserQuestion
     dropdown of `operators` (pre-select `default`) and use the chosen one.
3. Pass the resolved operator to the BlackBox call.

This is the foundation other BlackBox skills build on — keep operator resolution here, one place.
```

**Step 2: Commit**
```bash
git add .claude/commands/resolve-operator.md
git commit -m "feat(skills): reusable resolve-operator procedure for BlackBox actions"
```

---

## Task 6: De-Brandon `snapshot-dev.md`

**Files:** Modify `.claude/commands/snapshot-dev.md`

**Step 1:** Remove the 5 hard-coded `Brandon` references. Replace the operator section + default behavior with: "Operator: if a slash arg is given, use it; otherwise follow the resolve-operator procedure (`.claude/commands/resolve-operator.md`) — single operator auto-selected, multiple → dropdown." Update the example `/chat/save` payload to show `"operator": "<resolved>"` with a note that it's the resolved operator, not a literal. Update the anti-pattern bullet ("Don't AskUserQuestion for the operator…") to: "Do AskUserQuestion ONLY when get_current_operator reports needs_selection."

**Step 2: Verify** `grep -c -i brandon .claude/commands/snapshot-dev.md` → 0 (or only in an illustrative "e.g. Brandon" aside if truly needed; prefer 0).

**Step 3: Commit**
```bash
git add .claude/commands/snapshot-dev.md
git commit -m "fix(skills): snapshot-dev resolves operator dynamically (no hard-coded Brandon)"
```

---

## Task 7: Update CLAUDE.md operator guidance

**Files:** Modify `CLAUDE.md`

**Step 1:** Find the operator guidance (e.g. "Operator default: `Brandon`" in the snapshot section). Replace with: "Operator: resolve dynamically via the resolve-operator procedure / `get_current_operator` (single auto; multiple → dropdown). Never hard-code an operator. (`Brandon` is only the system seed on an unconfigured box.)" Keep wording consistent with the new procedure.

**Step 2: Verify** the snapshot/MCP guidance no longer instructs hard-coding Brandon: re-read the edited sections.

**Step 3: Commit**
```bash
git add CLAUDE.md
git commit -m "docs: operator guidance points to dynamic resolution, not hard-coded Brandon"
```

---

## Task 8: Integration verification

**Step 1:** Pure tests green: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_operator_resolution.py -v`.
**Step 2:** `/operators` reachable + shape correct: `curl -s http://localhost:9091/operators | python3 -m json.tool`.
**Step 3:** Standalone driver proves end-to-end resolution against the live endpoint without the running session's MCP:
```bash
cd MCP && venv/bin/python -c "
import asyncio, blackbox_mcp_server as s
print(asyncio.run(s.resolve_operator(None)))      # -> the single operator or default
print(asyncio.run(s.resolve_operator('Anna')))    # -> Anna
"; cd ..
```
Expected: first prints the resolved current operator (e.g. "Brandon" on the seed box), second prints "Anna".
**Step 4:** Grep sweeps: no `arguments["operator"]` in the MCP server; no hard-coded "Brandon" operator default in `snapshot-dev.md`.
**Step 5:** Note for the user: live MCP `get_current_operator` + multi-operator dropdown is verified in a NEW Claude Code session (MCP reconnect). To exercise the dropdown, temporarily set `[users] list = Brandon, Anna` (or `DEFAULT_OPERATOR`/config) and run `/snapshot-dev` in a fresh session.

---

## Definition of done
- `choose_operator()` unit-tested (6 cases); `resolve_operator()` + `get_current_operator` added; all MCP handlers resolve operator dynamically (no `arguments["operator"]`).
- Tool schemas + CLAUDE.md + `snapshot-dev.md` describe dynamic resolution; no hard-coded "Brandon" operator default remains.
- Reusable `resolve-operator` procedure exists for future BlackBox skills.
- Single-operator box auto-resolves (incl. fresh Brandon-seed); multi-operator box drives a dropdown via `needs_selection`.

## Risks / notes
- **MCP import path:** `from MCP.operator_resolution import ...` may not resolve when the server runs as a bare script; Task 2 Step 1 + the `import blackbox_mcp_server` check settle the correct form (likely `from operator_resolution import choose_operator` with the script dir on `sys.path`).
- **MCP reconnect:** server edits apply on next session; don't claim live MCP behavior verified from within this session.
- **Cache staleness:** `_fetch_operators` caches per-process; a newly added operator appears on next MCP spawn. Acceptable (operators change rarely); note it.
- **Backend chat-tool path** (`chat_routes.py`) is separate and already per-request operator-scoped — intentionally out of scope.
