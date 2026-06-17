# On-Device Gemma (`local` Provider) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `local` model provider whose Gemma 4 E2B/E4B models run on the user's Android phone, control the phone via AccessibilityService, and call back to the BlackBox as a tool/memory server — rendered in the existing BlackBox UI.

**Architecture:** Inside-out inversion. The agent loop runs on-device (LiteRT-LM + AI Edge Function Calling SDK). Two actuators: AccessibilityService (phone control, UI-tree-first) and an HTTP tool-bridge to the Orchestrator (the full ToolVault via semantic retrieval). The Orchestrator gains only thin endpoints (tool bridge, model mirror, device attestation, persona). Strict per-operator device binding; the on-device model is never a server-reachable endpoint.

**Tech Stack:** Backend — FastAPI (Python 3.12), ToolVault v2 (`execute_tool`/`meta_tool`), `device_registry`, pytest. Android — Kotlin, Jetpack Compose, LiteRT-LM, AI Edge On-Device Function Calling SDK, AccessibilityService, MediaProjection, JUnit/Robolectric + UIAutomator. compileSdk 36 / minSdk 26 (on-device features runtime-gated to API 31+).

**Design of record:** `docs/plans/2026-06-14-on-device-gemma-local-provider-design.md`

**Sub-skills to apply throughout:**
- @superpowers:test-driven-development — every task is test-first.
- @superpowers:verification-before-completion — run the command, confirm output, before claiming done.
- @superpowers:requesting-code-review — at each phase boundary.

**Conventions:**
- Backend tests live in `Orchestrator/tests/`; run with `python -m pytest`.
- Android unit tests in `app/src/test/...`; instrumented tests in `app/src/androidTest/...`.
- Never `git add -A`. Stage explicit paths only.
- After each phase: `python -m Orchestrator.toolvault.validate` (if ToolVault touched) + request code review.
- Android root: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/` — referred to below as `<ANDROID>`. App package root: `<ANDROID>/app/src/main/java/com/aiblackbox/portal/` — referred to as `<PKG>`.

---

## Phase 0 — Backend contracts (no phone needed)

**Outcome:** The Orchestrator exposes the tool bridge, device attestation, persona endpoint, and a conditional `local` catalog entry. Fully unit-tested without any device.

> **Module decision (refines the design doc):** On-device model records are operator-bound, not mesh-controllable ADB devices. To avoid polluting the ADB `device_registry`, put the new state in a dedicated cohesive module `Orchestrator/local_provider/`. Cross-reference this in the design doc's "Open decisions."

### Task 0.1: Local-provider attestation registry

**Files:**
- Create: `Orchestrator/local_provider/__init__.py`
- Create: `Orchestrator/local_provider/registry.py`
- Test: `Orchestrator/tests/test_local_provider_registry.py`

**Step 1: Write the failing test**
```python
# Orchestrator/tests/test_local_provider_registry.py
from Orchestrator.local_provider.registry import get_local_registry

def test_attest_then_status_roundtrip(tmp_path, monkeypatch):
    import Orchestrator.local_provider.registry as r
    monkeypatch.setattr(r, "STORE_FILE", tmp_path / "local_devices.json")
    reg = r.LocalProviderRegistry()  # fresh, file-backed
    reg.attest(operator="Brandon", device_id="pixel-9", model_slug="gemma-4-e4b",
               version="1.0", sha256="abc", delegate="gpu", autonomy_mode="permission")
    status = reg.status(operator="Brandon")
    assert status["available"] is True
    assert status["models"][0]["model_slug"] == "gemma-4-e4b"
    assert status["models"][0]["autonomy_mode"] == "permission"

def test_status_unknown_operator_is_unavailable(tmp_path, monkeypatch):
    import Orchestrator.local_provider.registry as r
    monkeypatch.setattr(r, "STORE_FILE", tmp_path / "local_devices.json")
    reg = r.LocalProviderRegistry()
    assert reg.status(operator="Nobody")["available"] is False
