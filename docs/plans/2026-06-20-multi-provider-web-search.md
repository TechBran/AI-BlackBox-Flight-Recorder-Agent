# Multi-Provider Web Search Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task (Brandon's choice). Build on `main` (staging-as-prod — NO worktrees/branches). Stage explicit paths only, never `git add -A`. Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` and use `-F -` heredocs (no backticks in `-m`). GitHub push = ship AFTER device/live validation.

**Goal:** Replace the single `web_search` tool with six per-provider web-search tools (Perplexity, OpenAI, Gemini, Grok-web, Grok-X/Twitter, DuckDuckGo), auto-injected only when (key present AND provider enabled), with an onboarding-selected default exposed as a system-prompt hint.

**Architecture:** A provider-adapter layer in `web_tools.py` (3 families behind a normalized result) + a net-new ToolVault availability gate (`x-availability` predicate, filtered at the chat injector, the voice/`get_tools_by_group` path, and `get_mcp_tools`) + two `.env` prefs (`WEB_SEARCH_ENABLED`, `WEB_SEARCH_DEFAULT`) + an onboarding step + dispatch migration that reuses the existing `control_phone` ToolVault catch-all.

**Tech Stack:** Python/FastAPI backend, `requests`, ToolVault v2 modules (`schema.json`+`executor.py`), pytest, vanilla-JS Portal onboarding.

**Design doc:** `docs/plans/2026-06-20-multi-provider-web-search-design.md`
**Spike (provider probe, already passing):** `diagnostics/websearch_spike.py`

**Run tests with the orchestrator venv:** `Orchestrator/venv/bin/python -m pytest <path> -v`

---

## Task 1: Provider-adapter layer in `web_tools.py`

Refactor `Orchestrator/web_tools.py` so each provider is an adapter returning a normalized result, behind one dispatcher `perform_provider_search(provider, query, ...)`. Keep the existing caching, rate-limiting, and output-string contract.

**Files:**
- Modify: `Orchestrator/web_tools.py`
- Test: `Orchestrator/tests/test_web_search_adapters.py` (create)

**Step 1: Write failing tests** (`Orchestrator/tests/test_web_search_adapters.py`)

```python
import types
import Orchestrator.web_tools as wt
from Orchestrator.web_tools import SearchResult, _format_search_result, perform_provider_search


def test_format_search_result_includes_answer_and_citations():
    r = SearchResult(answer="Hello world", citations=["https://a.com", "https://b.com"],
                     source_label="TestEngine")
    out = _format_search_result(r, "q")
    assert "Hello world" in out
    assert "https://a.com" in out and "https://b.com" in out
    assert "TestEngine" in out


def test_perform_provider_search_unknown_provider_returns_error_string():
    out = perform_provider_search("nope", "q", use_cache=False)
    assert "unknown" in out.lower() or "unsupported" in out.lower()


def test_perform_provider_search_dispatches_to_adapter(monkeypatch):
    called = {}
    def fake(query, recency):
        called["q"] = query
        return SearchResult(answer="A", citations=["u"], source_label="Perplexity Sonar")
    monkeypatch.setitem(wt.PROVIDER_SEARCHERS, "perplexity", fake)
    out = perform_provider_search("perplexity", "weather", use_cache=False)
    assert called["q"] == "weather"
    assert "A" in out and "Perplexity Sonar" in out


def test_responses_family_parses_output_and_citations(monkeypatch):
    # OpenAI/xAI Responses shape: output[].message.content[].text + top-level citations
    payload = {"output": [{"type": "message", "content": [
        {"type": "output_text", "text": "Synth answer"}]}],
        "citations": ["https://x.com/1"]}
    class FakeResp:
        status_code = 200
        text = ""
        def json(self): return payload
        def raise_for_status(self): pass
    monkeypatch.setattr(wt.requests, "post", lambda *a, **k: FakeResp())
    r = wt._responses_search("https://api.x.ai", "KEY", "grok-4.3", "x_search", "q")
    assert r.answer == "Synth answer"
    assert r.citations == ["https://x.com/1"]


def test_gemini_adapter_parses_grounding(monkeypatch):
    payload = {"candidates": [{"content": {"parts": [{"text": "Gem answer"}]},
        "groundingMetadata": {"groundingChunks": [
            {"web": {"uri": "https://redirect/1", "title": "t"}}]}}]}
    class FakeResp:
        status_code = 200
        text = ""
        def json(self): return payload
    monkeypatch.setattr(wt.requests, "post", lambda *a, **k: FakeResp())
    r = wt._search_gemini("q", "month")
    assert "Gem answer" in r.answer
    assert any("redirect" in c for c in r.citations)
```

