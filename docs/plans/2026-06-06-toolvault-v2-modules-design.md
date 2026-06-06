# ToolVault v2 — Modules-as-Source Design

**Date:** 2026-06-06
**Status:** Approved design (brainstorm complete) → implementation plan in `2026-06-06-toolvault-v2-modules.md`
**Author:** Brandon + Claude (Opus 4.8)

## Problem

ToolVault v1 stores all tools in a single append-only `toolvault_volume.txt`, addressed by
byte offsets recorded in `toolvault_manifest.json` (which also holds the 3072-dim embeddings —
the source of its 4.5 MB size). This design makes **editing a tool schema dangerous and the
system fragile**:

1. **No update path.** The write API is `mint_tool()` (append), `update_tool_embedding()`, and
   `rebuild_index()`. There is no `update_tool`/`edit_tool`/`delete_tool`.
2. **Editing corrupts the index silently.** A schema is a single line *inside* a byte-offset
   block. Changing its length shifts `byte_start`/`byte_end` for every *subsequent* tool; the
   manifest then points at garbled byte ranges with no error.
3. **The only re-sync path destroys metadata.** `rebuild_index()` rescans anchors but resets
   `category`, `groups`, `tier`, and **`embedding` → null**, killing semantic search + tiering.
4. **Dual source of truth.** `tool_registry.py` (`TOOL_DEFINITIONS`, 1404 lines) is canonical
   for the MCP server + static fallback; the vault is a *migrated copy*. `migrate_all()` *skips*
   tools already in the vault, so the two drift silently. (They already have — `generate_image`
   in `tool_registry.py` still advertises the retired `gemini-3-pro-image-preview`.)

Brandon's upcoming roadmap requires editing tool schemas (and the **logic behind them**, e.g.
populating an `operator` enum from the live operator list) frequently. The monolith blocks that.

## Goal

Make ToolVault **bulletproof and edit-friendly for production**: each tool is a self-contained
module (JSON schema + Python executor) that can be edited freely, with no byte-offset machinery,
no dual source of truth, and runtime-resolved dynamic fields.

## Approved decisions (brainstorm)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Module shape | **Per-tool folder**: `schema.json` + `executor.py` (+ optional `dynamic.py`) |
| 2 | Dynamic fields | **Declarative marker + registered resolver** (`"x-source": "operators"`) |
| 3 | Dual source | **Modules become the ONE source**; MCP + fallback + chat all derive from it |
| 4 | Runtime source | **Read modules directly at runtime** — no compiled `volume.txt`; embeddings the only cache |
| 5 | Executor logic | **Move ALL 47 executor bodies into per-tool `executor.py` now** (fully modular) |
| 6 | UGV wipe | **Delete UGV entirely** — schemas, `_execute_ugv_*`, the Tailscale proxy, and the injector heuristic (revisit the Beast later from git history) |

## Architecture

### Inversion of control

v1: the *volume* is canonical; manifest indexes it; the vault is a copy of `tool_registry`.
v2: the **per-tool folder is the only source of truth.** Everything else is *derived*:

```
ToolVault/tools/<name>/{schema.json, executor.py}      ← THE source
            │
            ├─ registry.load_canonical()   → canonical tool list (replaces TOOL_DEFINITIONS)
            │        ├─ chat injector (semantic, per-prompt)
            │        ├─ MCP server  (get_mcp_tools → all tools)
            │        └─ static fallback arrays
            ├─ executors (get_executor)     → BlackBoxToolExecutor dispatch
            └─ embeddings.json (cache)       → semantic search vectors (only generated artifact)
```

Because nothing is addressed by byte offset, the v1 corruption class **cannot exist**. Because
every consumer derives from the modules, **drift is structurally impossible**.

### On-disk layout

