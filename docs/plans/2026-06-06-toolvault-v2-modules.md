# ToolVault v2 — Modules-as-Source Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development) to implement this plan task-by-task.

**Goal:** Replace the byte-offset monolith with per-tool modules (`schema.json` + `executor.py`)
as the single source of truth, with runtime-resolved dynamic fields, embeddings as the only
cache, and the UGV tool surface removed.

**Architecture:** Per-tool folders under `ToolVault/tools/<name>/` are canonical. A `registry`
loads + validates them (in-memory, mtime-cached); `resolvers` fill `x-source` fields at injection
time; `embeddings.json` caches vectors (re-embedded only on description-hash change); the chat
injector, MCP server, static fallback, and `BlackBoxToolExecutor` dispatch all derive from the
registry. See `2026-06-06-toolvault-v2-modules-design.md`.

**Tech Stack:** Python 3.12, FastAPI, pytest, `gemini-embedding-001` (3072-dim), Gemini/OpenAI/
Anthropic tool-schema formats.

**Conventions:** TDD (red → green → commit). Frequent commits. Never `git add -A` — stage explicit
paths. Branch: `feat/toolvault-v2-modules`. Keep `TOOLVAULT_ENABLED=true`. The legacy fallback in
dispatch keeps the app runnable through the whole migration; v1 files are deleted only in Phase 6.

---

## Phase 0 — Contracts & scaffolding (no behavior change)

### Task 0.1: ToolContext + ToolResult re-export

**Files:**
- Create: `Orchestrator/toolvault/context.py`
- Test: `Orchestrator/toolvault/tests/test_context.py`

**Step 1 — failing test:** assert `ToolContext(operator="x", base_url="y")` exposes both fields,
defaults are `("system","http://localhost:9091")`, and `from Orchestrator.toolvault.context import
ToolResult` imports the same class object as `blackbox_tools.ToolResult`.

**Step 2 — run:** `pytest Orchestrator/toolvault/tests/test_context.py -v` → FAIL (no module).

**Step 3 — implement:** `@dataclass ToolContext(operator="system", base_url="http://localhost:9091")`;
`from Orchestrator.tools.blackbox_tools import ToolResult` re-export. (If circular, define
`ToolResult` in `context.py` and have `blackbox_tools` import it — verify no cycle.)

**Step 4 — run:** PASS.
**Step 5 — commit:** `feat(toolvault): ToolContext + ToolResult contract`

### Task 0.2: Module meta-schema + validator

**Files:**
- Create: `Orchestrator/toolvault/schema_spec.py`
- Test: `Orchestrator/toolvault/tests/test_schema_spec.py`

**Step 1 — failing tests:** `validate_module_dict(d, folder_name)` returns `[]` for a valid sample
and a non-empty error list for each of: missing `name`; `name != folder`; `parameters` not an
object schema; `tier` = 4; unknown `group`; `x-source` referencing an unregistered resolver.

**Step 2 — run:** FAIL.
**Step 3 — implement:** `KNOWN_GROUPS = {chat, chat_cu, realtime, gemini_live, grok_live, phone, mcp}`,
`VALID_TIERS = {1,2,3}`; checks per the design's Validation section. `x-source` validity defers to
a passed-in `known_sources` set (provided by `resolvers` in 0.3) to avoid import cycle.
**Step 4 — run:** PASS.
**Step 5 — commit:** `feat(toolvault): module schema validator`

### Task 0.3: Resolver registry + resolve_schema

**Files:**
- Create: `Orchestrator/toolvault/resolvers.py`
- Test: `Orchestrator/toolvault/tests/test_resolvers.py`

**Step 1 — failing tests:** with a fake `operators` resolver injected, `resolve_schema(schema, ctx)`
returns a NEW dict where the `x-source:"operators"` property gained `enum=[...]` and the original
schema is unmutated; a property with no `x-source` is untouched; unknown `x-source` raises/records.
`KNOWN_SOURCES` includes `"operators"`.

**Step 2 — run:** FAIL.
**Step 3 — implement:** `RESOLVERS = {"operators": lambda ctx: {"enum": _list_operators(ctx)}}`
(lazy-import the real operator list so tests can monkeypatch); `resolve_schema` deep-copies, walks
`properties`, applies `enum`/`default`/description-suffix from each resolver. Expose `KNOWN_SOURCES
= set(RESOLVERS)`.
**Step 4 — run:** PASS.
**Step 5 — commit:** `feat(toolvault): x-source resolver registry`

---

## Phase 1 — Registry (modules-as-source loader)