**Step 2: Run, verify they fail** — `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_web_search_adapters.py -v` → ImportError/AttributeError.

**Step 3: Implement** in `Orchestrator/web_tools.py`:

```python
from dataclasses import dataclass, field

@dataclass
class SearchResult:
    answer: str = ""
    citations: list = field(default_factory=list)
    source_label: str = ""
    ok: bool = True
    error: str = ""

# --- config (reuse existing PERPLEXITY_* import block; add the others) ---
try:
    from Orchestrator.config import (PERPLEXITY_API_KEY, PERPLEXITY_URL,
        OPENAI_API_KEY, XAI_API_KEY, GEMINI_API_KEY)
except ImportError:
    # MCP/lean fallback not needed here — web_tools runs in the full backend.
    OPENAI_API_KEY = XAI_API_KEY = GEMINI_API_KEY = ""

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"
OPENAI_SEARCH_MODEL = "gpt-4.1"
XAI_SEARCH_MODEL = "grok-4.3"
GEMINI_SEARCH_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash"]

def _format_search_result(r: SearchResult, query: str) -> str:
    if not r.ok:
        return f"Web search failed ({r.source_label}): {r.error}\nAnswer from your own knowledge if possible."
    out = f"Web Search Results:\n\n{r.answer}\n"
    if r.citations:
        out += "\nSources:\n"
        for i, c in enumerate(r.citations, 1):
            url = c if isinstance(c, str) else (c.get("url") or c.get("uri") or str(c))
            out += f"  {i}. {url}\n"
    out += f"\nSource: {r.source_label} | Query: \"{query}\""
    return out

def _responses_search(base_url, api_key, model, tool_type, query) -> SearchResult:
    """OpenAI-Responses family — serves OpenAI + xAI (web_search / x_search)."""
    label = {"web_search": "OpenAI" if "openai" in base_url else "Grok",
             "x_search": "Grok (X/Twitter)"}.get(tool_type, "Web")
    try:
        resp = requests.post(f"{base_url}/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "input": [{"role": "user", "content": query}],
                  "tools": [{"type": tool_type}]}, timeout=90)
        if resp.status_code >= 400:
            return SearchResult(ok=False, source_label=label, error=f"HTTP {resp.status_code}")
        d = resp.json()
        text = d.get("output_text") if isinstance(d.get("output_text"), str) else ""
        for item in d.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") in ("output_text", "text"):
                        text += c.get("text", "")
        return SearchResult(answer=text, citations=list(d.get("citations") or []), source_label=label)
    except Exception as e:
        return SearchResult(ok=False, source_label=label, error=f"{type(e).__name__}: {e}")
# NOTE: base_url here is the host root (https://api.x.ai); the f-string appends /v1/responses.
# Pass base_url="https://api.openai.com" / "https://api.x.ai".

def _search_perplexity(query, recency) -> SearchResult: ...   # wrap existing Perplexity call → SearchResult
def _search_openai(query, recency) -> SearchResult:
    return _responses_search("https://api.openai.com", OPENAI_API_KEY, OPENAI_SEARCH_MODEL, "web_search", query)
def _search_grok_web(query, recency) -> SearchResult:
    return _responses_search("https://api.x.ai", XAI_API_KEY, XAI_SEARCH_MODEL, "web_search", query)
def _search_grok_x(query, recency) -> SearchResult:
    return _responses_search("https://api.x.ai", XAI_API_KEY, XAI_SEARCH_MODEL, "x_search", query)
def _search_gemini(query, recency) -> SearchResult: ...       # generativelanguage google_search, try models in order
def _search_duckduckgo(query, recency) -> SearchResult: ...   # wrap existing _fallback_ddg_search → SearchResult

PROVIDER_SEARCHERS = {
    "perplexity": _search_perplexity, "openai": _search_openai,
    "gemini": _search_gemini, "grok": _search_grok_web,
    "grok_x": _search_grok_x, "duckduckgo": _search_duckduckgo,
}

def perform_provider_search(provider, query, search_recency_filter="month", use_cache=True) -> str:
    if not query:
        return "Search query is required."
    fn = PROVIDER_SEARCHERS.get(provider)
    if fn is None:
        return f"Unknown web-search provider: {provider!r}"
    cache_key = f"search:{provider}:{query}:{search_recency_filter}"
    if use_cache:
        hit = _get_cache(cache_key)
        if hit: return hit
    if not _check_rate_limit():
        return "Rate limit exceeded. Please try again in a moment."
    r = fn(query, search_recency_filter)
    out = _format_search_result(r, query)
    if r.ok and use_cache:
        _set_cache(cache_key, out, SEARCH_CACHE_TTL)
    return out
```