```
ToolVault/
  tools/
    send_sms/
      schema.json        # canonical, pure JSON — editable by hand or API
      executor.py        # async def execute(params: dict, ctx: ToolContext) -> ToolResult
      dynamic.py         # OPTIONAL: def resolve_schema(schema, ctx) -> dict  (escape hatch)
    search_snapshots/
      schema.json
      executor.py
    ... (47 tool folders)
  embeddings.json        # { name: {hash, model, vector:[3072]} }  ← ONLY cache artifact
  # DELETED: toolvault_volume.txt, toolvault_manifest.json
```

### `schema.json` format

Self-contained — absorbs the metadata that lived in the v1 manifest:

```json
{
  "name": "search_snapshots",
  "description": "Search the BlackBox memory ...",
  "category": "memory",
  "groups": ["chat", "chat_cu", "phone", "mcp"],
  "tier": 1,
  "executor": "search_memory",
  "parameters": {
    "type": "object",
    "properties": {
      "query": { "type": "string", "description": "..." },
      "operator": { "type": "string", "x-source": "operators",
                    "description": "Operator whose memory to search" }
    },
    "required": ["query"]
  },
  "returns": "List of matching snapshots",
  "example": "search_snapshots(query=\"...\")",
  "notes": ""
}
```

- `tier`: 1 = always injected, 2 = semantic retrieval, 3 = MCP-internal/approval-gated.
- `executor` (optional): executor name when it differs from `name` (the v1 `_EXECUTOR_NAMES`
  alias). Absent → executor name == tool name.
- `x-source` on any property → resolved at injection time by a registered resolver.
- Aliases (the v1 `_ALIASES`, e.g. `search_memory`→`search_snapshots`) live in a small constant
  in `registry.py` (only two entries; not worth a per-module field).

### Dynamic field resolution (`resolvers.py`)

One shared function used by **every** surface so resolution is identical:

```python
RESOLVERS = {
    "operators": lambda ctx: {"enum": list_operators()},     # fill enum from live list
    # future: "voices", "devices", "models", ...
}

def resolve_schema(schema: dict, ctx: ToolContext) -> dict:
    """Deep-copy schema, fill any x-source property from its resolver. Pure; never mutates source."""
```

`resolve_schema` is applied just before format conversion in the injector, in `get_mcp_tools`,
and in the fallback builder. Unknown `x-source` → validation error (caught at build/test time).
A tool needing logic beyond a simple enum/default uses its optional `dynamic.py::resolve_schema`.

### Executor contract (`context.py`, `executors.py`)

```python
@dataclass
class ToolContext:
    operator: str = "system"
    base_url: str = "http://localhost:9091"

# tools/<name>/executor.py
async def execute(params: dict, ctx: ToolContext) -> ToolResult: ...
```

`ToolResult` stays defined in `blackbox_tools.py` and is re-exported from `toolvault.context`
to avoid a circular import. Migrating a v1 `_execute_*` body is mechanical: `self.operator →
ctx.operator`, `self.base_url → ctx.base_url`; local imports unchanged.

### Dispatch façade (no caller changes)

`BlackBoxToolExecutor(operator=…).execute(name, args)` stays the public interface (~20 call
sites in phone/voice/gmail routes). Only its internals change:

```python
async def execute(self, tool_name, tool_input) -> ToolResult:
    canonical = resolve_alias(tool_name)
    ex = get_executor(canonical)                       # module executor from registry
    if ex is not None:
        return await ex(tool_input, ToolContext(self.operator, self.base_url))
    handler = getattr(self, f"_execute_{resolve_executor_name(canonical)}", None)  # legacy fallback
    ...
```

The legacy fallback exists only during migration; once all 47 are modules, the `_execute_*`
bodies and the fallback are deleted.

### Runtime caching (no build step)

`registry.load_modules()` loads + validates all modules into an in-memory cache, invalidated by
a **cheap max-mtime check** across `tools/**` (≈50 `stat()` calls, sub-millisecond) so on-disk
edits are picked up with no restart. The executor `.py` modules are imported once and cached
(reload endpoint busts both caches).

