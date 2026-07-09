# Custom Local Model Providers (OpenAI-Compatible Servers) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Let the user register any number of OpenAI-compatible local model servers (llama.cpp/llama-swap boxes) in the onboarding wizard's API-key step via a "+" button (alias + base URL + API key, validated live), and have those servers' models flow through the entire BlackBox as provider `custom`: model catalog, Portal + Android pickers, streaming chat with full ToolVault tool calling, reasoning separation, and the cron scheduler.

**Architecture:** A gitignored JSON registry (`credentials/custom_models.json`, atomic-write + fail-soft read, keys 0600) is the single source of truth, read FRESH per request so wizard edits take effect without a restart. One new provider string `custom` threads identically through: `/onboarding/validate` + new `/onboarding/custom-servers` CRUD, a `custom` branch in `GET /models/{provider}` that live-merges every server's `{base_url}/models` with qualified ids (`alias::model`), a new `stream_custom_with_reasoning()` cloned from the xAI loop (OpenAI SSE + `reasoning_content` + tool_calls accumulation + the mandatory BlackBoxToolExecutor catch-all), a `call_custom()` for the non-stream path, and additive picker entries on Portal + Android.

**Tech Stack:** FastAPI + httpx (streaming), `openai` SDK (validation + catalog probes only), vanilla-JS wizard (Portal/onboarding), Kotlin/Compose (Android), pytest (backend TDD), agent-driven Chrome (frontend verification).

---

## Verified facts (live-probed 2026-07-08 against http://192.168.1.50:8080)

- `GET /health` → `OK`; `GET /v1/models` → 3 models (`gemma-12b`, `gemma-26b`, `gemma-31b`) with a llama-swap `status.value: loaded|unloaded` extension; no key → HTTP 401.
- `GET /v1/models` is served by the llama-swap proxy itself — it does NOT load a model. It responds instantly even when everything is cold. Only chat completions trigger the 15–25 s cold load.
- Non-streaming chat returns `message.content` AND `message.reasoning_content` as separate fields. Streaming emits `delta.reasoning_content` chunks then `delta.content` chunks — the exact field the existing xAI loop already checks FIRST (`chat_routes.py:5306-5315` probes `reasoning_content, reasoning, thinking, thought`).
- Native OpenAI tool calling verified end-to-end: `finish_reason: "tool_calls"`, `tool_calls[].function.arguments` is a JSON string, id present.
- Per INTEGRATION.md: cold load 15–25 s (llama-swap HOLDS the request, no 503), one request at a time per model, ≥120 s client timeout for completions, `max_tokens` generous (thinking spends output budget), only `gemma-12b` accepts images (500 otherwise).

## Locked design decisions