Keep the legacy `perform_web_search()` as a thin alias → `perform_provider_search("perplexity", ...)` for the duration of the migration (removed in Task 4 once no caller remains). Reuse `_get_cache/_set_cache/_check_rate_limit/_fallback_ddg_search`.

**Step 4: Run tests, verify pass.**

**Step 5: Live smoke** (DO NOT commit secrets; keys read from `.env`): `Orchestrator/venv/bin/python diagnostics/websearch_spike.py` still passes (adapters not yet wired, but the spike validates the upstream APIs unchanged).

**Step 6: Commit** — `git add Orchestrator/web_tools.py Orchestrator/tests/test_web_search_adapters.py` → `feat(web-search): provider-adapter layer (perplexity/openai/gemini/grok/grok_x/ddg)`.

---

## Task 2: ToolVault availability gate (`x-availability`) + enabled-pref reader

Add presence-gating so a tool is injected only when available. New module + filters at the three consumer sites. Reader MUST be lean-venv-safe (stdlib only — see `feedback-mcp-lean-venv`).

**Files:**
- Create: `Orchestrator/toolvault/availability.py`
- Modify: `Orchestrator/toolvault/injector.py` (`_select_names`, ~line 195 & ~203 — filter candidates)
- Modify: `Orchestrator/tools/tool_registry.py` (`get_tools_by_group` ~line 112; `get_mcp_tools` ~line 287)
- Modify: `Orchestrator/toolvault/schema_spec.py` (allow optional `x-availability` field in validation)
- Test: `Orchestrator/toolvault/tests/test_availability.py` (create)

**Step 1: Write failing tests:**