```

**Step 2: Run → expect FAIL** (`ModuleNotFoundError`): `python -m pytest Orchestrator/tests/test_local_provider_registry.py -v`

**Step 3: Implement `registry.py`** — a singleton mirroring `device_registry/registry.py` (load/save JSON, `STORE_FILE` module-level Path). Keyed `operator -> {device_id -> record}`. Methods: `attest(...)` (upsert, sets `verified_at=time.time()`), `status(operator)` → `{"available": bool, "models": [records]}`, `set_autonomy(operator, device_id, mode)`, `remove(operator, device_id)`. Provide `get_local_registry()` singleton accessor.

**Step 4: Run → expect PASS.**

**Step 5: Commit** `git add Orchestrator/local_provider/ Orchestrator/tests/test_local_provider_registry.py && git commit -m "feat(local): operator-bound on-device attestation registry"`

### Task 0.2: Tool-bridge endpoints (`/local/tools/search`, `/local/tools/execute`)

**Files:**
- Create: `Orchestrator/routes/local_routes.py` (mirror the registration style of `Orchestrator/routes/gmail_routes.py` — it attaches handlers to the shared `app`)
- Test: `Orchestrator/tests/test_local_routes.py`

**Step 1: Write the failing test** (use FastAPI `TestClient` against the app; follow whatever app-import fixture `test_local_provider_registry`-adjacent route tests already use — check `Orchestrator/tests/` for an existing route-test fixture and reuse it).
```python
def test_tools_search_returns_schemas(client):
    resp = client.post("/local/tools/search", json={"query": "search my memory", "operator": "Brandon", "k": 3})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert isinstance(body["tools"], list) and len(body["tools"]) >= 1
    assert "name" in body["tools"][0] and "parameters" in body["tools"][0]

def test_tools_execute_routes_through_execute_tool(client, monkeypatch):
    import Orchestrator.routes.local_routes as lr
    async def fake_exec(tool, params, operator):
        class R: success=True; result={"echo": tool, "op": operator}
        return R()
    monkeypatch.setattr(lr, "execute_tool", fake_exec)
    resp = client.post("/local/tools/execute",
                       json={"tool": "search_snapshots", "params": {"query":"x"}, "operator":"Brandon"})
    assert resp.json() == {"success": True, "result": {"echo":"search_snapshots","op":"Brandon"}}
```

**Step 2: Run → expect FAIL** (404 / no route).

**Step 3: Implement `local_routes.py`:**
```python
# /local/tools/search  → semantic tool retrieval via ToolVault meta_tool
# /local/tools/execute → full-suite tool execution scoped by caller-asserted operator
from Orchestrator.toolvault import meta_tool
from Orchestrator.tools.blackbox_tools import execute_tool  # same import gmail_routes uses

@app.post("/local/tools/search")
async def local_tools_search(request: Request):
    body = await request.json()
    query = (body or {}).get("query") or ""
    k = int((body or {}).get("k") or 5)
    if not query:
        return JSONResponse({"success": False, "error": "query required"}, status_code=400)
    res = meta_tool.execute("search", query=query)         # MetaToolResult
    tools = res.tools[:k] if getattr(res, "tools", None) else []  # adapt to MetaToolResult shape
    return {"success": True, "tools": tools}

@app.post("/local/tools/execute")
async def local_tools_execute(request: Request):
    body = await request.json()
    tool = (body or {}).get("tool")
    operator = (body or {}).get("operator") or "system"
    params = dict((body or {}).get("params") or {})
    if not isinstance(tool, str) or not tool:
        return JSONResponse({"success": False, "error": "tool required"}, status_code=400)
    params["operator"] = operator
    result = await execute_tool(tool, params, operator)
    return {"success": bool(result.success), "result": result.result}
