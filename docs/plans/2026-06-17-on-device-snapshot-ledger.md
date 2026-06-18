# On-device Snapshot Ledger & Context Pipeline — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Wire the on-device Gemma `local` provider into the BlackBox's server-side context-assembly + minting pipeline, so every turn is pre-assembled per-operator (most-recent checkpoint + top 2-3 semantic snapshots + top-K directly-callable tools), run on-phone, and minted back as a searchable snapshot — giving the on-device model the "teleport" (remember everything, talk about anything).

**Architecture:** Server-bracketed turn — `POST /local/turn/prepare` assembles a lean, token-budgeted package; the phone runs the native E4B loop on it (direct tools + `search_snapshots`/`find_blackbox_tool` fallbacks, result-trimming, soft-stop); `POST /local/turn/complete` auto-mints the raw turn + provenance. Reuses `context_builder.build_fossil_context`, `checkpoint.perform_mint`, the checkpoint cadence, and the shipped `LocalSnapshotQueue`. Design: `docs/plans/2026-06-17-on-device-snapshot-ledger-design.md`.

**Tech Stack:** Python/FastAPI backend (`Orchestrator/`), pytest; Kotlin/Android (`AI_BlackBox_Portal_Android_MVP/.../portal/`), JUnit + MockWebServer; litertlm-android 0.13.1.

**Conventions:** TDD per task (failing test → minimal impl → green → commit). Explicit `git add <paths>` only — NEVER `git add -A`. Backend tests: `Orchestrator/venv/bin/python -m pytest <path> -q`. Android unit gate: `./gradlew :app:testDebugUnitTest --offline`. Keystone (Phase 1) is front-loaded per Brandon.

---

## Phase 1 — BACKEND KEYSTONE (the context pipeline)

### Task 1: `local` lean retrieval profile in `build_fossil_context`

**Files:**
- Modify: `Orchestrator/context_builder.py`
- Test: `Orchestrator/tests/test_context_builder_local.py` (create)

**Step 1: Write the failing test**
```python
# test_context_builder_local.py
from unittest import mock
from Orchestrator import context_builder as cb

def test_local_profile_is_lean_and_capped():
    # local profile: 1 checkpoint, <=3 semantic, NO recent, NO keyword
    with mock.patch.object(cb, "semantic_retrieve", return_value=["S1","S2","S3","S4","S5"]) as sem, \
         mock.patch.object(cb, "get_recent_checkpoints_for_operator", return_value=["CP1"]) as cp, \
         mock.patch.object(cb, "get_recent_fossils_for_operator", return_value=["R1"]) as rec, \
         mock.patch.object(cb, "keyword_retrieve_for_operator", return_value=["K1"]) as kw, \
         mock.patch.object(cb, "read_text_safe", return_value=""), \
         mock.patch.object(cb, "get_recent_media_artifacts", return_value=[]):
        text, prov = cb.build_fossil_context(
            "roll dice", "Brandon", provider="local",
            semantic_k=3, checkpoint_count=1, include_recent=False, include_keyword=False,
        )
    sem.assert_called_once()
    assert sem.call_args.kwargs["k"] == 3
    cp.assert_called_once()
    assert cp.call_args.kwargs["count"] == 1
    rec.assert_not_called()          # recent skipped for local
    kw.assert_not_called()           # keyword skipped for local
    assert prov["recent"] == [] and prov["keyword"] == []
    assert len(prov["semantic"]) <= 3 and prov["checkpoint"] == ["CP1"]

def test_local_provider_cap_reserves_loop_headroom():
    assert cb.PROVIDER_CAPS["local"] <= 16000   # ~4K tokens, leaves ~12K of 16K for the loop
```

**Step 2: Run → fails** (`semantic_k` kwarg unknown / `"local"` missing). `Orchestrator/venv/bin/python -m pytest Orchestrator/tests/test_context_builder_local.py -q`