```python
from Orchestrator.toolvault import availability as av

def test_no_gate_means_available():
    assert av.is_available({"name": "roll_dice"}, enabled=set(), env={}) is True

def test_gate_requires_env_present():
    entry = {"name": "grok_web_search",
             "x-availability": {"provider": "grok", "requires_env": ["XAI_API_KEY"]}}
    assert av.is_available(entry, enabled={"grok"}, env={"XAI_API_KEY": "k"}) is True
    assert av.is_available(entry, enabled={"grok"}, env={}) is False           # key missing
    assert av.is_available(entry, enabled=set(), env={"XAI_API_KEY": "k"}) is False  # not enabled

def test_duckduckgo_no_key_but_needs_enable():
    entry = {"name": "duckduckgo_web_search",
             "x-availability": {"provider": "duckduckgo", "requires_env": []}}
    assert av.is_available(entry, enabled={"duckduckgo"}, env={}) is True
    assert av.is_available(entry, enabled=set(), env={}) is False

def test_enabled_default_when_pref_unset_is_all_with_keys(monkeypatch):
    # WEB_SEARCH_ENABLED unset → every provider with a key is enabled + duckduckgo
    monkeypatch.setattr(av, "_read_env", lambda: {"PERPLEXITY_API_KEY": "k", "XAI_API_KEY": "k"})
    monkeypatch.setattr(av, "_read_pref", lambda name: "")  # unset
    enabled = av.enabled_web_search_providers()
    assert "perplexity" in enabled and "grok" in enabled and "grok_x" in enabled
    assert "duckduckgo" in enabled
    assert "openai" not in enabled  # no OPENAI key
```

**Step 2: Run, verify fail.**

**Step 3: Implement `availability.py`:**

```python
"""Tool-availability gate for ToolVault (v1: web-search presence-gating).

Lean-venv-safe: reads .env / config.ini with STDLIB ONLY (never import
Orchestrator.config — the MCP server's lean venv lacks fastapi). See
feedback-mcp-lean-venv.
"""
import os, configparser

_ROOT = os.environ.get("BLACKBOX_ROOT") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# provider key → env var that must be present (duckduckgo: none)
PROVIDER_ENV = {
    "perplexity": "PERPLEXITY_API_KEY", "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY", "grok": "XAI_API_KEY",
    "grok_x": "XAI_API_KEY", "duckduckgo": None,
}

def _read_env() -> dict:
    env = {}
    p = os.path.join(_ROOT, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    # process env overrides .env
    for k in list(PROVIDER_ENV.values()) + ["WEB_SEARCH_ENABLED", "WEB_SEARCH_DEFAULT"]:
        if k and os.environ.get(k):
            env[k] = os.environ[k]
    return env

def _read_pref(name: str) -> str:
    return _read_env().get(name, "") or ""

def enabled_web_search_providers() -> set:
    env = _read_env()
    raw = (env.get("WEB_SEARCH_ENABLED") or "").strip()
    if raw:
        return {p.strip() for p in raw.split(",") if p.strip()}
    # Unset → sensible default: every provider whose key is present, + duckduckgo
    enabled = {"duckduckgo"}
    for prov, key in PROVIDER_ENV.items():
        if key and env.get(key):
            enabled.add(prov)
    return enabled

def is_available(entry: dict, enabled: set = None, env: dict = None) -> bool:
    gate = entry.get("x-availability")
    if not gate:
        return True
    env = _read_env() if env is None else env
    enabled = enabled_web_search_providers() if enabled is None else enabled
    for k in (gate.get("requires_env") or []):
        if not env.get(k):
            return False
    return gate.get("provider") in enabled

def filter_available(entries: list, ctx=None) -> list:
    enabled = enabled_web_search_providers()
    env = _read_env()
    return [e for e in entries if is_available(e, enabled, env)]
```

Then apply `filter_available` in the three consumers:
- `injector._select_names`: filter the candidate catalog before the Tier-1 loop AND the semantic loop (so unavailable tools never get selected).
- `tool_registry.get_tools_by_group`: `return availability.filter_available(load_canonical(group))`.
- `tool_registry.get_mcp_tools`: filter the canonical list before converting to MCP `Tool` objects.

In `schema_spec.py`: permit an optional `x-availability` object (dict with `provider: str`, `requires_env: list[str]`) so `python -m Orchestrator.toolvault.validate` passes for the new modules.

**Step 4: Run tests, verify pass.** Also run the existing toolvault suite to confirm no regression: `Orchestrator/venv/bin/python -m pytest Orchestrator/toolvault/tests/ -v`.

