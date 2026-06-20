# Multi-Provider Web Search — Design

**Date:** 2026-06-20
**Status:** Validated (brainstorm complete, approved by Brandon). Next: implementation plan.
**Author:** Claude (Opus 4.8) + Brandon

## Goal

Replace the single generic `web_search` tool with **per-provider web-search tools**, each
auto-injected into the model's tool list only when its provider is both **configured (API key
present) AND enabled** for web search. The operator picks a **preferred default** in onboarding,
which becomes a system-prompt *hint* — the model may still choose any enabled engine (e.g. fire
Perplexity and live X/Twitter in parallel and cross-check). DuckDuckGo is the free, no-key floor.

## Why (motivation)

Today web search is Perplexity Sonar → DuckDuckGo fallback only (`Orchestrator/web_tools.py`).
DuckDuckGo is weak; there's no way to pick a stronger engine or to use provider-unique
capabilities (notably xAI's live X/Twitter search). Brandon wants production-grade, multi-provider
web search with the model able to choose and cross-check engines.

## Feasibility — spike results (2026-06-20)

All paid capabilities were probed live with the **existing** keys via
`diagnostics/websearch_spike.py`:

| Provider / capability | Result | Shape |
|---|---|---|
| Perplexity (sonar) | OK ~5s, 10 citations | synthesized answer + citation URLs (current default) |
| OpenAI `/v1/responses` `web_search` (gpt-4.1) | OK ~8.5s, 5 url_citations | synthesized + inline url citations |
| Gemini `google_search` grounding (2.5-flash) | OK ~14s, 14 grounding chunks | synthesized; citations are `vertexaisearch` **redirect** URLs |
| xAI Grok Agent Tools `web_search` (grok-4.3) | OK ~18s, 14 citations | synthesized + citations |
| xAI Grok Agent Tools `x_search` (grok-4.3) | OK ~8.6s, 3 citations | live X/Twitter, synthesized + citations |

**Key discovery:** xAI's old Live Search API (`search_parameters` on `/v1/chat/completions`) is
**deprecated → HTTP 410**. The current API is the **Agent Tools API**: `POST
https://api.x.ai/v1/responses`, model `grok-4.3`, body `input` + `tools:[{type:web_search}]` /
`[{type:x_search}]`, citations returned in a top-level `citations` field (OpenAI-Responses shape).

Keys present today: `PERPLEXITY_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`.
`GOOGLE_CSE_ID`/`CX` absent — and not needed (see Scope).

## Scope

**In (v1):** Perplexity, OpenAI, Gemini, Grok web, Grok X/Twitter, DuckDuckGo (free).

**Out:** Google *Custom Search* / Programmable Search — "Google web search" is satisfied by Gemini
grounding using the existing `GOOGLE_API_KEY` (no new `CX` credential, no raw-link reformatting).
Also out for v1: resolving Gemini's redirect citation URLs to final URLs; per-provider recency
normalization beyond each API's native support.

## Architecture

### 1. The six tools (provider-first naming, per the `provider-explicit-tool-naming` convention)

| Tool | Engine | Key gate |
|---|---|---|
| `perplexity_web_search` | Perplexity Sonar (`/chat/completions`, model `sonar`) | `PERPLEXITY_API_KEY` |
| `openai_web_search` | OpenAI Responses `web_search` (`/v1/responses`, gpt-4.1) | `OPENAI_API_KEY` |
| `gemini_web_search` | Gemini `google_search` grounding (`generativelanguage` REST) | `GOOGLE_API_KEY` |
| `grok_web_search` | xAI Grok Agent Tools `web_search` (`/v1/responses`, grok-4.3) | `XAI_API_KEY` |
| `grok_x_search` | xAI Grok Agent Tools `x_search` — live X/Twitter | `XAI_API_KEY` |
| `duckduckgo_web_search` | DuckDuckGo (`ddgs`) | none (free) |

All carry `query` (required) + `search_recency_filter` (where the provider supports it). Each is a
ToolVault module (`ToolVault/tools/<name>/{schema.json,executor.py}`), Tier-1, in the same 7 groups
the old `web_search` had: `chat`, `chat_cu`, `realtime`, `gemini_live`, `grok_live`, `phone`, `mcp`.
`grok_x_search` is the one genuinely distinct capability (live social), powering the cross-check use
case.

### 2. Provider adapters — `web_tools.py` refactor into 3 families

Collapse the providers into three adapter implementations behind a normalized result
`{answer: str, citations: list, source_label: str}`:

- **OpenAI-Responses family** — `POST {base}/v1/responses`, body `input` + `tools:[{type:...}]`,
  parse `output[]` message text + top-level/inline `citations`. Serves **both OpenAI and xAI**
  (base-URL + model swap; `web_search` vs `x_search` tool type). One implementation, three tools
  (`openai_web_search`, `grok_web_search`, `grok_x_search`).
- **Chat-completions family** — Perplexity (current code, lightly factored).
- **Gemini REST family** — `generativelanguage` `:generateContent` with `tools:[{google_search:{}}]`;
  parse `candidates[0].content.parts[].text` + `groundingMetadata.groundingChunks[].web`.
- **DuckDuckGo** — existing `_fallback_ddg_search`, promoted to a first-class adapter.

A shared formatter renders the existing human/LLM string contract (answer + "Sources:" list +
provenance line) so downstream consumers barely change. The existing caching + rate-limiting in
`web_tools.py` wrap all adapters, cache key includes the provider.

### 3. Presence-gating + enablement (net-new ToolVault capability)

ToolVault has **no availability gate today** (Tier-1 = always-on by group; Tier-2/3 = semantic).
Add an optional tool-level gate to `schema.json` — proposed **`"x-availability"`** — resolved by a
predicate registry that mirrors the existing `x-source` resolver pattern
(`Orchestrator/toolvault/resolvers.py`). The predicate returns true iff **key present AND provider
in `WEB_SEARCH_ENABLED`**. Both the injector (`injector.py`) and `get_mcp_tools()`
(`tools/tool_registry.py`) filter out unavailable tools.

Two new `.env` prefs (preference, not secret — like `STT_PROVIDER`):
- `WEB_SEARCH_ENABLED` — comma list of enabled providers (e.g. `perplexity,xai,duckduckgo`).
- `WEB_SEARCH_DEFAULT` — single preferred provider (the prompt hint).

**Lean-venv-safe:** the availability predicate must read `.env`/`config.ini` via stdlib
(`configparser`/`dotenv_values`), **never `import Orchestrator.config`** — same lesson as
`_list_operators` (`feedback-mcp-lean-venv`), so the MCP server gates correctly in its lean venv.

`duckduckgo_web_search` is **ungated by key** (no `x-availability` env requirement; still honors the
enabled list) — the structural floor guaranteeing no surface ever has zero web tools.

### 4. Onboarding step

New wizard step `web_search` appended to `ALL_STEPS` (`Orchestrator/onboarding/state.py`).
- Backend (`routes/onboarding_routes.py`): extend `/current-config` to report per-provider
  `{key present, enabled, default}` for web search; `/save` accepts `WEB_SEARCH_ENABLED` +
  `WEB_SEARCH_DEFAULT`. Validators for openai/google/xai/perplexity **already exist** (reuse);
  DuckDuckGo needs none (free).
- Frontend (`Portal/onboarding/onboarding.js` + step HTML): a step showing provider checkboxes
  (only those with keys, ≥1 required) + a "preferred default" radio. DuckDuckGo always selectable.
- **Surfaces:** Portal/desktop wizard only. The tools reach Android + WebView automatically via
  server-side tool injection — no Android onboarding UI needed. (3-surfaces rule applies to catalog
  contracts; this is backend-driven injection.)

### 5. Dispatch & migration

`web_search` is dispatched today by hand-written `if name == "web_search"` branches in ~13 sites.
Migration leverages the ToolVault catch-all we already added for `control_phone`:

- **Chat (`chat_routes.py`, ~8 sites):** the streaming dispatchers already route unknown
  `func_name` through `BlackBoxToolExecutor(operator).execute(...)`. Per-provider tools (ToolVault
  modules with executors) dispatch **automatically**. Remove the hand `web_search` branches + the
  `web_search` mentions in the system-prompt text (`chat_routes.py:3067,3126`).
- **Voice (`gemini_live_routes.py`, `realtime_routes.py`, `grok_live_routes.py`) + CU
  (`browser/driver_anthropic.py`):** these have explicit `web_search` branches and **no** generic
  catch-all. Add the same ToolVault catch-all (route unknown tool → `BlackBoxToolExecutor.execute`)
  so all six tools work there; remove the old `web_search` branch. (REUSABLE pattern per
  `project-control-phone`: new directly-callable ToolVault tools need a catch-all in each dispatch
  site.)
- **Delete** `ToolVault/tools/web_search/`; add the six new modules. Grep-sweep the frozen static
  fallback arrays (`BLACKBOX_TOOLS_*` / `CHAT_TOOLS_*`) + any other `web_search` literal. Note: the
  static arrays are import-time snapshots → require a restart (per CLAUDE.md ToolVault notes).

### 6. Default-provider hint

Injected wherever tool guidance is assembled (the tool-injection / context layer — **not**
persona/`behavioral_core.py`), built dynamically from the enabled set + default. Example:
*"Prefer `perplexity_web_search` for general web search; other engines are available for
cross-checking; use `grok_x_search` for live X/Twitter."* Changing the preferred engine is a
prompt-string swap, no code routing.

### 7. MCP + 3 surfaces

All six tools tagged with the `mcp` group → surfaced by `get_mcp_tools()` (availability-filtered via
the lean-venv-safe predicate). After landing: `POST /toolvault/reload` + `/mcp` reconnect. Android
and Portal receive the tools through normal chat tool injection — no client changes.

### 8. Citations / output contract

Keep the synthesized-answer + "Sources:" string. Normalize every adapter to a citations list.
**v1 caveat:** Gemini grounding citations are `vertexaisearch` redirect URLs, not final URLs —
accepted for v1; resolving them (follow redirects, adds latency) is a noted follow-up.

## Testing

- Live feasibility: `diagnostics/websearch_spike.py` (done — all providers pass).
- Unit tests: adapter normalization (mocked HTTP per family); availability-gate filtering across the
  key × enabled matrix incl. the DuckDuckGo floor; onboarding save/validate round-trip; injector
  include/exclude by availability; lean-venv gate read (no `import config`).
- Per-provider live smoke before ship; MCP `get_mcp_tools()` shows the right set after reload.

## Open items / follow-ups (non-blocking)

- Gemini redirect-URL resolution to final URLs.
- Potential voice/phone tool-list leanness (6 web tools may be many for latency-sensitive voice) —
  revisit if it bloats the voice tool list; could narrow voice to default + `grok_x_search`.
- Per-provider recency-filter normalization.

## Decisions locked in this brainstorm

1. **One tool per provider** (not a default-router) — model can cross-check engines in parallel.
2. **Replace** the generic `web_search` entirely (don't keep a router) — per-provider everywhere,
   default is a prompt hint only.
3. **Explicit enable + default** — tool gated on (key present AND in `WEB_SEARCH_ENABLED`); onboarding
   writes `WEB_SEARCH_ENABLED` + `WEB_SEARCH_DEFAULT`.
4. Provider-first tool names; `grok_*` for the xAI tools.
5. Default hint lives in the tool-injection/context layer, not persona.
