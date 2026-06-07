# ToolVault v2 — Authoring Guide

**Per-tool modules are the single source of truth.** Every tool the BlackBox can
call lives in its own folder under `ToolVault/tools/<name>/`. The chat injector,
the MCP server, and the static fallback arrays all *derive* from these modules —
there is no separate registry to keep in sync, no byte-offset volume, no
duplicated tool definitions. Edit a module on disk and every consumer picks it
up. (v1's `toolvault_volume.txt` / `toolvault_manifest.json` byte-offset
machinery is gone — see the design doc linked at the bottom.)

---

## Folder layout

```
ToolVault/
  tools/
    send_sms/
      schema.json        # canonical tool definition (pure JSON) — REQUIRED
      executor.py        # async def execute(params, ctx) -> ToolResult — optional*
      dynamic.py         # OPTIONAL escape hatch (see "Dynamic fields")
    search_snapshots/
      schema.json
      executor.py
    ...
  embeddings.json        # GENERATED cache: { name: {hash, model, vector:[3072]} }
  tools/README.md        # this file
```

\* `executor.py` is optional. A module with only `schema.json` is a valid
**schema-only tool** (see [Schema-only modules](#schema-only-modules)).

The folder name **must equal** `schema.json`'s `"name"` field — validation
rejects a mismatch.

`embeddings.json` is a **generated cache** and the *only* cached artifact in
ToolVault. Never hand-edit it; it is rebuilt from tool descriptions (see
[Embeddings](#embeddings-the-only-cache)).

---

## `schema.json` fields

The schema dict *is* the tool's canonical entry — it absorbs all the metadata
that used to live in the v1 manifest. Validated by
`Orchestrator/toolvault/schema_spec.py`.

### Required

| Field | Type | Notes |
|-------|------|-------|
| `name` | string | Must exactly match the folder name. |
| `description` | string | Non-empty. This is the text that gets embedded for semantic discovery — make it high-signal. |
| `category` | string | Non-empty free-form grouping label (e.g. `"communication"`, `"memory"`, `"media_generation"`). |
| `groups` | array of strings | Which consumer groups expose this tool's always-on (tier-1) slot. Each entry must be one of the [known groups](#groups). |
| `tier` | integer | One of `1`, `2`, `3`. See [Tiers](#tiers). |
| `parameters` | object | A JSON-Schema object: `{"type": "object", "properties": {...}, "required": [...]}`. `type` must be `"object"`; `properties` must be a dict; `required` (if present) must be a list. |

### Optional

| Field | Type | Notes |
|-------|------|-------|
| `executor` | string | **Executor alias** — the dispatch name when it differs from `name`. Must be a non-empty string when present. See [The `executor` alias](#the-executor-alias-field). |
| `returns` | string | Human-readable description of what the tool returns. |
| `example` | string | A one-line usage example, surfaced in the system-prompt instructions. |
| `notes` | string | Extra guidance, surfaced in the system-prompt instructions. |

Within `parameters.properties`, any property may carry an `"x-source"` marker
for [dynamic fields](#dynamic-fields-x-source). That marker is validated against
the registered resolver names and **stripped** before the schema reaches an LLM
provider.

### Minimal valid schema

```json
{
  "name": "send_sms",
  "description": "Send an SMS text message via the cellular gateway.",
  "category": "communication",
  "groups": ["chat", "realtime", "phone"],
  "tier": 2,
  "parameters": {
    "type": "object",
    "properties": {
      "phone_number": { "type": "string", "description": "E.164 number." },
      "message": { "type": "string", "description": "Plain-text body." }
    },
    "required": ["phone_number", "message"]
  }
}
```

---

## The `executor` alias field

By default a tool dispatches to the executor in its own folder under its own
`name`. When the canonical tool name and the internal executor name differ, set
the optional `"executor"` field to the executor name.

The live example is **`search_snapshots`**, whose internal/dispatch name is
`search_memory`:

```json
{
  "name": "search_snapshots",
  "...": "...",
  "executor": "search_memory"
}
```

Alias resolution is owned by `Orchestrator/toolvault/registry.py`:

- `_ALIASES` maps an alias → canonical name (`search_memory` → `search_snapshots`,
  `get_recent_snapshots` → `list_recent_snapshots`).
- `_EXECUTOR_NAMES` maps a canonical name → its executor name
  (`search_snapshots` → `search_memory`).

`registry.get_executor(name)` accepts either the alias or the canonical name,
resolves it via `resolve_alias()`, and loads the `execute` function from the
**canonical** folder's `executor.py`. So a model (or the MCP) can call
`search_memory` *or* `search_snapshots` and reach the same code in
`ToolVault/tools/search_snapshots/executor.py`.

---

## The `executor.py` contract

`executor.py` defines exactly one entry point:

```python
async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    ...
```

The registry's loader (`registry._validate_execute`) enforces:

- `execute` exists and is callable,
- it is an **`async def`** (coroutine function), and
- it accepts **exactly two positional parameters** (`params`, `ctx`).

A module that violates any of these is *excluded* and its error is surfaced via
`registry.load_errors()` / the validate CLI — it is never raised at runtime.

### `params`

A plain `dict` of the call arguments (the keys you declared under
`parameters.properties`). Always guard for missing/empty values — the model may
omit optional params.

### `ctx` — `ToolContext`

Defined in `Orchestrator/toolvault/context.py`:

```python
@dataclass
class ToolContext:
    operator: str = "system"
    base_url: str = "http://localhost:9091"
```

- `ctx.operator` — the resolved operator for this call (defaults to `"system"`).
- `ctx.base_url` — the Orchestrator base URL for any self-API calls.

### Return value — `ToolResult`

Also from `Orchestrator/toolvault/context.py`:

```python
@dataclass
class ToolResult:
    success: bool
    result: str                       # the string the model sees
    data: Optional[Dict[str, Any]] = None   # optional structured payload
```

Return `ToolResult(success, result, data=None)`. Use `success=False` with an
explanatory `result` string for error cases (don't raise). The optional `data`
dict is surfaced to the model via `rich_result()` when present.

> Import these from the toolvault package, not from `blackbox_tools`:
> `from Orchestrator.toolvault.context import ToolContext, ToolResult`.
> (`ToolResult` is defined canonically in `context.py` and only re-exported by
> `blackbox_tools` to avoid an import cycle.)

### Example `executor.py`

```python
"""Executor for send_sms."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    phone_number = params.get("phone_number", "")
    message = params.get("message", "")
    if not phone_number or not message:
        return ToolResult(False, "Phone number and message are required")

    # ... send the message ...

    return ToolResult(
        True,
        f"SMS sent to {phone_number}.",
        data={"to": phone_number},
    )
```

---

## Adding a new tool

1. **Create the folder + schema.** `ToolVault/tools/<name>/schema.json` with the
   required fields. The folder name and `"name"` must match.
2. **Add the executor** (unless it's a [schema-only tool](#schema-only-modules)):
   `ToolVault/tools/<name>/executor.py` with the `async def execute(params, ctx)`
   contract above.
3. **Validate.**
   ```bash
   python -m Orchestrator.toolvault.validate
   ```
   Exit code `0` means every module is valid; non-zero lists the offenders. Fix
   anything reported before relying on the tool.
4. **Reload** to embed the new description and bust the cache:
   ```bash
   curl -X POST http://localhost:9091/toolvault/reload
   ```

That's it — no restart needed. The registry's mtime cache picks up the new
`schema.json` automatically; `reload` additionally re-syncs `embeddings.json` so
the tool becomes semantically discoverable.

### Minimal new tool — full example

`ToolVault/tools/echo_message/schema.json`:

```json
{
  "name": "echo_message",
  "description": "Echo a message straight back. A trivial example tool.",
  "category": "utility",
  "groups": ["chat"],
  "tier": 2,
  "parameters": {
    "type": "object",
    "properties": {
      "text": {
        "type": "string",
        "description": "The text to echo back."
      }
    },
    "required": ["text"]
  },
  "example": "echo_message(text=\"hello\")"
}
```

`ToolVault/tools/echo_message/executor.py`:

```python
"""Executor for echo_message."""
from Orchestrator.toolvault.context import ToolContext, ToolResult


async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    text = params.get("text", "")
    if not text:
        return ToolResult(False, "text is required")
    return ToolResult(True, f"echo: {text}", data={"echoed": text})
```

This module passes `validate_module_dict` (all required keys present, `name`
matches the folder, `groups` ⊆ known groups, `tier` ∈ {1,2,3}, `parameters` is a
valid JSON-Schema object) and `execute` is a 2-arg `async def`.

---

## Dynamic fields (`x-source`)

A parameter whose allowed values are only known at runtime declares an
`"x-source"` marker instead of a hard-coded `enum`. At injection time a
registered **resolver** fills in the field and the marker is stripped (LLM
providers reject unknown JSON-Schema keys).

The live example is the `operator` field on memory tools:

```json
"operator": {
  "type": "string",
  "x-source": "operators",
  "description": "Optional. Omit to include ALL operators; pass a name to scope to one."
}
```

At injection time `"x-source": "operators"` becomes
`"enum": ["<live operator 1>", "<live operator 2>", ...]` and the `x-source` key
is removed. This happens uniformly in **chat injection**, the **MCP server**, and
the **static fallbacks** (all go through `resolvers.resolve_schema`).

### Registering a new resolver

Resolvers live in `Orchestrator/toolvault/resolvers.py`:

```python
def _resolve_operators(ctx: ToolContext) -> dict:
    operators = _list_operators(ctx)
    return {"enum": operators}   # dict of property overrides to merge in

RESOLVERS = {
    "operators": _resolve_operators,
}
KNOWN_SOURCES = set(RESOLVERS)
```

To add a new source:

1. Write a resolver `def _resolve_<name>(ctx: ToolContext) -> dict:` that returns
   the property overrides to merge (e.g. `{"enum": [...]}`, `{"default": ...}`).
2. Register it under its source name in the `RESOLVERS` dict.

`KNOWN_SOURCES` is derived from `RESOLVERS`, so schema validation will then accept
`"x-source": "<name>"` and the injector will resolve it. An unknown `x-source`
fails validation at build time (and is logged + left unresolved as a defensive
runtime fallback).

> The optional per-folder `dynamic.py` (`def resolve_schema(schema, ctx) -> dict`)
> is supported as an escape hatch by the design, but **registered resolvers are
> the normal path** — reach for `dynamic.py` only when a field's logic doesn't
> fit the shared resolver model.

---

## Tiers

`tier` controls *how* a tool reaches the model:

| Tier | Meaning |
|------|---------|
| **1** | **Always injected** for the requesting group — the always-on baseline. Filtered by `groups`. |
| **2** | **Semantic** — surfaced only when the user's prompt is semantically relevant (via the embeddings store). |
| **3** | **Internal** — not in the always-on set; reachable via global semantic discovery and direct dispatch. |

### Groups

`groups` lists which consumer surfaces get this tool in their **always-on (tier-1)
set**. Known groups (from `schema_spec.KNOWN_GROUPS`):

```
chat, chat_cu, realtime, gemini_live, grok_live, phone, mcp
```

### Group vs. semantic discovery

The group filter applies to **the tier-1 always-on set only**. Semantic search
spans the **entire catalog — all groups and all tiers** (tier-1, tier-2, *and*
tier-3 internal). So an out-of-group or internal tool can still surface for a
prompt when it's semantically relevant; `groups` only governs what's injected
unconditionally. (See `injector._select_names`.)

---

## Schema-only modules

A module with **no `executor.py`** is valid. These are tools whose execution
lives elsewhere — the canonical case is the **MCP-internal** tools (`groups`
includes `"mcp"`), which the MCP server itself handles. Validation reports a
missing `executor.py` as **not an error**; such tools show up under
`schema_only` in the validate report and `/toolvault/health`.

(If `executor.py` *exists* but fails to load — bad import, not `async`, wrong
arity — that *is* an error and the module is excluded.)

---

## Validate CLI + endpoints

### CLI (CI gate)

```bash
python -m Orchestrator.toolvault.validate
```

Sweeps every folder under `ToolVault/tools/`, validates each `schema.json`,
loads each existing `executor.py`, and reports embedding coverage. Prints a
human summary and **exits non-zero if anything is invalid** — wire it into CI.

### HTTP endpoints (`Orchestrator/routes/toolvault_routes.py`)

| Method & path | Purpose |
|---------------|---------|
| `GET /toolvault/health` | Lightweight status: tool count, schema-only set, load/validation errors, embedding coverage. Always returns 200 (use it for liveness). |
| `GET /toolvault/validate` | Full `validate_all()` report as JSON. Always HTTP 200 — the body's `ok` flag (and `errors` map) is the signal. |
| `POST /toolvault/reload` | Hot-reload: invalidate the registry schema + executor caches, then re-sync `embeddings.json` (only changed descriptions re-embed). No restart. This is the "edited a module → make it live" path. |

---

## Editing workflow (summary)

```
edit schema.json / executor.py
        │
        ├─ mtime cache picks up schema edits automatically (no restart)
        │
        ├─ python -m Orchestrator.toolvault.validate      # gate: exit 0 = clean
        │
        └─ POST /toolvault/reload                          # embed + bust caches
```

---

## Embeddings (the only cache)

`ToolVault/embeddings.json` caches one 3072-dim vector per tool, keyed by a
sha256 hash of the tool's **description**:

```json
{ "send_sms": { "hash": "<sha256>", "model": "...", "vector": [ ... ] } }
```

`sync_embeddings` (run by `POST /toolvault/reload` and at startup) re-embeds a
tool **only when its description hash changes**, prunes tools that no longer
exist, and writes the store atomically. It is a derived artifact — safe to
delete (it will be regenerated) and never to hand-edit.

---

## Reference

- Module layer: `Orchestrator/toolvault/{registry,resolvers,schema_spec,embeddings,injector,meta_tool,context,validate}.py`
- Routes: `Orchestrator/routes/toolvault_routes.py`
- Design doc: `docs/plans/2026-06-06-toolvault-v2-modules-design.md`