```
> Adapt `res.tools` to the real `MetaToolResult` field (see `Orchestrator/toolvault/meta_tool.py:_action_search`). If `_action_search` returns rendered text, add a `tools` list to its result or post-process here. Add a Task 0.2b if a `MetaToolResult.tools` field must be added (test-first in `meta_tool`).

**Step 4: Run → expect PASS.**
**Step 5: Commit** explicit paths.

### Task 0.3: Device attestation + status endpoints

**Files:** Modify `Orchestrator/routes/local_routes.py`; Test: extend `test_local_routes.py`.

- `POST /local/device/attest` — body `{operator, device_id, model_slug, version, sha256, delegate, autonomy_mode}` → `get_local_registry().attest(...)` → `{"success": True}`.
- `GET /local/device/status?operator=` → `get_local_registry().status(operator)`.
- `POST /local/device/autonomy` — body `{operator, device_id, mode}` → `set_autonomy(...)`.

Test: attest then `GET status` shows `available: true`; autonomy POST flips the mode. Commit.

### Task 0.4: Persona endpoint

**Files:** Modify `local_routes.py`; Test: extend `test_local_routes.py`.

- `GET /local/system-prompt?operator=` → return `{"prompt": <text>, "version": <hash>}` built from `behavioral_core.py` (import the same builder the chat path uses; grep `behavioral_core` in `Orchestrator/routes/chat_routes.py` for the exact call). Test asserts a non-empty prompt + a stable version hash for identical inputs. Commit.

### Task 0.5: Conditional `local` catalog entry

**Files:** Modify the centralized model catalog (per `docs/plans/2026-05-18-model-list-centralization.md`; grep for where `/models/{provider}` is served). Test: `Orchestrator/tests/test_local_catalog.py`.

- Add provider `local` with models `gemma-4-e2b`, `gemma-4-e4b`, flagged `on_device: true`, **no server inference path**.
- `GET /models/local?operator=Brandon` returns the models **only if** `get_local_registry().status(operator)["available"]`; otherwise returns `{"models": [], "available": false, "reason": "no verified on-device model"}`.
- Test both branches (verified operator → models; unverified → empty + reason). Commit.

**Phase 0 exit:** `python -m pytest Orchestrator/tests/test_local_*.py -v` all green; `python -m Orchestrator.toolvault.validate` clean; request code review (@superpowers:requesting-code-review).

---

## Phase 1 — Model acquisition (BlackBox mirror + Android Model Manager)

**Outcome:** A user taps "Download Gemma 4 E4B" in the system menu; bytes come from the BlackBox mirror over Tailscale; the app verifies the checksum, attests to the registry, and `local` appears in the picker. No reasoning yet.

### Task 1.1: Server-side mirror — catalog

**Files:** Modify `local_routes.py`; Create `Orchestrator/local_provider/mirror.py`; Test `Orchestrator/tests/test_local_mirror.py`.

- `GET /local/models/catalog` → list of downloadable bundles: `{slug, display_name, size_bytes, sha256, min_ram_gb, recommended_for}` for E2B and E4B. Source of truth: a small `BUNDLES` dict in `mirror.py` (slug → local mirror path + metadata).
- Test: catalog returns both bundles with required fields. Commit.

### Task 1.2: Server-side mirror — fetch-once + ranged download

**Files:** Modify `mirror.py`, `local_routes.py`; Test extend `test_local_mirror.py`.

- `mirror.ensure_present(slug)` — if the bundle file is absent, download once from Hugging Face using the project HF token (`HF_TOKEN` env; Gemma license accepted server-side), store under a configured mirror dir, record sha256.
- `GET /local/models/download/{slug}` — stream the file with **HTTP Range/resume** support (`Accept-Ranges: bytes`, honor `Range:` header, 206 partial). Return 404 for unknown slug.
- Tests: range request returns 206 + correct byte slice (use a tiny fixture file, monkeypatch `BUNDLES` path); unknown slug → 404. Mock the HF fetch. Commit.

### Task 1.3: Android — `LocalModelApi` client

**Files:** Create `<PKG>/data/api/LocalModelApi.kt`; Modify `<PKG>/data/api/BlackBoxApi.kt` (add base-URL-aware calls if needed); Test `<ANDROID>/app/src/test/java/com/aiblackbox/portal/data/api/LocalModelApiTest.kt`.

- Methods: `catalog(): List<LocalBundle>`, `download(slug, destFile, onProgress): DownloadResult` (ranged/resumable via OkHttp, writes to app-private storage), `attest(req): Boolean`, `status(operator): LocalStatus`.
- Test with MockWebServer: catalog parse; resumed download writes expected bytes; attest posts correct JSON. Commit.

### Task 1.4: Android — `LocalModelManager` (storage + verify + attest)

**Files:** Create `<PKG>/data/model/LocalBundle.kt` (+ `LocalStatus`), `<PKG>/data/local/LocalModelManager.kt`; Test `.../data/local/LocalModelManagerTest.kt` (Robolectric).

- `installedModels(): List<InstalledModel>` (scan app-private dir), `recommendForDevice(): String` (E2B vs E4B from `ActivityManager.MemoryInfo` / `Build` — threshold in one constant), `verify(slug): Boolean` (sha256 match vs catalog), `download+verify+attest` orchestration, `delete(slug)`.
- Tests: recommend returns E2B under threshold / E4B above (inject a fake mem provider); verify fails on checksum mismatch. Commit.

### Task 1.5: Android — Model Manager UI in the system menu

**Files:** Create `<PKG>/ui/settings/LocalModelSection.kt`; Modify `<PKG>/ui/settings/SettingsSheet.kt` (add the section) + `SettingsViewModel.kt` (state: catalog, download progress, installed, autonomy toggle); Test `.../ui/settings/LocalModelSectionTest.kt` (Compose UI test).

- UI: list bundles with size + "Recommended for your phone" badge; Download button → progress bar (resume on retry); Installed → Switch/Delete; the **YOLO ⇄ Permission** toggle (calls `/local/device/autonomy`); the **Enable AccessibilityService** call-to-action (deep-link to settings) — wired in Phase 4, present-but-disabled here.
- Test: progress state renders; toggle calls the VM. Commit.

### Task 1.6: Picker shows `local` only when verified

**Files:** Modify `<PKG>/data/model/Provider.kt` (add `LOCAL("local","On-Device (Gemma)")` + `val isLocal`); Modify the provider-picker composable + its VM to include `LOCAL` only when `LocalModelManager.installedModels()` is non-empty AND `/local/device/status` is available; Test the VM filter.

- On app open: app calls `attest` (re-attest installed model) then `status`; picker gates on the result.
- Test: empty installed → `LOCAL` absent; one installed + available → present. Commit.

**Phase 1 exit:** On a real phone, download E4B from the BlackBox, see it verified in `GET /local/device/status`, and `local` appears in the picker (selecting it does nothing yet). Request code review.

---

## Phase 2 — On-device runtime + chat (no tools, no phone control)

**Outcome:** Select `local / Gemma 4 E4B`, hold a text **and** voice conversation rendered in the existing chat UI, snapshotted to memory. Voice via existing `/ws/stt` + `/tts/catalog`.

> **SDK adaptation note:** Tasks 2.1–2.2 wrap the real LiteRT-LM and AI Edge FC SDK APIs. Pin exact Gradle coordinates + class names from the `google-ai-edge/gallery` reference app during execution; the contracts below are stable, the exact SDK calls must be matched to the shipped library.

### Task 2.1: Gradle deps + `LiteRtEngine` wrapper

**Files:** Modify `<ANDROID>/app/build.gradle` (add LiteRT-LM + AI Edge FC SDK deps); Create `<PKG>/data/local/LiteRtEngine.kt`; Test `.../data/local/LiteRtEngineTest.kt` (interface-level test with a fake backend — real inference is an instrumented smoke test, Task 2.6).

- Interface `LocalLlm { fun load(modelFile, delegate); fun generate(prompt, onToken): Flow<String>; fun close() }`.
- `LiteRtEngine` implements it over LiteRT-LM; delegate from the attested record (CPU/GPU/NPU).
- Test: a `FakeLocalLlm` streams canned tokens through the Flow contract (proves the streaming seam the UI depends on). Commit.

### Task 2.2: `FcLoop` — agent loop over the FC SDK (no tools yet)

**Files:** Create `<PKG>/data/local/FcLoop.kt`; Test `.../data/local/FcLoopTest.kt`.

- `FcLoop(llm, toolRegistry, actuators)` drives: assemble prompt (persona + resident tools — empty set in Phase 2) → generate → if structured tool call, dispatch + feed result back → else stream assistant text. Phase 2: no tools, so it just streams text.
- Test with `FakeLocalLlm`: plain-text turn streams to the sink; (tool-call path covered in Phase 3). Commit.

### Task 2.3: Persona fetch + cache

**Files:** Create `<PKG>/data/local/PersonaCache.kt`; Test `.../PersonaCacheTest.kt` (MockWebServer + Robolectric prefs).
- Fetch `GET /local/system-prompt?operator=`, cache with version; serve cached when offline. Test: fetch caches; offline returns cached. Commit.

### Task 2.4: Wire `local` into `ChatViewModel.sendMessage()`

**Files:** Modify `<PKG>/ui/chat/ChatViewModel.kt` (the `when` at ~line 411: add `provider.isLocal -> sendViaLocalEngine(...)`); Create `sendViaLocalEngine` that runs `FcLoop` and emits into the **same** `UiMessage` token sink `sendViaSSE` uses; Test `.../ui/chat/ChatViewModelLocalTest.kt`.
- Test (Robolectric, `FakeLocalLlm` injected): selecting `local` + sending text produces streamed assistant tokens in the same message-state flow as SSE. Commit.

### Task 2.5: Memory write-back (+ offline queue)

**Files:** Create `<PKG>/data/local/LocalSnapshotQueue.kt`; Modify `sendViaLocalEngine` to `saveConversation()` (`/chat/save`, tagged `provider=local`) on turn end; queue locally if offline, flush on reconnect; Test `.../LocalSnapshotQueueTest.kt`.
- Test: online → posts immediately; offline → enqueues; reconnect → flushes in order. Commit.

### Task 2.6: Voice reuse + instrumented runtime smoke

**Files:** Verify the existing voice path (`<PKG>/ui/voice/...`, `/ws/stt`, `/tts/catalog`) calls the model layer such that `local` slots in (STT→FcLoop→TTS); minimal wiring if needed. Create instrumented smoke `<ANDROID>/app/src/androidTest/.../LiteRtSmokeTest.kt` (load real bundle, generate ≥1 token) — `@RequiresDevice`.
- Verify on device: text + voice conversation on Gemma renders in the BlackBox UI and a snapshot appears via `search_snapshots`. Commit.

**Phase 2 exit:** Real-device text + voice chat on the on-device model, snapshotted. Request code review. **This is the "it's a real provider" milestone.**

---

## Phase 3 — BlackBox tool-bridge (full ToolVault from the phone)

**Outcome:** The on-device Gemma can search memory, generate images (cloud), and use any ToolVault tool via tiered retrieval — `search_tools` resident, everything else pulled on demand.

### Task 3.1: `ToolBridgeClient`

**Files:** Create `<PKG>/data/local/ToolBridgeClient.kt`; Test `.../ToolBridgeClientTest.kt` (MockWebServer).
- `searchTools(query, k): List<ToolSchema>` → `POST /local/tools/search`.
- `execute(tool, params, operator): ToolResult` → `POST /local/tools/execute`.
- Test: search parses schemas; execute posts `{tool, params, operator}` and parses `{success, result}`. Commit.

### Task 3.2: `search_tools` as a resident on-device function

**Files:** Modify `<PKG>/data/local/FcLoop.kt` + a `ResidentTools.kt`; Test extend `FcLoopTest.kt`.
- Register one always-resident function `search_tools(query)` whose executor calls `ToolBridgeClient.searchTools` and **injects the returned schemas as callable functions for the next turn**.
- Test (`FakeLocalLlm` scripted): turn 1 emits `search_tools("generate an image")` → bridge returns the `generate_image` schema → turn 2 emits `generate_image(...)` → `ToolBridgeClient.execute` is called → result fed back → assistant text streams. Assert the two-hop sequence + that the model never saw >N schemas at once. Commit.

### Task 3.3: Result rendering parity

**Files:** Modify `sendViaLocalEngine` so tool-call + tool-result events render in the chat UI identically to the cloud path (reuse the same `UiMessage` tool-event types); Test the VM emits tool-call/tool-result UI events for a bridged call. Commit.

### Task 3.4: Graceful offline tool behavior

**Files:** Modify `ToolBridgeClient` + `FcLoop`; Test.
- When the mesh is unreachable, a tool call returns a structured "needs connection" `ToolResult` the model can verbalize (not a crash). Test: bridge timeout → graceful result string surfaced. Commit.

**Phase 3 exit:** On device, ask Gemma to "search my snapshots for X" and "generate an image of Y" — both work via the bridge, rendered inline. Request code review.

---

## Phase 4 — Phone control ⭐ (the headline; gated behind 0–3 solid)

**Outcome:** "Use my phone" works. AccessibilityService actuator (UI-tree-first, vision fallback), the ~12 resident phone tools, the YOLO/Permission confirm-gate, and credential redaction.

> **Highest-risk phase.** Do not start until Phases 0–3 pass code review on a real device. Security review is mandatory at exit.

### Task 4.1: AccessibilityService skeleton + manifest + enablement

**Files:** Create `<PKG>/overlay/BlackBoxA11yService.kt`; Modify `<ANDROID>/app/src/main/AndroidManifest.xml` (service + `BIND_ACCESSIBILITY_SERVICE` + `accessibility_service_config.xml`); wire the "Enable" CTA from Task 1.5 to the system settings deep-link; Test instrumented `.../androidTest/.../A11yServiceTest.kt`.
- Test (UIAutomator against a bundled test Activity): service reads the root node and finds a labeled button. Commit.

### Task 4.2: UI-tree reader → `read_screen`

**Files:** Create `<PKG>/overlay/UiTreeReader.kt`; Test instrumented.
- Walk `AccessibilityNodeInfo` → compact JSON of actionable nodes `{node_id, role, text, bounds, clickable, editable, isPassword}`. **Redact `isPassword` node text** here (replace with `"·····"`).
- Test: a layout with a password field yields a redacted node; a normal button yields its text. Commit.

### Task 4.3: Gesture actuators (`tap`, `type`, `swipe`, `scroll`, `open_app`, `back`, `home`)

**Files:** Create `<PKG>/overlay/Actuators.kt`; Test instrumented against the test Activity.
- `tap(node_id)` via `performAction(ACTION_CLICK)` (or `dispatchGesture` fallback); `type(node_id, text)` via `ACTION_SET_TEXT`; `swipe/scroll` via `dispatchGesture`; `open_app(pkg)` via launch intent; `back/home` via global actions.
- Test: tap toggles a test button's state; type sets a field's text. Commit.

### Task 4.4: Vision fallback (MediaProjection → Gemma)

**Files:** Create `<PKG>/overlay/ScreenCapture.kt`; Modify `FcLoop` to attach a screenshot to the model input when `read_screen` returns a thin/empty tree; Test the "thin tree → request screenshot" decision (unit) + an instrumented capture smoke.
- **Redact**: never capture while a password field is focused (skip → request manual entry). Commit.

### Task 4.5: Register the 12 actuators as resident on-device functions

**Files:** Modify `ResidentTools.kt` + `FcLoop.kt`; Test (`FakeLocalLlm`): a scripted "open Settings and read the screen" emits `open_app` then `read_screen` and dispatches to the actuator layer (mocked). Commit.

### Task 4.6: Autonomy gate (YOLO vs Permission) at the actuator

**Files:** Create `<PKG>/overlay/ConfirmGate.kt`; Modify `Actuators.kt` to wrap the high-consequence set (`type`-into-password/login, send, post, pay, delete, grant-permission, install) in a confirm-future; Test.
- Permission mode → high-consequence action suspends until an on-screen confirm resolves; YOLO → resolves immediately; benign actions never gate. Mode read from the attested record / autonomy endpoint.
- Test (Robolectric): high-consequence call in Permission mode awaits confirmation; in YOLO proceeds. Commit.

### Task 4.7: Credential handoff

**Files:** Modify `Actuators.kt` + `FcLoop`; integrate Android Credential Manager / Autofill; Test the handoff decision.
- On a login form with no saved credential → emit a "user, please enter your password" handoff (confirm-gate variant), pause, resume after. Saved credential → trigger the system autofill picker. The model/loop never receives keystrokes from password fields.
- Test: focused password field → loop takes the handoff branch, not the `type` branch. Commit.

### Task 4.8: End-to-end phone-control verification

**Files:** none (manual + instrumented scenario).
- On device: "Open Settings, go to Display, tell me the brightness level" (read-only, autonomous). Then a gated one: "Open <app> and send <contact> a message" → confirm-gate fires in Permission mode. Then YOLO repeat. Verify redaction by attempting a login (password never appears in transcript or snapshot). Commit any fixes.

**Phase 4 exit:** "Use my phone" works in both modes; redaction verified. **Run @superpowers:security-review on the AccessibilityService + redaction surface.** Request code review.

---

## Phase 5 — Hardening & production

**Outcome:** Production-ready: offline resilience, battery/thermal sanity, error recovery, isolation proof.

### Task 5.1: Offline queue resilience
Stress the `LocalSnapshotQueue` (airplane mode mid-session, app kill/restart): no lost mints, ordered flush. Persist the queue to disk. Test + device verify. Commit.

### Task 5.2: Battery/thermal management
Add a thermal listener (`PowerManager.OnThermalStatusChangedListener`); back off generation cadence under `THERMAL_STATUS_SEVERE`; surface a UI notice. Test the back-off decision (unit). Commit.

### Task 5.3: Error recovery
Model-load failure, OOM on E4B (suggest E2B), bridge 5xx, actuator timeouts — each surfaces a clear, recoverable message, never a silent hang (cf. the telemetry-before-fixes lesson). Add structured logging. Test the failure→message mapping. Commit.

### Task 5.4: Multi-tenant isolation proof
Test: operator B's `/local/device/status` never exposes operator A's model; the picker on a phone signed in as B shows only B's model; there is no server route that reaches A's on-device engine. Document the negative result. Commit.

### Task 5.5: Web Portal picker note
Modify the web Portal model picker to render `local` as "available on your phone only" (disabled), per the design's surface exception. Commit.

### Task 5.6: Docs + final review
Update `CLAUDE.md` (new `local` provider + the `/local/*` endpoints) and `ANDROID_INTEGRATION.md`. Final @superpowers:requesting-code-review across the branch.

**Phase 5 exit:** Production-ready. Merge per @superpowers:finishing-a-development-branch.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| LiteRT-LM / FC SDK API drift vs. this plan | Contracts are stable; pin exact coordinates from `google-ai-edge/gallery` at execution. Tasks 2.1–2.2 isolate the SDK behind `LocalLlm`/`FcLoop` interfaces. |
| Small-model function-calling accuracy | Tiered tool surface keeps resident schemas tiny; E4B over E2B when RAM allows; resident actuators use node IDs not pixels. |
| AccessibilityService = god-mode blast radius | Permission default; actuator-level gate (not model judgment); mandatory security review (Phase 4 exit); structural credential redaction. |
| Gemma redistribution terms (mirror) | Private/hub use within Gemma license; revisit to direct-HF if terms change (design "Open decisions"). |
| minSdk 26 vs on-device requirements | Runtime-gate on-device features to API 31+; `local` simply absent from the picker on older devices. |

## Execution order summary
Phase 0 (backend, no phone) → 1 (acquisition) → 2 (runtime+chat) → 3 (tool-bridge) → 4 (phone control ⭐) → 5 (hardening). Value is usable from end of Phase 2; the riskiest surface (Phase 4) lands on a verified foundation.