Embeddings are the only thing requiring the (paid, networked) embedding API. `sync_embeddings()`
hashes each tool's embedding-target text and re-embeds **only** tools whose hash changed,
persisting to `embeddings.json`. It runs at startup and on `/toolvault/reload`; missing vectors
are embedded lazily/best-effort on first search.

### Validation (production gate)

`schema_spec.py::validate_module(folder)` checks: valid JSON; `name` == folder name; required
keys present; `parameters` is a valid JSON Schema object; `groups` ⊆ known groups; `tier` ∈
{1,2,3}; every `x-source` references a registered resolver; `executor.py` importable with an
`async execute(params, ctx)` signature. A pytest gate + a `validate` CLI fail loudly on any bad
module. At runtime, an invalid module is excluded + surfaced in `/toolvault/health` (never a
silent skip).

## Consumer touch-points (the blast radius)

| Consumer | v1 | v2 |
|----------|----|----|
| `chat_routes._get_tools/_get_system_prompt` | injector (vault) | injector (registry) — unchanged call shape |
| `tasks.py` task execution | injector (vault) | injector (registry) |
| `blackbox_tools.execute()` | `getattr(_execute_*)` | module executor + legacy fallback → modules only |
| `tool_registry.TOOL_DEFINITIONS` | static 1404-line list | `registry.load_canonical()` (converters stay) |
| `MCP/blackbox_mcp_server.get_mcp_tools` | from `tool_registry` | from registry (+ `resolve_schema`) |
| `chat_routes` static fallback arrays | from `tool_registry` | from registry |
| `meta_tool` (the `toolvault` tool) | vault read | registry read |

## Migration strategy

1. **Codegen** (`scripts/toolvault_generate_modules.py`, kept for record): for each of the 47
   non-UGV tools, write `schema.json` from `TOOL_DEFINITIONS` (canonical params + groups),
   enriched with `returns`/`example`/`notes` from the existing vault blocks and `category`/`tier`
   from `migrate.py`'s maps; set `executor`/aliases from `_EXECUTOR_NAMES`/`_ALIASES`.
2. **Executors**: move all 47 `_execute_*` bodies into `tools/<name>/executor.py` (mechanical
   `self.`→`ctx.` rewrite), grouped by category for reviewable batches.
3. **UGV wipe**: delete `ugv_*` from `tool_registry`, the 22 vault entries, ~24 `_execute_ugv_*`
   methods, the `_ugv_call`/`_ugv_er_call` proxies, and the injector expansion heuristic.
4. **Delete v1**: `volume.py`, `manifest.py`, `migrate.py`, `toolvault_volume.txt`,
   `toolvault_manifest.json`, and the legacy `_execute_*` fallback.

## Testing strategy (TDD)

- Schema validation (valid/invalid fixtures); resolver fill (operators → enum); embedding cache
  (hash skip vs re-embed, mocked API); registry load + mtime cache; executor import + signature.
- **Parity tests** (the safety net): module→Anthropic/OpenAI/Gemini/MCP output equals the v1
  converter output for sampled tools; injector selection (tier1 always, tier2 semantic, group
  filter) matches v1 semantics; MCP tool count/names/schemas match v1 minus UGV.
- Migration completeness: all 47 present, schemas valid, executors resolvable, zero `ugv_*` in
  the surface; end-to-end smoke (chat injects → model calls a tool → operator resolver fills).

## Out of scope

- UGV Beast tool surface (rebuilt later, from git history).
- An API-driven schema *editor* UI (file edit + `/toolvault/reload` is sufficient for now).
- Tier-3 self-minting of new tools by the model (future).

## Risks & mitigations

- **47-executor migration is large** → mechanical `self.`→`ctx.` rewrite, batched by category,
  each batch behind parity/smoke tests; legacy fallback keeps the system runnable mid-migration.
- **Embedding cost on first run** → hash-keyed cache; one-time embed of 47 descriptions, then
  only on change.
- **Per-prompt module reads** → in-memory cache + cheap mtime check; vectors preloaded.
- **Hidden consumer of the old vault API** → grep gate in the final review; `meta_tool` rewired.
