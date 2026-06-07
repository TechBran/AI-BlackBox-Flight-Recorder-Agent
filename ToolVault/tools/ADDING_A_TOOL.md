# Adding a Tool to ToolVault — Agent Playbook

**Audience:** any CLI agent (Claude/Gemini/Codex) or developer adding a new tool to the BlackBox.
**This is the procedure.** For the full field reference, see `ToolVault/tools/README.md`. For why it's built this way, see `docs/plans/2026-06-06-toolvault-v2-modules-design.md`.

> A tool = one folder: `ToolVault/tools/<name>/` containing `schema.json` (what the model sees) and, if it does anything, `executor.py` (the logic). That's it. Everything else — chat injection, the MCP server, voice/phone, the model's `toolvault` discovery tool — derives from these modules automatically. There is **no central registry file to edit** and **no byte-offset anything**.

---

## TL;DR (the 4 steps)

```bash
# 1. Create the folder + schema.json (+ executor.py if it has logic)
mkdir -p ToolVault/tools/<name>

# 2. Validate (catches every structural mistake; exits non-zero on any)
Orchestrator/venv/bin/python -m Orchestrator.toolvault.validate

# 3. Make it live (re-embeds the new/changed tool + busts caches; no restart)
curl -X POST http://localhost:9091/toolvault/reload

# 4. Verify it's discoverable + executable (see "Verify" below)
```

The live chat injector also picks up edits automatically via the registry's mtime cache. `/toolvault/reload` additionally re-embeds (so semantic search finds it) and refreshes the MCP/registry-derived lists. A full `sudo systemctl restart blackbox.service` is only needed for the import-time phone/fallback arrays.

---

## Step 1 — `schema.json` (required)

This is the canonical, provider-agnostic tool definition. Format converters generate the correct shape for every provider (Anthropic / OpenAI / Gemini / Grok / MCP) from this one file.

```json
{
  "name": "roll_dice",
  "description": "Roll one or more dice and return the individual results and their sum. Use for random rolls, e.g. 'roll 2 twenty-sided dice'.",
  "category": "utility",
  "groups": ["chat", "chat_cu", "realtime", "gemini_live", "grok_live", "phone", "mcp"],
  "tier": 2,
  "parameters": {
    "type": "object",
    "properties": {
      "sides": { "type": "integer", "description": "Number of sides per die (2-1000). Default 6.", "default": 6 },
      "count": { "type": "integer", "description": "How many dice to roll (1-100). Default 1.", "default": 1 }
    },
    "required": []
  },
  "returns": "The individual rolls and their total.",
  "example": "roll_dice(sides=20, count=2)",
  "notes": "Optional free-text gotchas/tips."
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `name` | ✅ | MUST equal the folder name. Snake_case. |
| `description` | ✅ | The model reads this to decide when to call the tool, AND it's the embedding target for semantic discovery. Write it for a model: say what it does and when to use it. |
| `category` | ✅ | Free-text grouping (e.g. `utility`, `media_generation`, `email`). |
| `groups` | ✅ | Which surfaces the tool is *always-on* for (tier-1) — see Tiers below. Use all 7 unless there's a reason not to. Known: `chat, chat_cu, realtime, gemini_live, grok_live, phone, mcp`. |
| `tier` | ✅ | `1` always-injected (per group) · `2` semantic (the default for most tools) · `3` internal/MCP-only. |
| `parameters` | ✅ | A JSON-Schema object: `{"type":"object","properties":{...},"required":[...]}`. Mirror what the executor reads. |
| `executor` | optional | Only when the executor name differs from the tool name (alias). E.g. `search_snapshots` sets `"executor": "search_memory"`. |
| `returns` / `example` / `notes` | optional | Surfaced to the model via the system-prompt tool instructions + `toolvault(action='read')`. |

**Tiers & groups, decided simply:**
- Most tools → **tier 2** (discovered semantically when relevant). Reserve **tier 1** for a handful of always-needed tools (it costs tokens on every request in that group).
- **Semantic discovery is GLOBAL** — a tier-2 tool is reachable from any surface regardless of `groups`. `groups` only controls the always-on tier-1 set. So `groups` matters most for tier-1 tools.

---

## Step 2 — `executor.py` (required if the tool *does* something)

Contract — exactly this signature:

```python
"""Executor for roll_dice."""
import random
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    sides = int(params.get("sides", 6))     # read inputs with schema-matching defaults
    count = int(params.get("count", 1))
    if sides < 2 or sides > 1000:           # validate; fail gracefully (never raise)
        return ToolResult(success=False, result="sides must be between 2 and 1000")

    rolls = [random.randint(1, sides) for _ in range(count)]
    return ToolResult(
        success=True,
        result=f"Rolled {count}d{sides}: {rolls} (total {sum(rolls)})",  # human/model-readable
        data={"rolls": rolls, "total": sum(rolls)},                       # optional structured data
    )