| Decision | Choice | Why |
|---|---|---|
| Provider id | `custom` — one string everywhere (dispatch, `_get_tools`, injector, catalog, both frontends) | `local` is taken by on-phone Gemma; avoids the xai/grok naming split that requires bridges |
| Registry | `credentials/custom_models.json` — gitignored (`.gitignore:27-28` `credentials/`), 0600, atomic tmp+rename write, timestamped backup, fail-soft read | Brandon's spec (SNAP-20260708-8105): "github will ignore that ... so it'll stick". `credentials/` is canonical per discovery-notes; the blueprint's `Orchestrator/Secrets/` is a flagged orphan. Base URLs don't fit `secrets_writer` env-var semantics |
| Activation | FRESH read per request. NO restart required | Strictly better than the restart-to-activate Brandon expected; avoids the E8 stale-constant trap (`config.py` import-time freeze). Keep custom OUT of `/onboarding/restart-status` drift checks (documented convention, `onboarding_routes.py:216-219`) |
| Model ids | Catalog serves qualified ids `"<alias>::<model_id>"`; display name `"<model_id> (<alias>)"`; backend splits on FIRST `::` to route. Unqualified ids fall back to first enabled server that lists the model, then first enabled server | Deterministic multi-server routing with zero changes to the locked `/chat` send contract (provider+model strings pass verbatim). `/` can't be the separator — HF-style model ids contain it |
| Catalog freshness | `/models/custom` bypasses ALL THREE caches (no backend `models_cache` entry; Portal skips sessionStorage; Android skips its in-memory cache for `custom`) — every catalog request re-probes each server's `{base_url}/models` live | Brandon's note: "download a new model and it automatically shows up, just like OpenAI". The probe is an instant LAN call that never loads a model, so caching buys nothing and staleness costs correctness (including the warm dot) |
| Warm-status dot | Catalog fetcher uses raw httpx, NOT the `openai` SDK (the SDK's pydantic response models silently drop llama-swap's non-standard `status` extension). Each catalog model gains an additive `status: "loaded" \| "unloaded" \| null` (null for servers that don't report it). Portal prefixes 🟠 to loaded models' option text; Android Composer renders an orange dot on loaded rows | Brandon's note: show which model is warm. llama-swap reports residency in `GET /v1/models` `data[].status.value` (live-verified) |
| Streaming loop | NEW `stream_custom_with_reasoning()` cloned from `stream_xai_with_reasoning` (`chat_routes.py:5165-5700`), parameterized by resolved server | Zero regression risk to existing providers; the xAI clone carries vision preprocessing, reasoning fields, tool_calls accumulation, and the catch-all for free. Matches the one-function-per-provider repo idiom |
| Tool injection | Standard `chat` group via injector defaults + explicit `PROVIDER_FORMATS["custom"]="openai_rest"` | These are 12B–31B servers, not phone models; gemma-26b passed live tool-calling. Leaner injection is a future knob |
| Context guard | Per-server `context_tokens` field (default 32768). BOTH `/chat/stream` routes resolve the server in their default-model block, compute `max(4000, int(context_tokens * 0.6))`, and pass it via a NEW optional `window_guard_tokens: int \| None = None` parameter that must be ADDED to `build_streaming_context` and `build_fossil_context` (verified: NO such override exists today — the guard is looked up internally at `context_builder.py:384`). Static floor `PROVIDER_WINDOW_GUARD_TOKENS["custom"] = 19200` backs any path that misses the override | Unknown providers get a 240,000 floor-token guard (`context_builder.py:92`) which would overflow a 16–32K llama.cpp window |
| Timeouts | Chat: flat 300 s httpx (matches every existing loop; covers 120 s cold loads). Validator + catalog probes: 10 s / 5 s (models.list is instant — see verified facts) | |
| `max_tokens` | Do not set it in the chat payload (llama-server generates until EOS/ctx) | INTEGRATION.md §6: small caps leave `content` empty after thinking |
| Wizard UX | A "Custom model servers" section INSIDE the api_keys step (repeatable rows, `operator.js` add-row pattern), NOT a new wizard step | Brandon's UX law (M10.0): "one home for all keys". A new step needs 4 synchronized registries |
| Secondary surfaces | Chat (Portal+Android) + cron scheduler: YES. SMS, device-control, voice: NO (future) | Voice is realtime-audio-only; SMS has Anthropic-specific thread constraints; scheduled jobs on free local models is the obvious win |
| Hub status | Each server appears as an item in the api_keys tile; a validated+enabled custom server satisfies the "have an LLM key" requirement | Fresh box running only local models is a valid production configuration |

## Key gotchas the implementer MUST honor

1. **Dispatch is duplicated**: `GET /chat/stream` (`chat_routes.py:~6159`) and `POST /chat/stream` (`~6251`) have identical if/elif provider chains AND identical default-model blocks (`~6139`, `~6233`). Touch BOTH or Portal works while EventSource breaks (or vice versa).
2. **The catch-all is non-negotiable** (ce90e24 lesson): the tool-execution section must route every unmatched `func_name` through `BlackBoxToolExecutor(operator=operator, origin_device_id=_ORIGIN_DEVICE_ID.get()).execute(...)` — copy `chat_routes.py:5652-5681` verbatim (the executor call + `_media_kind` task-event emission at 5664-5674 AND the tool-result append at 5680 with its `tool_result or "Tool executed successfully"` empty-result fallback). Without it, injected tools silently "succeed" without running.
3. **`tasks.py:1592` raises `unknown provider`** — POST `/chat` (non-stream), MCP `chat_with_context`, and the cron executor 500 on `custom` without an explicit branch.
4. **`/models/{provider}` 404s unknown providers** (`admin_routes.py:830-831`) and a dedicated `/models/custom` route would be SHADOWED by the generic handler — dispatch INSIDE `get_available_models` like `local` does (`826-828`). The `_wrap` envelope is a locked contract: extend additively only (per-model `server` field mirrors CU's `backend` precedent).
5. **Three stacked caches exist for other providers** — backend `models_cache` (600 s), Portal sessionStorage (5 min), Android in-memory (5 min) — and `custom` must bypass ALL of them (see design table). Do NOT route the custom branch through `models_cache`, and add explicit `provider === 'custom'` skip conditions in Portal `fetchAvailableModels` and Android `fetchLiveModels`, otherwise new downloads and the warm dot go stale for up to 15 minutes.
6. **Portal `updateModelDropdown()` early-returns on unknown provider** (`state-management.js:482-486`) — without a `MODEL_CONFIG['custom']` entry the dropdown silently never populates.
7. **Android `ChatProvider.fromId` falls back to GEMINI** for unknown ids (`Provider.kt:18`) and `mapProviderForApi` returns null → silent hydration no-op (`ChatViewModel.kt:2657-2666`). Both need explicit `custom` entries.
8. **`validated_at` is keyed by bare provider string** (`state.py:141-147`) — key custom validations as `custom:<server_id>` or one server's validation marks all validated.
9. **Registry must be fail-soft**: corrupt/absent file → empty server list + log warning, never a boot crash (devices-project boot-guard precedent). Fresh box = file absent = everything degrades to "no custom servers".
10. **Never `git add -A`** — stage explicit paths in every commit.
11. **Wizard files need NO `?v=` busting** (served no-cache via `_NoCacheStaticFiles`); `Portal/index.html` DOES need the `?v=genuiNNN` bump at BOTH occurrences (lines ~11 and ~21).
12. **`/onboarding/validate` with a user-supplied base_url is SSRF-shaped by design** — Tailscale/LAN is the security perimeter (memory: tailscale_security_perimeter). State it in the endpoint docstring; do not add auth.
13. **`tasks.py` media auto-route steals custom chats** (`1573-1581`): `has_video`/`has_audio` switches provider to `google` BEFORE dispatch. Exempt `provider == "custom"` from the switch — on a custom-only box (no Google key) the switch is a hard failure instead of a reply. Audio/video attachments are simply unsupported on custom servers (llama.cpp has no audio path; INTEGRATION.md §7).
14. **The non-stream path has NO window guard at all**: `tasks.py` builds its fossil block INLINE (`1380-1494`, `CAP=None`, count knobs only) and never calls `build_fossil_context`. Without an explicit custom trim there (Task 5.1), POST `/chat`, MCP `chat_with_context`, and cron ship an unguarded ~119k-floor-token context into a 16–32K window.

---

## Milestone 1 — Server registry backend (TDD)

### Task 1.1: Registry module `custom_servers.py`

**Files:**
- Create: `Orchestrator/onboarding/custom_servers.py`
- Test: `Orchestrator/tests/test_custom_servers.py`

**Step 1: Write the failing tests**

```python
# Orchestrator/tests/test_custom_servers.py
import json, os, stat
import pytest
from Orchestrator.onboarding import custom_servers as cs


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "custom_models.json"
    monkeypatch.setattr(cs, "REGISTRY_PATH", str(path))
    return path


def test_list_servers_absent_file_returns_empty(registry):
    assert cs.list_servers() == []


def test_list_servers_corrupt_file_returns_empty(registry):
    registry.write_text("{not json")
    assert cs.list_servers() == []  # fail-soft, never raises


def test_add_server_persists_and_generates_id(registry):
    srv = cs.add_server(alias="gemma-box", base_url="http://192.168.1.50:8080/v1",
                        api_key="sk-test", context_tokens=32768)
    assert srv["id"].startswith("srv-")
    on_disk = json.loads(registry.read_text())
    assert on_disk["version"] == 1
    assert on_disk["servers"][0]["alias"] == "gemma-box"
    assert on_disk["servers"][0]["enabled"] is True


def test_registry_file_is_0600(registry):
    cs.add_server(alias="a", base_url="http://x/v1", api_key="k")
    mode = stat.S_IMODE(os.stat(registry).st_mode)
    assert mode == 0o600


def test_base_url_normalized_no_trailing_slash(registry):
    srv = cs.add_server(alias="a", base_url="http://x:8080/v1/", api_key="k")
    assert srv["base_url"] == "http://x:8080/v1"


def test_alias_must_be_unique_and_separator_free(registry):
    cs.add_server(alias="box", base_url="http://x/v1", api_key="k")
    with pytest.raises(ValueError):
        cs.add_server(alias="box", base_url="http://y/v1", api_key="k")
    with pytest.raises(ValueError):
        cs.add_server(alias="bad::alias", base_url="http://z/v1", api_key="k")


def test_update_and_delete_server(registry):
    srv = cs.add_server(alias="a", base_url="http://x/v1", api_key="k")
    cs.update_server(srv["id"], {"alias": "b", "last_models": ["m1"]})
    assert cs.get_server(srv["id"])["alias"] == "b"
    cs.delete_server(srv["id"])
    assert cs.list_servers() == []


def test_resolve_model_qualified_and_fallback(registry):
    s1 = cs.add_server(alias="one", base_url="http://x/v1", api_key="k")
    s2 = cs.add_server(alias="two", base_url="http://y/v1", api_key="k")
    cs.update_server(s2["id"], {"last_models": ["gemma-26b"]})
    srv, bare = cs.resolve_model("two::gemma-26b")
    assert srv["id"] == s2["id"] and bare == "gemma-26b"
    # unqualified: server that listed it wins
    srv, bare = cs.resolve_model("gemma-26b")
    assert srv["id"] == s2["id"]
    # unknown unqualified: first enabled server
    srv, bare = cs.resolve_model("mystery-model")
    assert srv["id"] == s1["id"] and bare == "mystery-model"


def test_resolve_model_no_servers_returns_none(registry):
    assert cs.resolve_model("anything") == (None, "anything")


def test_redacted_listing_masks_keys(registry):
    cs.add_server(alias="a", base_url="http://x/v1", api_key="sk-secret-1234")
    red = cs.list_servers_redacted()[0]
    assert "api_key" not in red
    assert red["key_last4"] == "1234"
    assert red["key_present"] is True
```

**Step 2: Run tests, verify they fail** — `cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc" && Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_custom_servers.py -v` → import error (module missing).

**Step 3: Implement `Orchestrator/onboarding/custom_servers.py`** — stdlib-only (json, os, re, tempfile, uuid, datetime, threading, logging). Shape:

```python
"""Registry of user-added OpenAI-compatible model servers (provider 'custom').

Stored OUTSIDE git (credentials/ is gitignored) so servers survive pulls.
Read FRESH by every consumer -- no import-time constants (E8 lesson).
NOTE: /onboarding/validate probing user-supplied base_urls is LAN-trust by
design; Tailscale is the perimeter (do not add app-layer auth here).
"""
import json, logging, os, re, tempfile, threading, uuid
from datetime import datetime, timezone

from Orchestrator.utils import paths  # canonical root resolver: honors BLACKBOX_ROOT env var
                                      # FIRST (state.py:27 precedent; stdlib-only, lean-venv-safe).
                                      # Do NOT hand-roll dirname math -- installed boxes relocate the tree.

logger = logging.getLogger(__name__)
REGISTRY_PATH = paths.resolve("credentials", "custom_models.json")
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,31}$")  # no '::' possible
_LOCK = threading.Lock()
SEP = "::"
DEFAULT_CONTEXT_TOKENS = 32768
```

Functions: `_read()` (fail-soft: absent/corrupt → `{"version": 1, "servers": []}` with `logger.warning` on corrupt), `_write(data)` (makedirs `credentials/`, `tempfile.NamedTemporaryFile` in same dir + `os.replace`, `os.chmod(path, 0o600)`), `list_servers(enabled_only=False)`, `get_server(server_id)`, `add_server(alias, base_url, api_key="", context_tokens=DEFAULT_CONTEXT_TOKENS)` (validate alias regex + uniqueness case-insensitive, `base_url.rstrip("/")`, require scheme `http(s)://`, id `f"srv-{uuid.uuid4().hex[:8]}"`, stamps `added_at`, `enabled: True`, `validated_at: None`, `last_models: []`), `update_server(server_id, patch)` (allowlist patchable fields: alias, base_url, api_key, enabled, context_tokens, validated_at, last_models; re-validate alias/url rules), `delete_server(server_id)`, `resolve_model(model)` (split on FIRST `SEP`; qualified → alias match among enabled; unqualified → first enabled server whose `last_models` contains it, else first enabled; returns `(server_dict_or_None, bare_model)`), `qualify(alias, model_id)`, `list_servers_redacted()` (drop `api_key`, add `key_present`, `key_last4`).

All mutations under `_LOCK` with read-modify-write inside the lock. Keep the module stdlib-only (no Orchestrator.config import) so lean-venv readers can use it later.

**Step 4: Run tests, verify all pass.**

**Step 5: Commit** — `git add Orchestrator/onboarding/custom_servers.py Orchestrator/tests/test_custom_servers.py && git commit -m "feat(custom-models): gitignored server registry with fail-soft read + model resolution"`

### Task 1.2: Validator `validate_custom`

**Files:** Modify: `Orchestrator/onboarding/validators.py`; Test: `Orchestrator/tests/test_custom_validator.py`

**Step 1: Failing tests** — mock at the SOURCE: `monkeypatch.setattr("openai.OpenAI", FakeClient)`. Verified: `validators.py` imports the SDK INSIDE each function body (`from openai import OpenAI` at lines 53/95), so `monkeypatch.setattr(validators, "OpenAI", ...)` raises AttributeError; the existing suite (`test_onboarding_validators.py`) likewise patches at the source (`requests.get`/`requests.post`):

```python
def test_validate_custom_ok_returns_models():
    # mocked models.list() -> ids ["gemma-26b", "gemma-12b"]
    res = validators.validate_custom(base_url="http://x/v1", api_key="k")
    assert res.ok and res.detail["model_count"] == 2
    assert "gemma-26b" in res.detail["models"]

def test_validate_custom_auth_error():
    # mocked AuthenticationError -> ok False, error mentions key/401

def test_validate_custom_unreachable():
    # mocked APIConnectionError -> ok False, error mentions unreachable/URL
```

**Step 3: Implement** modeled on `validate_xai` (`validators.py:88-108`) but using `client.models.list()` like `validate_openai` (zero tokens, instant even on a cold llama-swap — verified live): `OpenAI(api_key=api_key or "none", base_url=base_url, timeout=10.0, max_retries=0)`, wrap in `_measure()`, success detail `{"model_count": n, "models": [ids...]}` (cap the list at ~50 ids). Map `AuthenticationError` → "API key rejected (401)", `APIConnectionError` → "Server unreachable at {base_url}", generic → str(e). Note: `api_key or "none"` because the openai SDK refuses empty keys and some LAN servers run keyless.

**Step 5: Commit** — `git commit -m "feat(custom-models): validate_custom via models.list (doubles as model discovery)"` (explicit paths).

### Task 1.3: `/onboarding/custom-servers` CRUD + validate dispatch

**Files:** Modify: `Orchestrator/routes/onboarding_routes.py`; Test: `Orchestrator/tests/test_custom_servers_routes.py`

**Step 1: Failing tests** — follow the repo's two-tier pattern: the `/validate` dispatch + stamping assertions use DIRECT route-function calls with monkeypatched `ob.validators.<fn>` and `ob._state.record_validation` (exact precedent: `Orchestrator/tests/test_onboarding_validate_route.py` — no app import needed); reserve the heavier TestClient-over-the-app pattern for the CRUD roundtrip. Monkeypatch `REGISTRY_PATH` in both:

```python
def test_crud_roundtrip(client, tmp_registry):
    r = client.post("/onboarding/custom-servers", json={
        "alias": "gemma-box", "base_url": "http://192.168.1.50:8080/v1",
        "api_key": "sk-x", "context_tokens": 32768})
    assert r.status_code == 200
    sid = r.json()["server"]["id"]
    listing = client.get("/onboarding/custom-servers").json()["servers"]
    assert listing[0]["key_last4"] == "sk-x"[-4:] and "api_key" not in listing[0]
    assert client.patch(f"/onboarding/custom-servers/{sid}", json={"alias": "box2"}).status_code == 200
    assert client.delete(f"/onboarding/custom-servers/{sid}").status_code == 200
    assert client.get("/onboarding/custom-servers").json()["servers"] == []

def test_add_duplicate_alias_400(client, tmp_registry): ...
def test_validate_custom_provider_dispatch(client, tmp_registry, monkeypatch):
    # monkeypatch validators.validate_custom -> ok result with models
    # POST /onboarding/validate {"provider":"custom","credentials":{"base_url":...,"api_key":...,"server_id":sid}}
    # -> 200 ok; registry server now has validated_at set and last_models populated
```

**Step 3: Implement:**
- Extend `ValidateRequest.provider` Literal (`onboarding_routes.py:93`) with `"custom"`.
- In the `/validate` dispatch (`357-392`): `custom` branch pulls `base_url`/`api_key` from `req.credentials`; if `server_id` supplied and credentials omitted, resolve them from the registry (stored-server re-validation, mirroring `_resolve_stored_creds`). On ok: `record_validation(f"custom:{server_id}")` when server_id known (NOT bare `custom` — gotcha 8), and `custom_servers.update_server(server_id, {"validated_at": now_iso, "last_models": result.detail["models"]})`.
- New endpoints (same router, after existing config routes): `GET /onboarding/custom-servers` → `{"servers": custom_servers.list_servers_redacted()}`; `POST` (Pydantic body: alias str, base_url str, api_key str = "", context_tokens int = 32768) → 400 on ValueError; `PATCH /{server_id}` (partial, key omitted ⇒ unchanged); `DELETE /{server_id}`. Docstring on POST/validate noting the LAN-trust/SSRF-by-design stance (gotcha 12).

**Step 5: Commit.**

### Task 1.4: Hub status rollup

**Files:** Modify: `Orchestrator/onboarding/status_rollup.py` (`_derive_api_keys`, `77-96`) AND `Orchestrator/routes/onboarding_routes.py` (`_collect_status_inputs`, `~606+`); Test: `Orchestrator/tests/test_onboarding_status.py`

**Purity constraint (verified):** `build_status` is contractually PURE — it derives state from already-read inputs only (module docstring `status_rollup.py:8-14`); all I/O lives in the route layer's `_collect_status_inputs`. Do NOT read the registry inside `_derive_api_keys` — the hermetic tests call `build_status(**_empty_inputs())` with no filesystem mocks and would flip once a real server is registered on this box.

Steps:
1. Failing tests in `test_onboarding_status.py`: add `custom_servers=[]` to `_empty_inputs()`; new cases — (a) one validated+enabled custom server + zero env keys → api_keys section NOT "no LLM key" attention, items include `{"key": "custom:srv-abc", "label": "Custom: gemma-box", "configured": True, "validated_at": <ts>}` (the ACTUAL item shape — items carry no `state` field, state is section-level), summary string mentions `1 custom server`; (b) unvalidated server → nudge/attention summary.
2. Implement: `_collect_status_inputs` reads the registry fail-soft (like the paired/operators inputs) and passes `custom_servers=[...]` through `build_status` into `_derive_api_keys(env, state, custom_servers)`.
3. **Visibility (verified gotcha):** the hub tile renders ONLY `state` + `summary` + pip (`Portal/onboarding/status.js:51-63`); per-section `items[]` are never rendered anywhere. Surface the servers in the SUMMARY string (e.g. `"2 keys validated · 1 custom server"`) — that is what Task 2.1's hub verification must assert.
4. Run the full rollup + parity test files, commit.

---

## Milestone 2 — Wizard UI ("+" custom servers in the api_keys step)

### Task 2.1: Custom-servers section in `api_keys.js`

**Files:** Modify: `Portal/onboarding/steps/api_keys.js`

After the `PROVIDERS.map` card render (~line 135), add a **"Custom model servers"** section:

- Section header + copy: "OpenAI-compatible servers on your network (llama.cpp, llama-swap, vLLM, Ollama). Models are discovered automatically."
- Rows hydrate from `GET /onboarding/custom-servers`. Each configured row renders alias, base_url, `••••` + `key_last4`, validated state pill, per-row **Re-validate** (POST `/onboarding/validate` `{provider:"custom", credentials:{server_id}}`), **Edit** (swaps to editable inputs; PATCH on save), and **[×] Remove** (DELETE + re-render; use `confirm()`-free inline "Remove? ✓/✕" toggle since browser dialogs are banned in this project's automation).
- **"+ Add server"** button (the `operator.js:236-243` addRow pattern): appends an editable row `{alias, base_url, api_key}` with a **Validate & Add** button. Flow: POST the server FIRST to `/onboarding/custom-servers`, then validate with `server_id` so `validated_at`/`last_models` persist via the Task 1.3 path; on validation failure leave the row configured-but-error-pilled with a Remove option (the server may simply be powered off).
- Success detail rendering: `"3 models: gemma-26b, gemma-12b, gemma-31b"` from `detail.models`. Note (verified): `formatDetail` (`444-459`) is only invoked from the PROVIDERS card paths — the new section's own success handler must call it (or its own formatter) explicitly.
- Configured-row "validated" pill source: the `validated_at` field from `GET /onboarding/custom-servers` (`list_servers_redacted()` keeps it) — NOT `/onboarding/current-config`, whose `providers` dict is a fixed pydantic shape with no custom entry.
- Save-button enablement (`466-479`): a configured custom server counts as "retained existing" so a custom-only user can advance (gotcha: Save gates on PROVIDERS only today).
- Keep everything additive to the current-config consumers (this section uses its own endpoint; do not touch the `providers` payload shape).

No `?v=` bump needed (wizard is no-cache).

**Verify (agent-driven Chrome):** open `http://localhost:9091/onboarding/?step=api_keys`, add the real server (alias `gemma-box`, `http://192.168.1.50:8080/v1`, the key from `/home/ai-black-box-fc/Desktop/INTEGRATION.md`), see "3 models: …" success, reload page → row rehydrates as configured with `••••DHE` style last4, Re-validate works, Remove works, re-add for subsequent milestones. Also open `?mode=manage` and confirm the api_keys tile SUMMARY text mentions the custom server (per Task 1.4: the hub renders only state/summary/pip — items are never rendered).

**Commit** after verification.

### Task 2.2: Restart the service + registry survives

`sudo systemctl restart blackbox.service` (pre-authorized; 60–90 s warm-up), then `curl -s localhost:9091/onboarding/custom-servers` → server still listed (file persistence), and `git status --porcelain credentials/` → empty (gitignore holds). Commit nothing; this is a checkpoint.

---

## Milestone 3 — Model catalog (`GET /models/custom`)

### Task 3.1: Catalog fetcher + dispatch

**Files:** Modify: `Orchestrator/routes/admin_routes.py`; Test: `Orchestrator/tests/test_models_custom.py`

**Step 1: Failing tests** (monkeypatch registry + the per-server HTTP fetch):

```python
def test_models_custom_merges_servers_with_qualified_ids():
    # two servers, one returns [gemma-26b], other [qwen-7b]
    # -> models [{id:"one::gemma-26b", name:"gemma-26b (one)", server:"one", status:...}, ...]
    # -> default_id == "one::gemma-26b", source == "live"

def test_models_custom_includes_warm_status():
    # server payload carries llama-swap extension data[].status.value ("loaded"/"unloaded")
    # -> model dicts carry additive status: "loaded"/"unloaded"
    # a plain OpenAI-shaped payload WITHOUT the extension -> status None (defensive parse)

def test_models_custom_no_servers_empty_not_404():
    # empty registry -> 200 {provider:"custom", models:[], source:"fallback", default_id:""}

def test_models_custom_dead_server_falls_back_to_last_models():
    # fetch raises -> that server's cached last_models used (status None), source "live" if any live fetch succeeded else "fallback"

def test_models_custom_never_cached():
    # two consecutive calls -> fetcher invoked twice (no models_cache entry for "custom")
```

**Step 3: Implement:**
- `_fetch_custom_models()` near `_fetch_xai_models` (`693-718`): iterate `custom_servers.list_servers(enabled_only=True)`; per server do a raw `httpx.get(f"{base_url}/models", headers=bearer-if-key, timeout=5.0)` — NOT the `openai` SDK, whose pydantic models silently drop llama-swap's `status` extension. Parse `data[].id` and `data[].status.value` defensively (`.get` chains; missing → `None`). Probe servers concurrently (thread-pool map — a dead LAN box must not serialize a 5 s stall per server). On success `update_server(id, {"last_models": ids})`; on failure fall back to `last_models` with `status: None`.
- Dispatch INSIDE `get_available_models` before the 404 (mirror the `local` special-case at `826-828` — gotcha 4), and BYPASS `models_cache` entirely — verified clean: the cache is only consulted via `_models_cache_get` at `837-841`, AFTER the insertion point. Response via `_wrap` with additive per-model `server` and `status` fields, then POST-ASSIGN two envelope fields the helper can't produce (verified): `default_id` (=first model of first enabled server, `""` when none — `_wrap` hardcodes it from the static `_DEFAULT_MODEL` map at `786`) and `cached: False` (every other provider's envelope carries `cached` via `get_cached_or_fetch`; don't be the only response missing it).

**Step 4-5: Pass, then live-verify + commit:** `curl -s localhost:9091/models/custom | python3 -m json.tool` → 3 qualified gemma models with `server: "gemma-box"`, exactly one of them `status: "loaded"` (whichever is resident), and a repeat call after `curl`-chatting a DIFFERENT model shows the dot moved.

---

## Milestone 4 — Chat streaming with tools (the core)

### Task 4.1: `stream_custom_with_reasoning()`

**Files:** Modify: `Orchestrator/routes/chat_routes.py`

Clone `stream_xai_with_reasoning` (`5165-5700`) → `stream_custom_with_reasoning(messages, model, operator)` (verified: the xai signature is exactly those three params — there are no extra kwargs to carry; session_id/device_id are CU-only). Exact deltas from the clone:

1. Top of function: resolve the server FRESH — `server, bare_model = custom_servers.resolve_model(model or "")`; if `server is None`, yield an `error` event ("No custom model servers configured — add one in the onboarding wizard") and return. If `model` was empty/Auto: resolve `default` = first of `server["last_models"]` else error. (The ROUTES also resolve the server before context building — Task 4.3 — this in-function resolve is the defensive backstop for direct callers.)
2. URL: `f"{server['base_url']}/chat/completions"` (base_url already ends in `/v1`). Headers: `Authorization: Bearer {server['api_key']}` only when key non-empty.
3. Payload: `bare_model` (unqualified) as `model`; DO NOT set `max_tokens`; keep `stream_options: {"include_usage": true}` but wrap the FIRST request attempt so a 400 mentioning `stream_options` retries once without it (compatibility knob for older llama.cpp builds).
4. Keep verbatim: the 300 s httpx client, SSE `data:` parsing, reasoning-field priority list (`reasoning_content` first — llama.cpp lands there), tool_calls index-accumulation, `finish_reason=="tool_calls"` execution round with the **BlackBoxToolExecutor catch-all block copied verbatim** (gotcha 2), tool-result feedback shape, 30-iteration cap, vision preprocessing (data-URL conversion — gemma-12b accepts the same `image_url` shape).
5. `_get_tools("custom", prompt)` for the tools payload (Task 4.2 wires the format).
6. Import `custom_servers` at the top of `chat_routes.py` alongside the other onboarding imports.

No test harness exists for these generators; correctness is proven by Task 4.4's live E2E. Keep the diff a mechanical clone+delta to stay reviewable.

### Task 4.2: Injector + `_get_tools` + context guard

**Files:** Modify: `Orchestrator/toolvault/injector.py` (`47-75`), `Orchestrator/routes/chat_routes.py` (`_get_tools` ~216), `Orchestrator/context_builder.py` (`79-101`); Test: `Orchestrator/tests/test_custom_provider_wiring.py`

Failing tests → implement → pass → commit:

```python
def test_injector_formats_custom_as_openai_rest():
    from Orchestrator.toolvault.injector import PROVIDER_FORMATS, PROVIDER_DEFAULT_GROUP
    assert PROVIDER_FORMATS["custom"] == "openai_rest"
    assert PROVIDER_DEFAULT_GROUP["custom"] == "chat"

def test_context_guard_custom_scales_with_context_tokens():
    # helper custom_window_guard(server) -> max(4000, int(ctx*0.6))
```

- `injector.py`: add `"custom": "openai_rest"` and `"custom": "chat"` (explicit, though the default already covers it — keeps the `[TOOLVAULT-INJECT]` log truthful).
- `_get_tools` (`chat_routes.py:216`): add `"custom"` to the openai-format tuple.
- `context_builder.py` — **no override parameter exists today (verified); ADD one**: `build_fossil_context` (signature at `149-159`) gains `window_guard_tokens: int | None = None`, consumed at line `384` as `window_guard_tokens if window_guard_tokens is not None else window_guard_budget_tokens(provider)`. Thread the same optional param through `build_streaming_context` (`chat_routes.py:5983`, pass-through at `6014-6019`). Also add `PROVIDER_WINDOW_GUARD_TOKENS["custom"] = 19200` (0.6 × 32768 default) as the no-override floor so any path that misses the thread never gets the 240K default (verified: the lookup is `.get(provider.lower())` at `104-114`, so a static entry works).

### Task 4.3: Dispatch in BOTH `/chat/stream` routes

**Files:** Modify: `Orchestrator/routes/chat_routes.py` (`~6139-6175` GET, `~6233-6269` POST)

In BOTH routes (gotcha 1), and ORDER MATTERS (verified: `build_streaming_context` is called at `6152` GET / `6246` POST, BEFORE the dispatch at `6159`/`6251`, so the server must be resolved in the default-model block or its `context_tokens` is unknowable at context-build time):

- Default-model block (`~6139`/`~6233`): `elif provider == "custom":` → `server, bare = custom_servers.resolve_model(model or "")`; if `server`, default empty model to the server's first `last_models` entry (qualified) and compute `guard = max(4000, int(server.get("context_tokens", 32768) * 0.6))`; if no server, leave model as-is (the stream fn emits the clean error event).
- `build_streaming_context` call site: pass `window_guard_tokens=guard` when provider is custom (None otherwise — Task 4.2's new param).
- Dispatch chain (`~6159`/`~6251`): `elif provider == "custom": stream = stream_custom_with_reasoning(context_messages, model, operator)` mirroring the xai line, wrapped by the existing `_stream_with_keepalive`.

Commit.

### Task 4.4: Live E2E — streaming, reasoning, tools

**Verification (evidence, not vibes):**

```bash
# 1. plain streamed chat via POST /chat/stream, provider=custom, model=gemma-box::gemma-26b
#    EXPECT: 'thinking' SSE events (reasoning_content) then 'content' events, then done
# 2. tool round-trip: prompt "Roll 3 dice using the roll_dice tool" (roll_dice is the ToolVault
#    worked example; executes via the catch-all)
#    EXPECT: tool_start + tool_result SSE events and journalctl [TOOLVAULT-EXEC] lines
# 3. Auto model: model="" resolves to first registry model
# 4. GET /chat/stream (EventSource path) — same prompt, confirms the duplicated dispatch
sudo journalctl -u blackbox.service -n 200 | grep -E "TOOLVAULT-EXEC|custom"
```

Then Chrome-agent verification in the Portal once Milestone 6 lands. Commit any fixes; if a symptom survives two fix attempts, STOP and add telemetry (memory: telemetry_before_fixes).

---

## Milestone 5 — Non-stream path + cron

### Task 5.1: `call_custom` + tasks.py dispatch

**Files:** Modify: `Orchestrator/routes/chat_routes.py` (clone `call_xai`, `1655-1857`), `Orchestrator/tasks.py` (`1380-1494`, `1566-1626`); Test: `Orchestrator/tests/test_custom_provider_wiring.py` (extend)

- `call_custom(messages, model, operator, ...)`: clone **`call_xai` (`1655-1857`), NOT `call_openai`** — verified: `call_openai` returns a bare `(text, usage)` 2-tuple with ZERO reasoning extraction ("response-only by design", `tasks.py:1611`), so a clone of it would silently drop llama.cpp's ever-present `reasoning_content`. `call_xai` probes the same 4 reasoning fields (`1852`), returns `(text, usage, reasoning, media_tasks)` (`1857`), and carries the `"(no output)"` catch-all at `1827`. Apply the same server-resolution deltas as Task 4.1 (fresh resolve, bare model, bearer-if-key, keep the `requests` timeout 200).
- `tasks.py`: alias/normalize accepts `custom`; default-model resolution branch (registry first model, no hardcoded literal — the `1592` raise must no longer fire for custom); dispatch in the lazy-import block (`1605-1626`) as `raw, usage, reasoning, media_tasks = _unpack_call(call_custom(...))` mirroring the xai line at `1624`.
- **Media auto-route exemption (gotcha 13):** at `1573-1581`, exempt `provider == "custom"` from the `has_video`/`has_audio` → `google` switch. Audio/video attachments on custom get a normal text reply (unsupported media noted), not a Gemini hijack that hard-fails on Google-keyless boxes.
- **Non-stream context guard (gotcha 14):** the inline fossil block (`1380-1494`) is uncapped (`CAP=None`) and never consults the window guard. Add a custom-provider trim: when provider is custom, drop whole snapshots until `estimate_tokens(assembled) <= window_guard_tokens(server)` — the stream guard's EXACT metric (`tokenization.py` `CHARS_PER_TOKEN_FLOOR = 2.0`; an earlier revision of this plan said `guard_tokens * 4` chars, which is 2× looser than the floor definition and could overflow a 16K window — corrected after the 5.1 quality review). Failing test proves a huge fossil block shrinks under the cap for custom and is untouched for other providers.
- Failing test: `process_chat_task`-level dispatch test with mocked `call_custom` proving provider `custom` routes, defaults, unpacks via `_unpack_call`, and skips the media auto-route.
- Live check: `curl -X POST localhost:9091/chat -d '{"prompt":"say hi","provider":"custom", ...}'` completes; reply lands with reasoning separated (appended last per the `_unpack_call` path).

### Task 5.2: Cron scheduler

**ORDERING: execute AFTER Milestone 6** — the Portal cron modal hydrates via `fetchAvailableModels('custom')`, which TypeErrors into its catch (`state-management.js:612` assigns onto `MODEL_CONFIG[provider].models`) until Task 6.1's `MODEL_CONFIG['custom']` entry exists.

**Files:** Modify: `Orchestrator/scheduler/executor.py` (`_provider_default_model`, `449-470`), `Portal/modules/cron-manager.js` (`CRON_PROVIDERS`, ~15), `Portal/index.html` (cron modal `~1429-1442`), Android `ui/cron/CronViewModel.kt` (`435-466` AND `selectProvider` `529-584`), Android `ui/cron/CronManagerScreen.kt` (`CRON_PROVIDER_LABELS`, `1163-1169`)

- Executor (verified): `_provider_default_model` is `449-470`; its map's `.get(provider, GEMINI_MODEL_DEFAULT)` at `470` means an unknown provider today SILENTLY falls back to Gemini's default (the failure only surfaces later at `tasks.py:1592`). Add `custom` to the MAP (registry first model; empty registry → skip-with-log replaces the silent Gemini fallthrough); `_resolve_model_name`'s Auto branch (`489`) then picks it up for free.
- Portal: add `custom` to `CRON_PROVIDERS` + the cron modal options.
- Android: add `custom` to `cronProviders` (`438-439`); `selectProvider` (`529-584`) has its OWN 5-min `modelsCache` with a cache-hit early-return at `534-539` — skip it for `custom` (the bypass-all-caches rule; ChatViewModel's cache is a separate fix in Task 7.1); add a `custom` → "Custom (Local)" entry to `CRON_PROVIDER_LABELS` (`CronManagerScreen.kt:1163-1169`, else the dropdown shows the raw string `custom`).

Commit per surface file-set.

---

## Milestone 6 — Portal picker

### Task 6.1: Provider option + model hydration + optgroups

**Files:** Modify: `Portal/index.html` (`359-367` + `?v=` bumps at lines ~11, ~21), `Portal/modules/state-management.js` (`401-467`, `496-521`, `561-576`)

- `<option value="custom">Custom (Local Network)</option>` in `providerSelect`.
- `MODEL_CONFIG['custom'] = { name: 'Custom (Local Network)', models: [] }` — key is `name:`, NOT `label:` (verified against siblings at `403/412/422/431/440`); prevents the silent early-return (gotcha 6). Offline fallback empty is correct — models are inherently dynamic.
- `fetchAvailableModels` (`592-631`): skip the sessionStorage cache when `provider === 'custom'` (always hit the network — new downloads must appear immediately and the warm dot must be live).
- Per-server grouping + warm dot — TWO functions must change (verified: extending the CU branch alone does nothing for `custom`): (1) `buildHydratedModels` — its non-CU fallthrough (`572-575`) strips models to `{id, name}`, discarding `server`/`status`; add a `custom` branch that maps `server` INTO the `backend` field (so `updateModelDropdown`'s existing backend-keyed optgroup code at `506-511` groups per box unmodified) and prefixes `🟠 ` to `name` when `m.status === 'loaded'` (native `<option>` elements can't carry CSS dots; the emoji renders everywhere including the Android WebView). (2) `updateModelDropdown` — no change needed once `backend` carries the server alias (`BACKEND_LABELS[x] || x` falls back to the raw alias as the optgroup label). Auto entry prepends as usual (`default_id` from the catalog).
- Bump `?v=genuiNNN` → next number at BOTH occurrences.

**Verify (agent-driven Chrome):** hard-reload Portal → pick Custom → dropdown shows `gemma-26b (gemma-box)` etc. under a `gemma-box` optgroup with 🟠 on exactly the resident model → send "What time is it? Use your tools." on `gemma-box::gemma-26b` → reasoning bubble renders (separate channel), `get_current_time` executes, answer streams → reopen the dropdown after chatting on a different model and confirm the dot moved. Screenshot for the record. Commit.

---

## Milestone 7 — Android native picker

### Task 7.1: Provider enum + hydration mapping + fallback config

**Files (under `AI_BlackBox_Portal_Android_MVP (2)/…/AI_BlackBox_Portal/`):**
- Modify: `data/model/Provider.kt` (`3-35`), `ui/chat/ChatViewModel.kt` (`mapProviderForApi`, `2657-2666`), `util/Constants.kt` (`MODEL_CONFIG`, `66-146`), `ui/settings/SettingsSheet.kt` (`527-614`), `NativeMainActivity.kt` (`885-907` — verify `custom` falls to the plain-chat else branch)
- Test: extend the existing unit suites (`ConstantsLiveDefaultsTest` pins MODEL_CONFIG shape — update it deliberately)

Steps:

1. **Failing unit tests** — NEW file `ChatProviderCustomTest` mirroring `ChatProviderLocalTest.kt` (which pins fromId round-trip + isStreaming): `ChatProvider.fromId("custom")`, `CUSTOM.isStreaming == true`, `mapProviderForApi(CUSTOM) == "custom"`. (Verified: `ConstantsLiveDefaultsTest` pins only the gemini-live default — adding a `custom` MODEL_CONFIG entry cannot break it; no test pins MODEL_CONFIG's key set.)
2. Implement `CUSTOM("custom", displayName "Custom (Local)")` keeping `isStreaming == true`; map it in `mapProviderForApi`; `Constants.MODEL_CONFIG["custom"]` minimal entry (Auto only) so SettingsSheet doesn't hide the section (it reads ONLY Constants — the model list there shows Auto until a liveModels rework, while the Composer pill hydrates fully).
3. **Status data path (verified: none exists)** — `liveModels` is `List<Pair<String,String>>` end-to-end (`ChatViewModel.kt:346`, cache `2671`, `Composer.kt:112`, wired at `NativeMainActivity.kt:919`); don't widen the type. Follow the existing multi-origin precedent `_cuModelBackends` (`ChatViewModel.kt:2674-2678`): add a parallel `_customModelStatus: StateFlow<Map<String, String>>` (model id → status) populated by `fetchLiveModels`, threaded to the Composer like `cuModelBackends` is, rendering an orange dot (Compose, color `0xFFFF9800`) on rows where status == "loaded".
4. `fetchLiveModels` (`2680-2759`): skip the 5-min in-memory cache for `custom`; parse the additive `status` field tolerantly. **Empty-catalog staleness fix (verified bug-in-waiting):** the function only publishes when `models.isNotEmpty()` (`2719`) and never clears `_liveModels` — switching to Custom with an empty/dead registry would keep showing the PREVIOUS provider's models as selectable custom models. When custom returns zero models, explicitly publish an empty (or Auto-only) list and clear `_customModelStatus`.
5. Auto-navigate: verify `custom` falls to the plain-chat `else` in BOTH when-blocks — `NativeMainActivity.kt:885-907` AND the parallel `SettingsSheet.kt:561-567` (both currently pass).
6. `./gradlew :app:testDebugUnitTest --offline` (~35 s). Build APK, sideload to the Fold, device-validate: pick Custom, models hydrate from `/models/custom` with the warm dot on the resident model, send a chat with a tool call over Tailscale. Commit.

**Note:** the wizard "+" UI needs ZERO Android work — `WizardWebViewScreen` renders the live wizard, and alias/URL/key are plain text entry (validated in-app).

---

## Milestone 8 — Hardening, fresh-box gate, docs, ship

### Task 8.1: Fresh-box simulation

Temporarily move the registry aside (`mv credentials/custom_models.json /tmp/…`), restart service:
- `GET /models/custom` → 200 empty (not 404, not 500); Portal Custom shows empty dropdown gracefully (the 2026-07-07 Android empty-dropdown parse fix territory — verify no crash); **Android: switching to Custom with the empty registry must show an EMPTY model list, not the previous provider's models** (the Task 7.1 step-4 staleness fix under test); wizard shows zero rows + "+ Add"; `/chat/stream` provider=custom → clean SSE error event, no traceback; hub tile summary shows no custom mention.
Restore the file. This is the production-quality-portable gate (memory).

### Task 8.2: Concurrency + cold-load reality check

llama-swap serializes per model (`--parallel 1`) and swaps on model change. Three live checks: (a) fire two concurrent `/chat/stream` requests on `gemma-26b` → both complete (queued, keepalives hold both frontends); (b) request `gemma-31b` cold → SSE keepalive comments keep the stream alive through the ~20 s load and the reply arrives; (c) after (b), `GET /models/custom` shows the 🟠 status moved from `gemma-26b` to `gemma-31b` (swap semantics reflected live). Document the "don't ping-pong models" caveat in the wizard section copy if not already.

### Task 8.3: Docs + CLAUDE.md + snapshot

- Append a "Custom model servers (provider `custom`)" subsection to `CLAUDE.md` Key Services / API Endpoints: registry path, endpoints, qualified-id scheme, fresh-read activation.
- `git push` after Brandon's device validation (staging-as-prod rule).
- Mint the dev snapshot via `/snapshot-dev` (auto-mint mechanics per `.claude/commands/snapshot-dev.md`).

---

## Explicit non-goals (this iteration)

- SMS reply routing to custom models (free-text model field half-works today; Anthropic-specific thread constraints in `sms/router.py:541-556`).
- Device-control default-provider enum, voice-agent catalogs (realtime-audio-only).
- Embeddings/reranker/Whisper from the utility box (separate feature; INTEGRATION.md §10 routes those elsewhere — the reranker tiering seam already exists).
- Per-model tool/vision capability flags (no such machinery exists for any provider; llama.cpp returns 500 on image-to-text-model, surfaced as a normal error event).
- Provider-list hydration (static provider entries remain the pattern on all surfaces).