### Task 1.1: Load + validate modules into a cached canonical list

**Files:**
- Create: `Orchestrator/toolvault/registry.py`
- Test: `Orchestrator/toolvault/tests/test_registry.py` (uses a `tmp_path` fixture tools dir)

**Step 1 — failing tests:** point `registry` at a temp dir with 2 valid tool folders + 1 invalid;
`load_canonical()` returns the 2 valid tools (list of `{name, description, parameters, groups,
category, tier, ...}`), excludes the invalid one, and records it in `load_errors()`. Mutating a
`schema.json` mtime causes the next `load_canonical()` to reflect the change (cache invalidation).

**Step 2 — run:** FAIL.
**Step 3 — implement:** `TOOLS_DIR` (config-overridable for tests); `load_modules()` globs
`*/schema.json`, validates via `schema_spec` (+ `KNOWN_SOURCES`), caches result keyed by max-mtime
across `tools/**`; `load_canonical(group=None)`, `get_tool(name)`, `load_errors()`, `aliases()`.
Include the `_ALIASES`/`_EXECUTOR_NAMES` constants here.
**Step 4 — run:** PASS.
**Step 5 — commit:** `feat(toolvault): module registry with mtime cache`

### Task 1.2: Executor loader

**Files:**
- Modify: `Orchestrator/toolvault/registry.py`
- Test: add to `test_registry.py`

**Step 1 — failing tests:** `get_executor("good_tool")` returns a coroutine fn; calling it with
`({...}, ctx)` returns a `ToolResult`; `get_executor("schema_only_tool")` (no executor.py) returns
`None`; an executor.py with a wrong signature is reported in `load_errors()`.

**Step 2 — run:** FAIL.
**Step 3 — implement:** import `tools/<name>/executor.py` via `importlib`, cache the module, verify
`execute` is `async` with 2 params; expose `get_executor(name)`.
**Step 4 — run:** PASS.
**Step 5 — commit:** `feat(toolvault): per-tool executor loader`

---

## Phase 2 — Embeddings as the only cache

### Task 2.1: Hash-keyed embedding sync

**Files:**
- Modify: `Orchestrator/toolvault/embeddings.py`
- Create: (data) `ToolVault/embeddings.json` (generated)
- Test: `Orchestrator/toolvault/tests/test_embeddings_sync.py`

**Step 1 — failing tests (mock the embed call):** `sync_embeddings(canonical, store_path)` embeds
all tools on first run; a second run with unchanged descriptions makes **zero** embed calls; changing
one description re-embeds **only** that tool; `store` shape is `{name:{hash,model,vector}}`.

**Step 2 — run:** FAIL.
**Step 3 — implement:** `_emb_hash(text)` (sha256 of embedding-target), compare to stored hash,
re-embed on miss, atomic write. Reuse the existing embed client.
**Step 4 — run:** PASS.
**Step 5 — commit:** `feat(toolvault): hash-keyed embedding cache`

### Task 2.2: Search over the new store

**Files:**
- Modify: `Orchestrator/toolvault/embeddings.py`
- Test: add to `test_embeddings_sync.py`

**Step 1 — failing test:** `semantic_search(query_vec, store, limit)` and `hybrid_search(query,
canonical, store, limit, threshold)` return ranked `(name, score)` using vectors from the new
store (not the old manifest); a tool absent from the store is simply not returned.
**Step 2 — run:** FAIL.
**Step 3 — implement:** adapt the existing cosine/hybrid math to read vectors from the store dict.
**Step 4 — run:** PASS.
**Step 5 — commit:** `feat(toolvault): semantic search over embeddings.json`

---

## Phase 3 — Injector rewrite

### Task 3.1: inject_for_prompt over registry + resolvers + store

**Files:**
- Modify: `Orchestrator/toolvault/injector.py`
- Test: `Orchestrator/toolvault/tests/test_injector_v2.py`

**Step 1 — failing tests (fixture registry + store):** `inject_for_prompt(prompt, provider, group,
ctx)` always includes the meta-tool + tier-1 tools in the group; injects tier-2 tools above the
similarity threshold; respects the group filter; applies `resolve_schema` (operator enum filled);
output is correct provider format. **No UGV branch.**
**Step 2 — run:** FAIL.
**Step 3 — implement:** rewrite using `registry.load_canonical`, `resolvers.resolve_schema`,
`embeddings` search + the existing converters; **delete** the two UGV expansion blocks. Thread an
optional `ctx` (default `ToolContext()`); callers pass operator.
**Step 4 — run:** PASS.
**Step 5 — commit:** `refactor(toolvault): injector reads modules, drops byte-offset + UGV heuristic`

