# On-Device Stateful-Lean Chat Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development OR straight execution to implement this plan task-by-task. Build on `main` (no worktree — [[feedback-staging-box-as-production]]). Android `ChatViewModel.kt` is **CRLF** — preserve line endings on every edit.

**Goal:** Invert the on-device (`local` Gemma E4B) chat strategy from "push a heavy server-assembled context every turn, stateless" to "**start lean, build context up conversationally**": no pushed BlackBox context, restored in-session statefulness (token-budgeted rolling history), memory pulled on demand via tools, all inside the GPU-survivable 6144-token window.

**Architecture:** The snapshot-ledger's per-turn "teleport" push ([[project-snapshot-ledger]]) is retired for `local`. `/local/turn/prepare` returns **persona + injected tools only** (no fossil). The phone carries a **token/char-budgeted rolling conversation history** (oldest dropped first) so the model is stateful within the session but the prompt never *starts* heavy. `toolvault`/`search_snapshots` stay injected so the ~6000-snapshot ledger is discoverable on demand. `/local/turn/complete` still mints each turn (ledger = long-term memory). A warm-loop guard ensures an OOM warm can never make the phone unusable again.

**Why:** Device-proven 2026-06-19 — the E4B GPU warm ceiling on the Fold 6 is **6144** (8192 + 16384 both OOM the warm: pinned, unpageable GPU KV buffers). At 6144 the agent scaffolding leaves no room for an 18981-char pushed package (it overran: "Input token ids are too long: 8231 >= 6144"). "Starting with a lot is really difficult; we can build up context" — Brandon.

**Tech Stack:** Kotlin/Android (`ChatViewModel`, `FcLoop`, `TurnBudget`, `LiteRtEngine`, `LocalModelService`, `LocalEngineHolder`), Python/FastAPI backend (`context_builder`, `local_routes`), litert-lm 0.13.1.

**Already landed this session (foundation, do NOT redo):**
- `LiteRtEngine.DEFAULT_MAX_TOKENS = 6144` (+ hard cap `coerceAtMost`).
- M3 double-warm serialization: `LocalEngineHolder.warmingPath` + `beginWarming/endWarming`; `LocalModelService` marks/clears around its load; `ChatViewModel.localProviderOrWire` waits for the service warm (SERVICE_WARM_* constants) instead of building a 2nd concurrent engine.
- `LocalModelService` START_STICKY null-redelivery restarts LISTENER-ONLY (crash-loop fix).

---

### Task 1: Backend — lean `prepare` (drop the pushed context)

**Files:**
- Modify: `Orchestrator/config.ini` (add `[context]` knobs)
- Reference: `Orchestrator/routes/local_routes.py:285-294` (prepare assembly), `Orchestrator/context_builder.py:80` (`build_fossil_context`)
- Test: `Orchestrator/tests/test_local_turn_prepare.py`

**Step 1: Write the failing test** — assert that for `provider='local'`, `/local/turn/prepare` returns an EMPTY fossil/context block (persona + tools only), given `local_semantic_k=0` + `local_checkpoint_count=0`.

```python
def test_prepare_local_is_lean_no_fossil(monkeypatch, ...):
    # config local_semantic_k=0, local_checkpoint_count=0
    resp = client.post("/local/turn/prepare", json={"prompt": "hi", "operator": "Brandon"})
    body = resp.json()
    assert body["fossil_context"] in ("", None)      # no pushed snapshots/checkpoint
    assert body["tools"]                              # tools still injected
    assert "persona" in body or body.get("system_prompt")  # persona still present
```

**Step 2: Run it to confirm it fails** (current fallbacks 3/1 push a fossil).

**Step 3: Set the config** — add to `config.ini`:
```ini
[context]
local_semantic_k = 0
local_checkpoint_count = 0
local_injected_tools_k = 5
```
`build_fossil_context(semantic_k=0, checkpoint_count=0, include_recent=False, include_keyword=False)` → empty fossil. Tools (k=5) unchanged.

**Step 4: Run the test — PASS.** Also re-run `test_local_turn_*` + `test_context_builder_local` (cloud callers must stay byte-identical — the 0 values are LOCAL-only via the `local_*` keys).

**Step 5: Commit** (`fix(local): lean prepare — drop pushed fossil context for on-device E4B`).

---

### Task 2: Android — token/char-budgeted rolling history (restore statefulness)

**Files:**
- Modify: `app/.../ui/chat/ChatViewModel.kt` (`LOCAL_HISTORY_WINDOW_TURNS` const @164; history selection @~875)
- Reference/Reuse: `app/.../data/local/TurnBudget.kt` (existing char-trim helper)
- Test: `app/src/test/.../ui/chat/ChatViewModelLocalEngineTest.kt` (or a new pure `LocalHistoryBudgetTest.kt`)