```

Rules:
- `async def execute(params: dict, ctx: ToolContext) -> ToolResult` — **exactly 2 positional args**, must be `async`. The loader rejects anything else.
- Import `ToolContext, ToolResult` from `Orchestrator.toolvault.context` (NOT from `blackbox_tools`).
- `ctx.operator` and `ctx.base_url` are the only context fields. Use `ctx.operator` for operator-scoped work.
- Pull everything else via normal imports inside the file (e.g. `from Orchestrator.fossils import hybrid_retrieve`). Add the imports your body uses at the top of the file.
- Return `ToolResult(success, result, data=None)`. On any error, return `ToolResult(success=False, result="...")` — don't let exceptions escape (the dispatcher catches them, but a clear message is better).
- **Schema-only tools** (no `executor.py`) are valid — used for tools executed elsewhere (e.g. MCP-internal tools handled by the MCP server process). If your tool needs logic, it needs `executor.py`.

---

## Step 3 — Validate, then make it live

```bash
# Structural gate — run this BEFORE reloading. Exits non-zero + lists every problem.
Orchestrator/venv/bin/python -m Orchestrator.toolvault.validate

# Make it live: re-embed (so semantic search finds it) + bust caches. No restart.
curl -X POST http://localhost:9091/toolvault/reload
```

`validate` checks: valid JSON; `name == folder`; required fields; `parameters` is an object schema; `groups` ⊆ known; `tier` ∈ {1,2,3}; every `x-source` references a registered resolver; and that `executor.py` (if present) exposes a valid `async execute(params, ctx)`.

> **Production note:** the BlackBox runs live from this working tree. ALWAYS `validate` (and, for executors, `import Orchestrator.app`) before relying on a change — a broken module is excluded at runtime and surfaced in `/toolvault/health`, but a Python syntax error in an `executor.py` only bites when that tool is called.

---

## Step 4 — Verify (what we ran for `roll_dice`)

```bash
Orchestrator/venv/bin/python - <<'PY'
import asyncio
from Orchestrator.toolvault import registry, embeddings
from Orchestrator.tools.blackbox_tools import BlackBoxToolExecutor

print("in catalog:", any(t["name"]=="roll_dice" for t in registry.load_canonical()))
print("executor loads:", bool(registry.get_executor("roll_dice")), "errors:", registry.load_errors())
r = asyncio.run(BlackBoxToolExecutor(operator="Brandon").execute("roll_dice", {"sides":20,"count":2}))
print("dispatch:", r.success, r.result)
PY
```

Health/report endpoints: `GET /toolvault/health`, `GET /toolvault/validate`.

---

## Dynamic fields — fill an enum from live data (e.g. the operator list)

Mark a property with `"x-source": "<resolver>"`. At injection time the resolver fills its `enum`/`default` and the marker is stripped (so providers never see it). The `operators` resolver is built in:

```json
"operator": { "type": "string", "x-source": "operators",
              "description": "Operator to scope to. Omit for all." }
```

To add a NEW dynamic source, register a resolver in `Orchestrator/toolvault/resolvers.py`:

```python
RESOLVERS = {
    "operators": lambda ctx: {"enum": _list_operators(ctx)},
    "voices":    lambda ctx: {"enum": _list_voice_ids(ctx)},   # your new source
}
```

Then any schema can use `"x-source": "voices"`. (Validation will reject an `x-source` that isn't registered.)

---

## Aliases (executor name ≠ tool name)

If the model-facing tool name differs from the executor, set `"executor": "<exec_name>"` in `schema.json` and put the body in `ToolVault/tools/<tool_name>/executor.py`. Example: `search_snapshots` → `"executor": "search_memory"`. `registry.get_executor("search_memory")` and `get_executor("search_snapshots")` both resolve to the same module. (Alias map lives in `registry._ALIASES` / `_EXECUTOR_NAMES`.)

---

## Removing or renaming a tool

- **Remove:** delete the folder, then `validate` + `/toolvault/reload`. The embedding is pruned from `embeddings.json` on the next sync. (For phone/fallback arrays, restart.)
- **Rename:** rename the folder AND the `name` field together (they must match), then validate + reload.

---

## Gotchas (the ones that actually bite)

- **`name` must equal the folder name** — validate fails loudly otherwise.
- **Don't hand-edit `ToolVault/embeddings.json`** — it's a generated cache (hash-keyed on each description). Change the description, then reload/sync; it re-embeds only what changed.
- **Don't hardcode model names or API keys** in executors — read from `Orchestrator/config.py` / `os.getenv`.
- **`executor.py` imports**: a `NameError` from a missing import won't show at load time — only when the tool runs. Add every import the body uses.
- **Tier-1 is not free** — every tier-1 tool in a group is injected on every request for that surface. Default to tier 2.
- **Schema edits are live (mtime cache); embeddings + MCP/registry lists need `/toolvault/reload`; phone/fallback arrays need a restart.**

---

## Where the machinery lives

`Orchestrator/toolvault/` — `registry.py` (load/validate/cache + `get_executor`), `resolvers.py` (`x-source`), `schema_spec.py` (validation), `embeddings.py` (hash-keyed cache + search), `injector.py` (per-prompt selection + provider rendering), `meta_tool.py` (the `toolvault` discovery tool), `context.py` (`ToolContext`/`ToolResult`), `validate.py` (CLI). Endpoints: `Orchestrator/routes/toolvault_routes.py`.

**Reference example:** `ToolVault/tools/roll_dice/` is a complete, working minimal tool — copy its shape.