### Task 3.2: build_tool_instructions over registry

**Files:** Modify `injector.py`; Test add to `test_injector_v2.py`.
**Steps:** failing test (instructions text built from registry entries, meta-tool skipped) → run FAIL
→ implement → run PASS → commit `refactor(toolvault): tool instructions from registry`.

---

## Phase 4 — Collapse the dual source of truth

### Task 4.1: tool_registry sources from the registry

**Files:**
- Modify: `Orchestrator/tools/tool_registry.py`
- Test: `Orchestrator/tools/tests/test_registry_parity.py` (create)

**Step 1 — failing tests (PARITY — capture golden BEFORE this task):** snapshot current
`to_anthropic/to_openai_rest/to_openai_realtime/to_gemini_rest/to_gemini_live/to_mcp` output for a
fixed sample of non-UGV tools to a golden file; after rewiring, `get_*_tools(group)` for each
provider equals the golden output (minus UGV).
**Step 2 — run:** FAIL.
**Step 3 — implement:** replace the literal `TOOL_DEFINITIONS` with `registry.load_canonical()`
(lazy, cached); keep `_TOOL_INDEX`/`_ALIASES`/`_EXECUTOR_NAMES`/converters. Keep public fn
signatures identical.
**Step 4 — run:** PASS.
**Step 5 — commit:** `refactor(tools): tool_registry derives definitions from module registry`

### Task 4.2: MCP server from the registry

**Files:** Modify `MCP/blackbox_mcp_server.py` (or its `get_mcp_tools` source); Test
`MCP/tests/test_mcp_tools_parity.py`.
**Steps:** failing parity test (MCP tool names/schemas == v1 minus UGV; `resolve_schema` applied with
a server-side ctx) → FAIL → implement → PASS → commit `refactor(mcp): tools from module registry`.

### Task 4.3: Static fallback arrays from the registry

**Files:** Modify `Orchestrator/routes/chat_routes.py` (`CHAT_TOOLS_*`); Test add to parity suite.
**Steps:** failing test (fallback arrays == registry-derived) → FAIL → implement → PASS → commit
`refactor(chat): static fallback tools from registry`.

---

## Phase 5 — Executor dispatch façade

### Task 5.1: BlackBoxToolExecutor.execute dispatches to modules

**Files:**
- Modify: `Orchestrator/tools/blackbox_tools.py`
- Test: `Orchestrator/tools/tests/test_dispatch.py`

**Step 1 — failing tests:** with a fixture module executor registered, `execute("good_tool", {...})`
calls the module executor with a `ToolContext(self.operator, self.base_url)`; an alias
(`search_snapshots`) resolves correctly; a tool with no module executor falls back to legacy
`_execute_*`; unknown tool → `ToolResult(success=False)`.
**Step 2 — run:** FAIL.
**Step 3 — implement:** rewrite `execute()` per the design façade (module-first, legacy fallback).
Keep `resolve_alias`/`resolve_executor_name`.
**Step 4 — run:** PASS (legacy executors still present → fallback path exercised).
**Step 5 — commit:** `feat(tools): dispatch prefers module executors, legacy fallback`

---

## Phase 6 — Codegen, migrate all 47, UGV wipe, delete v1

### Task 6.1: Generate 47 schema.json modules

**Files:**
- Create: `scripts/toolvault_generate_modules.py`
- Create (generated): `ToolVault/tools/<name>/schema.json` ×47
- Test: `Orchestrator/toolvault/tests/test_migration_complete.py`

**Step 1 — failing test:** after running codegen, exactly 47 tool folders exist, all validate, zero
`ugv_*`, and each non-UGV name from the v1 manifest is present.
**Step 2 — implement codegen:** read pre-refactor `TOOL_DEFINITIONS` (name/params/groups) + vault
blocks (`returns`/`example`/`notes`) + `migrate.CATEGORY_MAP`/tier maps; write `schema.json` per
tool (set `executor`/aliases). Exclude `ugv_*`. Run it.
**Step 3 — run test:** PASS.
**Step 4 — commit:** `feat(toolvault): generate 47 tool schema modules` (stage `scripts/` +
`ToolVault/tools/**` explicitly).

### Task 6.2: Migrate all 47 executor bodies (batched by category)

**Files:** Create `ToolVault/tools/<name>/executor.py` ×47; Modify `blackbox_tools.py` (remove the
migrated `_execute_*`); Test `test_migration_complete.py` (extend).