**Step 5: Commit** — `git add Orchestrator/toolvault/availability.py Orchestrator/toolvault/injector.py Orchestrator/tools/tool_registry.py Orchestrator/toolvault/schema_spec.py Orchestrator/toolvault/tests/test_availability.py` → `feat(toolvault): x-availability presence-gate (key + enabled-pref)`.

---

## Task 3: The six tool modules (+ remove `web_search`)

Create `ToolVault/tools/<name>/{schema.json,executor.py}` for each of the six. Use `ToolVault/tools/roll_dice/` and the deleted `web_search` module as templates. Read `ToolVault/tools/ADDING_A_TOOL.md`.

**Files:**
- Create: `ToolVault/tools/{perplexity_web_search,openai_web_search,gemini_web_search,grok_web_search,grok_x_search,duckduckgo_web_search}/{schema.json,executor.py}`
- Delete: `ToolVault/tools/web_search/` (whole dir)
- Test: `Orchestrator/toolvault/tests/test_web_search_tools.py` (create)

**Schema template** (perplexity shown; repeat per provider, swapping name/description/`x-availability.provider`/`requires_env`):

```json
{
  "name": "perplexity_web_search",
  "description": "Search the web using Perplexity Sonar AI search. Returns a synthesized answer with source citations.",
  "category": "web",
  "groups": ["chat", "chat_cu", "realtime", "gemini_live", "grok_live", "phone", "mcp"],
  "tier": 1,
  "x-availability": {"provider": "perplexity", "requires_env": ["PERPLEXITY_API_KEY"]},
  "parameters": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "The search query to look up on the web"},
      "search_recency_filter": {"type": "string",
        "description": "Filter by recency: 'day','week','month','year' (default 'month')",
        "enum": ["hour", "day", "week", "month", "year"], "default": "month"}
    },
    "required": ["query"]
  },
  "returns": "A synthesized answer with source citations",
  "example": "perplexity_web_search(query=\"...\")"
}
```

Per-provider differences:
- `openai_web_search` → provider `openai`, `requires_env:["OPENAI_API_KEY"]`.
- `gemini_web_search` → provider `gemini`, `requires_env:["GOOGLE_API_KEY"]`.
- `grok_web_search` → provider `grok`, `requires_env:["XAI_API_KEY"]`.
- `grok_x_search` → provider `grok_x`, `requires_env:["XAI_API_KEY"]`; description emphasizes **live X/Twitter** search; **omit `search_recency_filter`** (X search is inherently recent).
- `duckduckgo_web_search` → provider `duckduckgo`, `requires_env:[]`; description notes "free fallback, no API key".

**Executor template** (perplexity; swap the provider string per module):

```python
"""Executor for perplexity_web_search."""
from Orchestrator.toolvault.context import ToolContext, ToolResult

async def execute(params: dict, ctx: ToolContext) -> ToolResult:
    query = params.get("query", "")
    if not query:
        return ToolResult(False, "Search query is required")
    recency = params.get("search_recency_filter", "month")
    try:
        from Orchestrator.web_tools import perform_provider_search
        result = perform_provider_search("perplexity", query, search_recency_filter=recency)
        return ToolResult(True, result)
    except Exception as e:
        return ToolResult(False, f"Web search error: {e}")
```

**Step 1 (test first):**
```python
from Orchestrator.toolvault import registry
def test_six_web_search_tools_load():
    names = {t["name"] for t in registry.load_canonical()}
    for n in ["perplexity_web_search","openai_web_search","gemini_web_search",
              "grok_web_search","grok_x_search","duckduckgo_web_search"]:
        assert n in names
    assert "web_search" not in names  # old generic tool removed
```
**Step 2:** run → fails. **Step 3:** create modules + delete `web_search`. **Step 4:** run → passes; then `Orchestrator/venv/bin/python -m Orchestrator.toolvault.validate` exits 0.
**Step 5: Commit** — explicit paths for the 12 new files + `git rm -r ToolVault/tools/web_search` + the test → `feat(web-search): six per-provider ToolVault tools; remove generic web_search`.