**Step 1: Write the failing test** for a PURE budget function `budgetHistory(turns: List<FcLoop.Turn>, maxChars: Int): List<FcLoop.Turn>`:
- keeps the NEWEST turns, drops OLDEST first, until total chars ≤ `maxChars`
- never splits a turn; returns `[]` if even the newest exceeds budget
- a single huge (tool-result) turn doesn't blow the budget (it's dropped/kept atomically)

```kotlin
@Test fun budget_keeps_newest_drops_oldest_under_cap() { ... }
@Test fun budget_single_oversize_turn_excluded() { ... }
@Test fun budget_empty_or_within_cap_passthrough() { ... }
```

**Step 2: Run → fails** (function doesn't exist).

**Step 3: Implement** `budgetHistory` (pure, companion or top-level) — sum chars from newest backward, include while under cap. Add `LOCAL_HISTORY_BUDGET_CHARS` constant (start ~8000 chars ≈ ~2K tokens — leaves persona+tools+current-msg+output+intra-turn-growth headroom under 6144; device-tunable). Replace `LOCAL_HISTORY_WINDOW_TURNS = 0` usage at @875:
```kotlin
val history = budgetHistory(toFcHistory(_messages.value), LOCAL_HISTORY_BUDGET_CHARS)
```
Keep `LOCAL_HISTORY_WINDOW_TURNS` as a secondary COUNT cap if desired (hybrid), or remove. **Preserve CRLF.**

**Step 4: Run tests — PASS.**

**Step 5: Commit** (`feat(local): token-budgeted rolling history (restore in-session statefulness)`).

---

### Task 3: Android — warm-loop guard (OOM can never make the phone unusable)

**Files:**
- Modify: `app/.../ui/chat/ChatViewModel.kt` (`preloadLocalEngine` @1438) and/or `LocalModelService`
- New: a tiny persisted flag (SharedPreferences) for "warm in-flight"
- Test: pure decision function `shouldAttemptWarm(prevWarmCrashed: Boolean, consecutiveFails: Int): Boolean`

**Step 1: Write the failing test** — `shouldAttemptWarm` returns false when the previous warm started but never completed (process died mid-warm) until a manual retry / cooldown; true on a clean prior state.

**Step 2: Run → fails.**

**Step 3: Implement** — persist `warm_inflight=true` (+ timestamp) BEFORE a warm starts; clear on success/graceful-failure. On launch, if `warm_inflight` is still set (== a prior process died mid-warm), DON'T auto-warm — surface a "tap to load on-device model" CTA / require an explicit trigger, and reset the flag. This converts the activity-restore→warm→OOM→restore LOOP into a single failed attempt + a manual retry. (At 6144 the warm should succeed, so this is defense-in-depth for memory-pressured edge cases.)

**Step 4: Run tests — PASS.**

**Step 5: Commit** (`fix(local): warm-loop guard — never auto-retry a crashed warm into an OOM loop`).

---

### Task 4: Steering — system-prompt nudge for the lean/stateful posture

**Files:**
- Modify: the on-device persona/nativeAddendum source (persona cache / `/local/system-prompt` — confirm site via `grep -rn "nativeAddendum\|system-prompt\|persona" Orchestrator app/src/main`)
- Test: assert the addendum contains the steering line

**Step 1–4 (TDD):** Add a line steering the model: *"You retain this conversation's recent turns. Relevant long-term memory is NOT pre-loaded — call `search_snapshots`/`toolvault` to recall older or deeper context when needed."* (Replaces the old snapshot-ledger nudge that assumed context was pre-assembled.) Verify via test.

**Step 5: Commit** (`feat(local): steer on-device model to pull memory via tools (lean posture)`).

---

### Task 5: On-device settings store + read-at-warm (make the window/sampler user-tunable)

**Files:**
- Modify: `app/.../data/local/LocalModelManager.kt` (`ModelConfig` already has `max_tokens` + sampler fields), `LiteRtEngine.kt` (hard cap), the warm sites (`LocalModelService`, `ChatViewModel.localProviderOrWire`, `EngineWarm`)
- New (if needed): a small global prefs store for `auto_warm`
- Test: cap-resolution + read-at-warm unit tests

**Key change:** the warm path ALREADY reads `cfg.maxTokens ?: DEFAULT_MAX_TOKENS` and the sampler trio — so a user setting written to the per-model `ModelConfig` flows through automatically. BUT `buildAndInitialize` currently `coerceAtMost(DEFAULT_MAX_TOKENS=6144)` — change the HARD CAP to a **soft default + sanity max**: honor the user's explicit value (DEFAULT 6144 only when unset), clamped only to the model's native maximum (not 6144). The UI shows the OOM warning above the recommended 6144; the user owns the risk. **Steps (TDD):** test that an explicit `max_tokens` (e.g. 8192) is honored (not clamped to 6144), and that unset → 6144; implement; commit.

### Task 6: Re-warm on settings change

**Files:** `ChatViewModel` (`invalidateLocalEngine` + `preloadLocalEngine` exist), settings save path.

Changing window/sampler/model must invalidate + re-warm the engine (close + reload at the new config) — litert `maxNumTokens` is fixed at `initialize()`, so a live change needs a re-init. Reuse `invalidateLocalEngine()` then `preloadLocalEngine()`. Show "reloading on-device model…". **TDD:** test invalidate→re-warm fires on a config change; commit.

### Task 7: Settings screen UI (Compose) — Android-only (the on-device model is Android-only; not a Portal/WebView surface)

**Files:** new `app/.../ui/settings/LocalModelSettingsScreen.kt` (+ a ViewModel or reuse), nav entry.

Controls (wired to Task 5 store + Task 6 re-warm + Task 2 history):
- **Context-window slider** — range (e.g. 1024–16384), **recommended marker at 6144**, live label, and a warning band above 6144: *"may run out of memory on GPU."*
- **Sampler** — temperature / topK / topP inputs (defaults from `LiteRtEngine.DEFAULT_SAMPLER_*`).
- **Auto-warm on app open** — toggle (off = load on demand).
- **Clear conversation** — button → clears the operator's in-session rolling history (Task 2) + the persisted `ChatHistoryStore`.
- **Status readout** (Task 8).

**TDD:** the slider/clamp/label logic + clear-conversation action are unit-testable (pure mappers); the Composable is assembled from them. Commit.

### Task 8: Status readout (non-editable)

**Files:** the settings screen + a small state source.

Show: model **loaded?** (from `_localEngineState`: IDLE/WARMING/READY/ERROR), **window in use** (the effective maxTokens of the warm engine), **free-RAM headroom** (`ActivityManager.getMemoryInfo().availMem`). Helps the user see *why* a big window might be risky. **TDD:** the formatting/mapping is pure-testable; commit.

### Task 9: Device validation (Brandon, watched) — GATES the push

Validate on the real Fold 6 (Android 16), reinstall the 6144 APK + restart backend:
- [ ] **Lean start:** open app, switch to Gemma-4 (local) → warm reaches READY, no "model could not finish", **no loop** (pid stable, memory steady). Confirm `engine load: ... maxNumTokens=6144` + NO `Input token ids are too long`.
- [ ] **Statefulness:** multi-turn conversation — the model remembers earlier turns within the session (e.g. "my name is X" → later "what's my name?").
- [ ] **Lean prompt fits:** logcat shows the per-turn input well under 6144 even after several turns (budget drops oldest).
- [ ] **Memory on demand:** ask about old work → model calls `search_snapshots`/`toolvault` and uses the result.
- [ ] **No regression:** control_phone still wakes + executes (flashlight/maps/read_screen) on GPU.
- [ ] **Warm-loop guard:** force a memory-pressured warm failure (optional) → single failure + CTA, NOT a loop.
- [ ] **Settings — window slider:** change it → re-warm at the new window; `engine load: maxNumTokens=<new>` in logcat; >6144 shows the OOM warning.
- [ ] **Settings — sampler:** change temp/topK/topP → applied on the next turn.
- [ ] **Settings — clear conversation:** resets statefulness (model no longer recalls the earlier turn).
- [ ] **Settings — auto-warm toggle:** off → model loads on first send, not on open; on → loads on open.
- [ ] **Settings — status readout:** shows correct loaded-state, window, and free-RAM headroom.

**After PASS:** final whole-diff review, then `git push origin main` (device validation gates the push — [[feedback-staging-box-as-production]]).

---

## Open design notes / tunables
- **History budget value:** start 8000 chars; measure the lean package (persona+tools) on device and set the budget to fill the remaining 6144 headroom minus an output + intra-turn-tool-growth buffer. Tunable without rebuild if promoted to a `[context]`-style knob later.
- **Session boundary:** continuity = the operator's rolling budgeted history (no session-ID). An explicit "new conversation" clear is optional (future).
- **Intra-turn growth:** a big `read_screen`/tool result still grows the prompt WITHIN a turn (native loop appends results). `TurnBudget` soft-stop already guards overflow; keep the history budget conservative so a turn has room to grow.
- **control_phone:** unchanged — lean `remotePrompt` + filtered tools, rides the warm engine; no context, fits 6144 trivially.