Do in reviewable sub-batches by category (web, media_generation, media_management, memory,
communication, contacts, scheduling, computer_control, task_management, analysis, audio, email,
mcp_internal). For each batch:
- **Step A — failing test:** every tool in the batch has `get_executor(name)` returning a callable;
  a representative smoke per tool (mock externals) returns a `ToolResult`.
- **Step B:** move each `_execute_*` body into `tools/<name>/executor.py` as `async def
  execute(params, ctx)`; `self.operator→ctx.operator`, `self.base_url→ctx.base_url`; delete the
  method from `blackbox_tools.py`.
- **Step C — run:** PASS.
- **Step D — commit:** `feat(toolvault): migrate <category> executors to modules`

End-of-phase assertion: no `_execute_<nonugv>` remain in `blackbox_tools.py`; dispatch now always
hits module executors.

### Task 6.3: UGV wipe

**Files:** Modify `tool_registry.py` (remove `ugv_*` defs), `blackbox_tools.py` (remove
`_execute_ugv_*`, `_ugv_call`, `_ugv_er_call`, `UGV_*` consts), `injector.py` (already heuristic-free),
delete `ToolVault/tools/ugv_*` if any codegen leaked; Test add `test_no_ugv_surface.py`.

**Step 1 — failing test:** `registry.load_canonical()` has zero `ugv_*`; `get_anthropic_tools("chat")`
has zero `ugv_*`; `get_mcp_tools()` has zero `ugv_*`; no `_execute_ugv_*` attribute on the executor.
**Step 2 — implement:** delete all UGV code paths.
**Step 3 — run:** PASS.
**Step 4 — commit:** `chore(toolvault): remove UGV tool surface (revisit Beast later)`

### Task 6.4: Delete v1 byte-offset machinery

**Files:** Delete `Orchestrator/toolvault/volume.py`, `manifest.py`, `migrate.py`,
`ToolVault/toolvault_volume.txt`, `ToolVault/toolvault_manifest.json`; update
`Orchestrator/toolvault/__init__.py` exports; rewire `meta_tool.py` to the registry; remove the
legacy fallback branch in `blackbox_tools.execute()`.
- **Step 1 — failing test:** `grep` gate test asserts no import of `toolvault.volume`/`.manifest`/
  `.migrate` remains; `meta_tool` search/read/list works against the registry.
- **Step 2:** delete + rewire.
- **Step 3 — run full suite:** PASS.
- **Step 4 — commit:** `chore(toolvault): delete v1 volume/manifest/migrate (modules are source)`

---

## Phase 7 — Production hardening & admin

### Task 7.1: validate CLI + pytest gate
`python -m Orchestrator.toolvault.validate` → exit non-zero on any invalid module; a pytest test
runs it over the real `ToolVault/tools`. Commit `feat(toolvault): validation CLI + CI gate`.

### Task 7.2: Admin endpoints + startup sync
`GET /toolvault/health` (counts, load_errors, embedding coverage), `GET /toolvault/validate`,
`POST /toolvault/reload` (bust caches + `sync_embeddings`). Add a startup hook that runs
`sync_embeddings` once. Tests for each. Commit `feat(toolvault): health/validate/reload endpoints`.

### Task 7.3: Docs + memory
Update `CLAUDE.md` (ToolVault section → "edit a module, hit /reload"), refresh the
`toolvault_architecture` memory file, add a `ToolVault/tools/README.md` describing the module
format + `x-source`. Commit `docs(toolvault): v2 module authoring guide`.

---

## Phase 8 — Final review & verification

- Run the full pytest suite; run the `validate` CLI; restart the service; confirm
  `[TOOLVAULT-INJECT]` logs show module-sourced tools and `[EMBEDDING]` sync succeeded.
- End-to-end smoke: a chat prompt injects the right tools; the model calls one; an `x-source:
  operators` field shows the live operator list; a deliberate `schema.json` edit + `/toolvault/reload`
  takes effect with no restart.
- Final code review (superpowers:requesting-code-review) over the whole branch; then
  superpowers:finishing-a-development-branch (merge `--no-ff` to main + push) — **on Brandon's go.**
- `/snapshot-dev` to record the landing.

## Definition of done

47 module tools (schema + executor), zero UGV in the surface, no byte-offset code, single source of
truth (registry) feeding chat/MCP/fallback/dispatch, embeddings hash-cached, dynamic `operator`
field resolving live, all tests green, validate CLI clean, edit→reload works without restart.