---

## Task 4: Dispatch migration (chat + voice + CU)

Remove the hand-written `web_search` branches; ensure all six tools dispatch via the ToolVault catch-all.

**Files:**
- Modify: `Orchestrator/routes/chat_routes.py` (remove ~8 `web_search` branches at lines ~307,479,1118,1651,1856,2564,4445,5155; update prompt text at ~3067,3126)
- Modify: `Orchestrator/routes/gemini_live_routes.py` (~932), `Orchestrator/routes/realtime_routes.py` (~773), `Orchestrator/routes/grok_live_routes.py` (~635) — add ToolVault catch-all, remove `web_search` branch
- Modify: `Orchestrator/browser/driver_anthropic.py` (~351)
- Modify: `Orchestrator/web_tools.py` (remove the legacy `perform_web_search` alias once no caller remains; keep `perform_web_fetch`)

**Approach:**
- **Chat:** the streaming dispatchers already route unknown `func_name` → `BlackBoxToolExecutor(operator).execute(...)` (the `control_phone` catch-all). Delete each `if/elif tool_name == "web_search": ... perform_web_search(...)` branch so the new tool names fall through to the catch-all. Update the static system-prompt text to mention `perplexity_web_search` etc. (or generically "the per-provider web_search tools").
- **Voice + CU:** these have explicit `web_search` branches and NO catch-all. Add a catch-all mirroring `control_phone`: in each tool-dispatch `if/elif` chain, add a final `else:` that calls the ToolVault executor for the requested tool name (use the same `BlackBoxToolExecutor` pattern; resolve operator as those routes already do). Remove the `web_search` branch. (Pattern documented in `project-control-phone`.)

**Test:** `Orchestrator/tests/test_web_search_dispatch.py` — assert no source line matches `== "web_search"` in the migrated files (regex guard), and that `perform_web_search` is no longer imported anywhere except possibly a back-compat shim. Plus a unit test that a voice route's dispatch falls through to the ToolVault executor for an unknown tool name (mock `BlackBoxToolExecutor.execute`).

**Step 5: Commit** — `feat(web-search): route per-provider tools via ToolVault catch-all; drop web_search branches`.

---

## Task 5: Onboarding backend (step + prefs)

**Files:**
- Modify: `Orchestrator/onboarding/state.py` (add `"web_search"` to `StepName` and `ALL_STEPS`, after `"transcription"`)
- Modify: `Orchestrator/routes/onboarding_routes.py` (`/current-config` web_search block; `/save` already writes arbitrary keys via `update_env` — verify `WEB_SEARCH_ENABLED`/`WEB_SEARCH_DEFAULT` pass the allowlist in `secrets_writer`; extend allowlist if needed)
- Modify: `Orchestrator/onboarding/secrets_writer.py` (allow the two non-secret pref keys if it has a write allowlist)
- Test: `Orchestrator/tests/test_onboarding_web_search.py` (create)

**Behavior:** `/current-config` returns a `web_search` object: `{providers: {perplexity:{key_present, enabled}, ...}, default: "<provider>"}` derived from `.env` (fresh read, E8 pattern) + `availability.enabled_web_search_providers()`. `/save` persists `WEB_SEARCH_ENABLED` (comma list) + `WEB_SEARCH_DEFAULT`.