**Step 3: Minimal impl** in `context_builder.py`:
- Add `"local": 16000` to `PROVIDER_CAPS` (chars ≈ 4K tokens; comment: reserves ~12K of the phone's 16K window for the agent loop).
- Add optional params to `build_fossil_context(...)`: `semantic_k: int | None = None, checkpoint_count: int | None = None, include_recent: bool = True, include_keyword: bool = True`.
- Use them: `SF = semantic_k if semantic_k is not None else CFG.getint(...)`; `CP = checkpoint_count if checkpoint_count is not None else CFG.getint(...)`; gate the `get_recent_fossils_for_operator` call on `include_recent` (else `recent_snaps=[]`), and `keyword_retrieve_for_operator` on `include_keyword` (else `keyword_snaps_raw=[]`). Preserve all current defaults so cloud callers are unchanged.

**Step 4: Run → pass.** **Step 5: Commit** `git add Orchestrator/context_builder.py Orchestrator/tests/test_context_builder_local.py && git commit -m "feat(ledger): lean 'local' retrieval profile + cap in build_fossil_context"`

---

### Task 2: Top-K tool-description injection helper

**Files:**
- Create: `Orchestrator/local_provider/tool_injection.py`
- Test: `Orchestrator/tests/test_tool_injection.py`

**Spec:** `build_injected_tools(query: str, k: int = 5) -> list[dict]` runs ToolVault semantic search (`meta_tool.execute("search", query=query)`) and returns up to `k` callable tool specs `{"name","description","parameters"}` (the JSON-Schema params from each tool's module). Empty query or no matches → `[]` (never raises). This is what the phone registers as *directly-callable* native tools.

**Step 1: failing test** — patch `meta_tool.execute` to return 2 tool dicts; assert `build_injected_tools("roll dice", k=5)` returns those 2 with `name`/`description`/`parameters`, and `build_injected_tools("", k=5) == []`.

**Step 2–4:** implement against `meta_tool` (mirror the existing `/local/tools/search` handler's call shape in `local_routes.py:160-192`), run, green.

**Step 5: Commit** `git add Orchestrator/local_provider/tool_injection.py Orchestrator/tests/test_tool_injection.py && git commit -m "feat(ledger): top-K tool-description injection helper"`

---

### Task 3: `POST /local/turn/prepare`

**Files:**
- Modify: `Orchestrator/routes/local_routes.py`
- Test: `Orchestrator/tests/test_local_turn_prepare.py`

**Spec:** request `{ "prompt": str, "operator": str }`. Response:
```json
{ "turn_id": "<uuid>", "system_prompt": "<persona + behavioral core + fossil context>",
  "tools": [ {"name","description","parameters"} ],   // top-K injected, directly callable
  "provenance": {"semantic":[...], "checkpoint":[...]},
  "budget": {"package_chars": int, "cap_chars": 16000} }
```
Behavior:
- Resolve operator from the body (per-operator is server-side authoritative). Blank operator → 400.
- `fossil, prov = build_fossil_context(prompt, operator, provider="local", semantic_k=3, checkpoint_count=1, include_recent=False, include_keyword=False)`.
- `tools = build_injected_tools(prompt, k=5)`.
- `system_prompt = get_behavioral_core("chat") + "\n\n" + fossil` (the persona/behavioral spec already used elsewhere).
- `budget.package_chars = len(system_prompt)`; include the `local` cap.
- Mint nothing here.

**Step 1: failing test** (FastAPI `TestClient`): patch `build_fossil_context`→(`"FOSSIL"`,{"semantic":["S1"],"checkpoint":["CP1"]}), `build_injected_tools`→[{"name":"roll_dice","description":"d","parameters":{}}], `get_behavioral_core`→"PERSONA". POST `{"prompt":"roll dice","operator":"Brandon"}`; assert 200, `system_prompt` contains "PERSONA" and "FOSSIL", `tools[0]["name"]=="roll_dice"`, `provenance["checkpoint"]==["CP1"]`, `budget["cap_chars"]==16000`. POST blank operator → 400.

**Step 2–4:** add `@app.post("/local/turn/prepare")` mirroring existing local handlers (module-top imports; `from fastapi import Request`/`Body`). Run, green. **Note for reviewer:** route-registration ORDER — register before any generic catch-alls (same caveat as the `/models/local` dispatch).

**Step 5: Commit** `git add Orchestrator/routes/local_routes.py Orchestrator/tests/test_local_turn_prepare.py && git commit -m "feat(ledger): POST /local/turn/prepare — per-turn context assembly"`

---

### Task 4: `POST /local/turn/complete` (mint + provenance)

**Files:**
- Modify: `Orchestrator/routes/local_routes.py`
- Test: `Orchestrator/tests/test_local_turn_complete.py`

**Spec:** request `{ "turn_id", "operator", "prompt", "final_response", "tool_transcript": [ {"name","args","result"} ] }`. Behavior:
- Compose the snapshot body server-side: `prompt` + `final_response` + a compact provenance block listing `tool_transcript` (names + artifact refs) — the 4B never authors this.
- Persist + auto-mint reusing the same path `/chat/save` uses (write the turn, then `perform_mint(operator, reason="LOCAL_TURN")` from `Orchestrator/checkpoint.py`), so the embedding is generated inline and the existing checkpoint cadence can fire.
- Response `{ "snap_id": "...", "checkpoint_triggered": bool }`.

**Step 1: failing test** — patch `perform_mint`→{"snap_id":"SNAP-X"}; POST a complete payload; assert 200 + `snap_id=="SNAP-X"`; assert the composed body passed to persistence contains the prompt, the response, and each `tool_transcript[].name` (provenance captured).

**Step 2–4:** implement; reuse `/chat/save` internals where possible (locate its handler; if it’s HTTP-only, factor the persist+mint into a shared helper and call it from both — do NOT duplicate mint logic). Run, green.

**Step 5: Commit** `git add Orchestrator/routes/local_routes.py Orchestrator/tests/test_local_turn_complete.py && git commit -m "feat(ledger): POST /local/turn/complete — mint raw turn + provenance"`

---

### Task 5: Backend integration smoke (live :9099 worktree backend)

**No code** — a verification gate before phone work.
- Start the worktree backend on :9099 (this worktree). `curl` `/local/turn/prepare {"prompt":"roll a six-sided dice","operator":"Brandon"}` → expect `tools` includes `roll_dice`, `system_prompt` non-empty, `budget.package_chars <= 16000`.
- `curl` `/local/turn/complete {...}` → expect `snap_id`; then `curl /local/tools/search` and confirm the new snapshot is semantically findable (teleport works).
- Document the actual `package_chars` for budget tuning (Task 11). Commit a short note in the plan file if numbers differ from assumptions.

---

## Phase 2 — PHONE (turn driver, tools, budget, offline)

> Package paths under `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/`. Reuse the shipped `BlackBoxApi`, `NativeTool`, `NativeToolCallingLlm`, `LocalSnapshotQueue`, `ResidentTools`.

### Task 6: prepare/complete DTOs
- Create `data/model/LocalTurn.kt`: `@Serializable PrepareRequest(prompt, operator)`, `PrepareResponse(turn_id="", system_prompt="", tools: List<ToolSchema> = emptyList(), provenance: JsonObject = JsonObject(emptyMap()), budget: TurnBudget = TurnBudget())`, `TurnBudget(package_chars=0, cap_chars=16000)`, `CompleteRequest(turn_id, operator, prompt, final_response, tool_transcript: List<ToolCallRecord>)`, `ToolCallRecord(name, args: JsonObject, result: String)`, `CompleteResponse(snap_id="", checkpoint_triggered=false)`. **All fields defaulted** (decode-safe, per the [[feedback-ondevice-device-test-method]] lesson). Test: round-trip serialize/deserialize.

### Task 7: `TurnClient` (prepare/complete over `BlackBoxApi`)
- Create `data/local/TurnClient.kt`: `suspend fun prepare(prompt, operator): PrepareResponse?` (POST `/local/turn/prepare`) and `suspend fun complete(req): CompleteResponse?` (POST `/local/turn/complete`). Catch `IOException` → return `null` (signals OFFLINE to the caller — the degraded-mode trigger). Test with MockWebServer: 200 decodes; non-2xx/timeout → null.

### Task 8: Register injected tools as directly-callable native tools
- In `ChatViewModel` (the native-loop builder, near `buildCloudNativeTools`): add `buildInjectedNativeTools(tools: List<ToolSchema>, operator): List<NativeTool>` — each wraps `ToolBridge.execute(name, args, operator)` (direct call, NOT find→run). Keep `find_blackbox_tool` in the set as the long-tail fallback; DROP the requirement to call `find` first. Phone-tool/intent actuators stay resident as today. Unit-test: given 2 injected schemas, returns 2 NativeTools whose names match and whose execute routes to the bridge (fake bridge asserts name+args).

### Task 9: Per-turn budget — result trimming + soft-stop
- Create pure helpers `data/local/TurnBudget.kt`: `fun trimToolResult(result: String, maxChars: Int): String` (truncate + marker) and `class TokenLedger(capTokens: Int)` with `fun wouldExceed(addChars: Int): Boolean` (chars/4 heuristic). Wire into the native loop: trim each tool result before re-feed; before dispatching the next tool call, if `wouldExceed`, stop the loop cleanly and let the model emit a final answer (soft-stop, NOT an error). Keep `MAX_NATIVE_TOOL_CALLS=24`. Unit-test both helpers + a loop test asserting soft-stop fires instead of throwing.

### Task 10: Turn driver — prepare → loop → complete
- In `ChatViewModel`, replace the local-turn entry so a turn: (1) calls `TurnClient.prepare`; (2) if non-null, runs `generateWithToolsNative` using the returned `system_prompt` + (injected tools + resident phone tools + `find_blackbox_tool` fallback + `search_snapshots`); (3) on completion, calls `TurnClient.complete` with the collected `{prompt, final_response, tool_transcript}`. The model's `system_prompt` comes from the server now (NOT the local persona cache) when online. Unit-test the orchestration with fakes (prepare returns a package → loop runs → complete called with the transcript).

### Task 11: Offline degraded mode + queued mint
- If `TurnClient.prepare` returns `null` (offline): fall back to the existing local persona-cache prompt + resident phone tools only (no fresh memory/injected cloud tools), run the loop, and enqueue the turn into the shipped `LocalSnapshotQueue` to mint via `/local/turn/complete` when connectivity returns (flush on app-open / next online turn). Unit-test: prepare=null → degraded path taken, queue receives the turn; queue flush calls complete.

---

## Phase 3 — Integration, tuning, device validation

### Task 12: Budget tuning + config
- Using the measured `package_chars` from Task 5 and on-device timing, finalize `PROVIDER_CAPS["local"]`, `semantic_k` (2 vs 3), and the soft-stop cap token count. Expose as config where sensible. Re-run all backend + Android unit gates.

### Task 13: Device validation (watched session, Brandon)
Checklist on the Fold 6 against the staging origin (`tailscale serve --bg http://127.0.0.1:<worktree-port>` per [[feedback-staging-box-as-production]]):
- [ ] Topic-shift teleport: ask about something from an old snapshot; confirm the relevant memory surfaces (pre-assembled, no manual search).
- [ ] Direct tool call (`roll_dice`) fires without the find→run dance.
- [ ] Long-tail tool reachable via `find_blackbox_tool` fallback.
- [ ] A turn with several tool calls stays under budget (soft-stop, no `[on-device error]`).
- [ ] Turn mints a snapshot (verify `snap_id` + 3072-dim embedding in journalctl) → immediately findable.
- [ ] Airplane mode → degraded turn works; reconnect → queued mint flushes.

### Task 14: Final review + ship
- superpowers:code-reviewer over the whole branch (whole-effort + a security pass on the new endpoints: per-operator scoping, no cross-operator leakage in prepare/complete).
- Ship per [[feedback-staging-box-as-production]]: restore prod routing, ff `main` → branch, deploy live tree + restart, `git push`. (Brandon's ship gate.)

---

## Risks / watch-items
- **Route order:** `/local/turn/*` must register before any generic catch-all (the `/models/local` shadowing lesson).
- **Don't duplicate mint logic:** factor `/chat/save`'s persist+mint into a shared helper; call from both.
- **`system_prompt` source flips online vs offline** (server vs persona cache) — keep the seam explicit so offline never silently sends a stale server prompt.
- **Budget is empirical** — Task 5 measures real `package_chars`; Task 12 tunes. Don't hardcode until measured.
- **Decode-safety:** every phone DTO field defaulted (a missing field must not fault a turn).