**Tests:** save round-trip (write enabled+default → current-config reflects it); `"web_search"` is a valid step (`mark_step_complete` doesn't raise); default-when-unset matches `enabled_web_search_providers()`.

**Step 5: Commit** — `feat(onboarding): web-search provider step (enable list + default pref)`.

---

## Task 6: Onboarding frontend (Portal step)

**Files:**
- Modify: `Portal/onboarding/onboarding.js` (+ the step's HTML/template) — render the new step
- Reference: existing `transcription` step (STT preference) is the closest pattern to copy

**Behavior:** a step that fetches `/onboarding/current-config`, shows a checkbox per provider that has a key (≥1 required) + "DuckDuckGo (free)" always, plus a "preferred default" radio limited to enabled providers; on Save → POST `/onboarding/save` with `WEB_SEARCH_ENABLED` + `WEB_SEARCH_DEFAULT`, then `/onboarding/step/complete {step:"web_search"}`. Bump `?v=genuiXX` in `Portal/index.html` per CLAUDE.md.

**Test:** manual (Portal wizard) — documented checklist in the task. No unit test framework for Portal JS.

**Step 5: Commit** — `feat(onboarding): web-search step UI (Portal)`.

---

## Task 7: Default-provider hint injection

**Files:**
- Modify: the tool-instruction assembly (`Orchestrator/toolvault/injector.py` `build_tool_instructions` ~line 304, OR the chat context builder where tool guidance is added — pick the layer that already emits per-tool guidance, NOT `behavioral_core.py`)
- Test: `Orchestrator/toolvault/tests/test_web_search_hint.py` (create)

**Behavior:** when ≥1 web-search tool is in the injected set, append a short hint built from `availability.enabled_web_search_providers()` + `WEB_SEARCH_DEFAULT`: e.g. *"For web search, prefer `<default>_web_search`; other engines are available for cross-checking; use `grok_x_search` for live X/Twitter."* Omit if no web tool present.

**Test:** with default=perplexity, hint names `perplexity_web_search`; with no web tool injected, no hint emitted.

**Step 5: Commit** — `feat(web-search): default-provider system-prompt hint`.

---

## Task 8: MCP + static-array sweep + reload

**Files:**
- Sweep: `grep -rn '"web_search"' Orchestrator --include=*.py | grep -v __pycache__` — update/remove any remaining literal (frozen `BLACKBOX_TOOLS_*`/`CHAT_TOOLS_*` snapshots if present; they require a restart per CLAUDE.md)
- Verify: `MCP/blackbox_mcp_server.py` `get_mcp_tools()` returns the availability-filtered six (lean-venv path)

**Step 1:** Confirm in the lean MCP venv that `get_mcp_tools()` reflects the gate:
```
cd MCP && BLACKBOX_ROOT=<root> venv/bin/python -c "import importlib; m=importlib.import_module('blackbox_mcp_server'); n=[t.name for t in m.get_mcp_tools()]; print([x for x in n if 'search' in x])"
```
Expected: the per-provider tools whose keys+enabled match; `web_search` absent.
**Step 2:** `curl -X POST http://localhost:9091/toolvault/reload` (re-embed + bust caches).
**Step 5: Commit** — `chore(web-search): sweep web_search literals; verify MCP gate`.

---

## Task 9: Integration live smoke + restart + final review

**Steps:**
1. `sudo systemctl restart blackbox.service` (pre-authorized; ~60-90s warm-up).
2. Live per-provider smoke through the real chat path (or `/local/tools/execute`) for each of the six tools; confirm synthesized answers + citations.
3. Confirm onboarding step writes prefs and gating reflects them (disable one provider → its tool disappears from `get_mcp_tools()` after reload).
4. Run full suites: `Orchestrator/venv/bin/python -m pytest Orchestrator/tests Orchestrator/toolvault/tests -q`.
5. Dispatch the final whole-diff `superpowers:code-reviewer`.
6. **DEVICE/LIVE VALIDATION GATES THE PUSH** — only after Brandon confirms working, push to `origin/main`.
7. Update memory (`MEMORY.md` + a `project_multi_provider_web_search.md`) and mint a dev snapshot via `/snapshot-dev`.

---

## Post-review follow-ups (non-blocking)
- Resolve Gemini `vertexaisearch` redirect citations to final URLs.
- Revisit voice/phone web-tool-list leanness (6 tools may be heavy for latency-sensitive voice).
- Per-provider recency-filter normalization.
