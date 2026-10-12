# Updates-Panel Cleanup + Android Surfacing — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (or executing-plans) to implement task-by-task.

**Goal:** Make the two Updates panels (Portal web + Android) *status + action + progress* only — move all "underlying-system selection" (reranker picker, embedding compute/placement) into the onboarding/manage wizard, which is already the single management home — and make Android's Updates discoverable via a top-bar badge, with a smooth in-app WebView handoff to the wizard.

**Architecture:** Both updates panels already deep-link "full management" to the wizard (`/onboarding/?step=embeddings`). This plan removes the *selectors* those panels duplicate, keeps a minimal read-only reranker **status** line (a reranker can silently fail preflight and be OFF while the sidecar says ON — invisible elsewhere), adds a real progress **bar** to the running re-embed/migrate job, surfaces Android Updates with a top-bar badge, and upgrades Android's wizard handoff from an external-browser `ACTION_VIEW` to an in-app WebView (reusing `PortalActivity`'s WebView infra) that refreshes native state on return.

**Tech Stack:** vanilla JS (`Portal/modules/updates-manager.js`), Kotlin/Compose (Android `ui/updates/*`, `ui/settings`, `navigation`, `NativeMainActivity`), source-contract Python tests + `node --check` + `./gradlew :app:testDebugUnitTest --offline`.

**Context:** design rationale + current-state map from the 2026-07-07 investigation workflow. The onboarding wizard (`Portal/onboarding/steps/embeddings.js`) is confirmed the full **superset** management surface (model select→migrate, placement, reranker, re-embed, keep_alive, Ollama pull), re-enterable at `/onboarding/?step=embeddings` (revisit; never mutates onboarding state). Reranker selection is pure `rerank.json` sidecar state (no vector store) — removing the picker from panels is behaviorally inert.

**Branch:** work on `main` (build-off-main convention); commit per task.

---

## Locked decisions (2026-07-07)
- **D1** — Android Updates entry = **top-bar badge icon** on the floating `BlackBoxTopBar`, badged from `UpdatesViewModel` state (updates-available OR embedding attention), taps → `Routes.UPDATES`.
- **D2** — Reranker **selection** moves to the wizard only; **keep a read-only status line** ("Reranking: ON — <model> · active / preflight-failed / OFF") on both panels' embeddings card, reusing the existing `[Manage]` deep-link. No picker, no off-switch, no key entry in the panels.
- **D3** — Portal updates **compute/placement card removed entirely** (hardware line + Auto/GPU/CPU toggle both → wizard, which already has both).
- **D4** — Android → wizard handoff upgraded to an **in-app WebView** destination (reuse `PortalActivity` WebView infra) for `/onboarding/?step=…` & `?mode=manage`; on return, refresh native embeddings/rerank status. Replaces external `ACTION_VIEW`.
- **Cross-cutting** — add a visual **progress bar** to the embeddings running-job in both panels (data `done`/`total` already present).

---

## Adversarial-review fixes folded in (2026-07-07, workflow `wmrw87w6q` — GO-WITH-FIXES). REQUIRED — do not "optimize away."

**B1 (Task 2.1, Android compile):** removing the reranker also requires deleting the `UpdatesScreen.kt` sites that consume the removed VM flows, or it won't compile: the `RerankCard(...)` invocation block (~245–275), the `rerankBusy`/`rerankError` `collectAsState` (~107–108), and the `rerankError` `LaunchedEffect` + `clearRerankError()` (~136–142). KEEP the `rerankStatus` collect/`let` (repurposed by 2.2's status line).

**B2 (Task 2.1):** UNCONDITIONALLY delete `app/src/test/java/com/aiblackbox/portal/RerankStatusTest.kt` — it references `RerankModel/rerankActionFor/tierModels/RerankSelectRequest` (deleted) and stripped fields, so leaving it fails `compileDebugKotlin`/`testDebugUnitTest`.

**B3 (Task 1.2, same commit):** remove `'/embeddings/placement'`, `'recommended_placement'`, `'hardware'` from `REQUIRED_LITERALS` in `test_portal_embeddings_card_parity.py` (keep `/embeddings/status`, `/embeddings/migrate`, `/onboarding/?step=embeddings`, `successor_slug`). Negative asserts must target UNIQUE tokens (`_computeCardHtml`, `embeddings-rerank-btn`, `/embeddings/placement`) — NEVER the bare word `hardware` (survives in prose → false-fail). Scrub the module-header comment (~lines 32–37).

**B4 (Task 1.1 + 2.2 — headline correctness):** render the reranker status line **UNCONDITIONALLY**, gated only on `rr != null`, NOT nested inside the health-conditional embeddings card (which renders nothing on a healthy box — exactly when a silent reranker failure needs surfacing). Portal: append `_rerankStatusLineHtml(rr)` at the old line-401 site (where `_rerankCardHtml` was), fix the `if (!html) _hideEmbeddingsCard()` guard so a present `rr` keeps the card visible, and bind `btnEmbeddingsManage` whether or not a notification card exists. Android: emit a standalone `RerankStatusLine(rr)` at the old `rerankStatus?.let{}` call site (~245), not inside `EmbeddingsCard`.

**B5 (Task 3.0 — NEW prerequisite, shared by 3.1/4.1/M3):** `UpdatesViewModel` is `NavBackStackEntry`-scoped to `Routes.UPDATES` — the activity top bar can't read it and it isn't alive on the Chat home. Hoist ONE activity-owned `UpdatesViewModel` in `NativeMainActivity.setContent`, call `initialize(origin)` there, expose `attention: StateFlow<Boolean>`, collect it into the top bar, and pass the SAME instance into `composable(Routes.UPDATES){ UpdatesScreen(viewModel = sharedVm) }`. The badge (3.1), the WebView return-refresh (4.1), and the badge poll (M3) all use this one instance.

**M1 (Task 2.1↔2.2 contradiction):** Task 2.1 removes ONLY selector pieces (`RerankCard/RerankOptionRow/RerankLink/rerankKeyLabel`, `selectRerank`, `RerankSelectRequest`, `RerankModel`, `RerankAction`, `RerankPreflight`, `rerankActionFor`, `tierModels`, `_rerankBusy/_rerankError`) and **KEEPS the read path** (`getRerankStatus`, `refreshRerank`, `_rerankStatus`, and a trimmed `RerankStatus{enabled, available, provider, model}` — drop `modelCatalog`/`preflight`). 2.2 only adds the read-only line.

**M2 (Task 4.1):** the WebView close-handler calls `refreshStatus()/refreshRerank()` on the **activity-scoped VM from B5** (a separate `Routes.WIZARD` destination can't reach the screen VM, and returning to a retained UPDATES entry does NOT re-fire `initialize`).

**M3 (Task 3.1):** the badge needs a genuinely NEW low-frequency activity-scope fetch (there is no existing app-foreground update/embedding poll — `checkHealth()` only GETs `/health`). Drive it from a foreground `LaunchedEffect` next to `checkHealth()`; don't claim it reuses existing polling.

**M4 (Task 4.1):** build the wizard URL from the Compose-world `origin` (`normalizeApiOrigin` base) as `$origin/onboarding/?step=<arg>`; reuse ONLY PortalActivity's `WebSettings` (`javaScriptEnabled`, `domStorageEnabled`, `mixedContentMode=COMPATIBILITY`) — NOT its `normalizeOrigin()`/`/ui/` URL logic (that would 404).

**M5 (Task 4.2):** update `test_android_embeddings_card_parity.py` in the same commit — add `WizardWebViewScreen.kt` to `KOTLIN_FILES` + keep a `/onboarding/?` literal there, or swap the required literal to the new route contract; the contiguous `/onboarding/?step=embeddings` string leaves the four scanned files.

**M6 (gate wording, all Android tasks):** the `.parity` tests are pytest source-text tests (`Orchestrator/venv/bin/pytest`), NOT gradle; after B2 there is no feature JVM unit test. True gates: Portal = `node --check` + `pytest test_portal_embeddings_card_parity.py`; Android compile = `./gradlew :app:compileDebugKotlin`; Android contract = `pytest test_android_embeddings_card_parity.py`; behavior = M5 device-validation. Drop "unit tests green" as the Android gate.

**Minors:** m1 — scope D4 to plain-key reranker providers; Google-SA/OAuth `_blank` links spawn firefox ON THE BOX (pre-existing), not the phone — document it. m2 — Task 1.3: change the two `<p class="embeddings-card-progress">` wrappers (~368, 377) to `<div>` (a `<div>` bar inside `<p>` auto-closes the `<p>`). m3 — Task 3.1: `BlackBoxTopBar` gains additive `showUpdatesBadge: Boolean=false` + `onUpdatesClick: ()->Unit={}` params; add `Badge`/`BadgedBox` (+`LinearProgressIndicator`) imports. m4 — Task 2.2: keep only `enabled/available/provider/model` on `RerankStatus`; `ignoreUnknownKeys` makes the trim safe. m5 — soften the not-`available` copy to a generic "not in use" (covers both preflight-failed and not-configured).

---

## Ground-truth (verified — don't re-derive)
- Portal panel: `Portal/modules/updates-manager.js` renders into `#embeddingsCard` (index.html:647) inside `#menuModal`; sub-cards built in `_renderEmbeddingsCard`: [notification] + `_computeCardHtml(status)` + `_rerankCardHtml()`. Reranker wiring: `_rerankCardHtml`/`_onRerankSelect`, `/rerank/status` ride-along fetch (`_fetchJsonSoft`, ~line 333), `.embeddings-rerank-btn`/`.embeddings-rerank-off` handlers (~425-427). Placement: `_computeCardHtml`/`_onPlacementClick` (`POST /embeddings/placement`). Keep: git self-update state machine + `_embedProgressLine`/`_startEmbedPoll` (5s poll) + `btnEmbeddingsManage` → `/onboarding/?step=embeddings`.
- Android panel: `ui/updates/UpdatesScreen.kt` — `RerankCard` (~602-670), `RerankOptionRow` (~672-755), `RerankLink` (~757-770), `rerankKeyLabel` (~772-783); imports `RerankAction/RerankModel/RerankStatus/rerankActionFor/tierModels` (~66-72). ViewModel `ui/updates/UpdatesViewModel.kt`: `_rerankStatus/_rerankBusy/_rerankError`, `refreshRerank()` (called from `refreshStatus()` ~line 102), `selectRerank()`, `clearRerankError()`. Repo `data/repository/UpdateRepository.kt`: `getRerankStatus()/selectRerank()` + `RerankSelectRequest`. Model `data/model/RerankStatus.kt` (+ `rerankActionFor`, unit-tested). `EmbeddingsCard` (~474-574): progress is a **monospace text line** `embeddingsProgressLine(job)` — NO bar; `EmbeddingsJob` carries `done`+`total` (`EmbeddingsStatus.kt` 37-42). `EmbeddingsManageButton` (~576-583) → `ACTION_VIEW $origin/onboarding/?step=embeddings`.
- Android nav: NO bottom nav / drawer. `Routes.UPDATES="updates"` (`navigation/NavGraph.kt`); reached only via `SettingsSheet` (Tools `CollapsibleSection`, `initiallyExpanded=false`, Updates = last item ~line 271). Floating `BlackBoxTopBar` in `NativeMainActivity.kt` (~566, hamburger `onMenuClick`). `SettingsSheet` "Manage Setup" (~964-975) → `ACTION_VIEW $origin/onboarding/?mode=manage`. Full-Portal WebView exists in `PortalActivity.kt` (`web.loadUrl` ~487) — reuse its config for the scoped onboarding WebView.
- Wizard reach: `/onboarding/?step=embeddings` (revisit — `onboarding.js` REVISIT_STEP, never mutates onboarding state) is the canonical selection home. `?mode=manage` renders the hub.
- Tests: no JS runtime infra → source-contract tests (`Orchestrator/tests/test_portal_embeddings_card_parity.py`, `test_android_embeddings_card_parity.py`) that read the source and assert required literals; `node --check`; Android `./gradlew :app:testDebugUnitTest --offline`.

---

# MILESTONE 1 — Portal updates panel: strip selectors, add reranker-status line + progress bar

### Task 1.1: Remove the reranker selector card (keep a read-only status line)
**Files:** Modify `Portal/modules/updates-manager.js`; Test `Orchestrator/tests/test_portal_embeddings_card_parity.py`.
- Remove `_rerankCardHtml`, `_onRerankSelect`, `_rerankBusy`, the `.embeddings-rerank-btn`/`.embeddings-rerank-off` wiring, and the `_rerankCardHtml()` call in `_renderEmbeddingsCard`.
- **Keep** the `/rerank/status` fetch (still needed for the status line) but rename its use: build a NEW `_rerankStatusLineHtml(rr)` that renders read-only text only — `Reranking: ON — <provider>/<model>` when `rr.enabled && rr.available`; `ON — preflight failed (not in use)` when `rr.enabled && !rr.available`; `OFF` when `!rr.enabled`; hides entirely when `rr` is null (fail-soft). No buttons. Fold it into the embeddings notification card body (above the existing `[Manage]`/`[Update]` row) so it reuses `btnEmbeddingsManage` → `/onboarding/?step=embeddings`.
- **Test:** update the parity test — assert the picker literals are GONE (`embeddings-rerank-btn`, `Use this reranker`, `Turn reranking off`, `/rerank/select`) and the status literals PRESENT (`Reranking:`, `/rerank/status`, `/onboarding/?step=embeddings`). Run `node --check Portal/modules/updates-manager.js`.
- **Commit** `refactor(portal-updates): reranker selector → wizard; keep read-only reranker status line`.

### Task 1.2: Remove the compute/placement card entirely (D3)
**Files:** Modify `updates-manager.js`; Test parity test.
- Remove `_computeCardHtml`, `_onPlacementClick`, the `.embeddings-placement-*` wiring, and its call in `_renderEmbeddingsCard`. (`POST /embeddings/placement` is no longer called from this module; the wizard owns placement.)
- **Test:** assert `/embeddings/placement` and the placement-toggle literals are GONE from the module; `node --check`.
- **Commit** `refactor(portal-updates): remove embedding compute/placement card (moves to wizard)`.

### Task 1.3: Add a visual progress bar to the embeddings running-job
**Files:** Modify `updates-manager.js` (+ a minimal CSS class if needed, reusing existing `embeddings-*` styles).
- Where the job is running (`_embedProgressLine`), add a determinate bar element (`<div class="embeddings-progress-track"><div class="embeddings-progress-fill" style="width:${pct}%"></div></div>`), `pct = total>0 ? min(100, floor(done/total*100)) : 0`. Keep the text line ("Re-embedding N/total…") beneath it. Reuse the wizard's `ob-emb-progress-*` visual language via local `embeddings-*` classes (mirror, don't share — matches the file's existing pattern).
- **Test:** parity test asserts `embeddings-progress-fill` present; `node --check`.
- **Commit** `feat(portal-updates): visual progress bar for the embeddings re-embed/migrate job`.

### Task 1.4: Portal panel green gate
Run the parity test + `node --check`; confirm the git self-update state machine is untouched. **Milestone gate** before Android.

---

# MILESTONE 2 — Android updates panel: strip reranker, add status line + progress bar

### Task 2.1: Remove the reranker SELECTOR (keep the read path for 2.2's status line)
**Files:** `ui/updates/UpdatesScreen.kt`, `ui/updates/UpdatesViewModel.kt`, `data/repository/UpdateRepository.kt`, `data/model/RerankStatus.kt`, delete `app/src/test/java/com/aiblackbox/portal/RerankStatusTest.kt`; Test `Orchestrator/tests/test_android_embeddings_card_parity.py`.

Remove ONLY the selector pieces; KEEP the `/rerank/status` read (2.2 needs it). *(B1, B2, M1, m4.)*
- `UpdatesScreen.kt`: delete `RerankCard`, `RerankOptionRow`, `RerankLink`, `rerankKeyLabel` **and their consuming sites** — the `RerankCard(...)` invocation block (~245–275), the `rerankBusy`/`rerankError` `collectAsState` (~107–108), and the `rerankError` `LaunchedEffect` + `clearRerankError()` (~136–142). Drop now-dead imports `RerankAction/RerankModel/rerankActionFor/tierModels/RerankSelectRequest`. **KEEP** the `rerankStatus` `collectAsState` + the `rerankStatus?.let{}` call site (2.2 replaces the body with `RerankStatusLine(rr)`).
- `UpdatesViewModel.kt`: remove `selectRerank()`, `clearRerankError()`, `_rerankBusy`, `_rerankError`. **KEEP** `_rerankStatus`, `refreshRerank()`, and its call in `refreshStatus()`.
- `UpdateRepository.kt`: remove `selectRerank()` + `RerankSelectRequest`. **KEEP** `getRerankStatus()`.
- `data/model/RerankStatus.kt`: trim to `RerankStatus{enabled, available, provider, model}` (drop `modelCatalog`, `preflight`, `gpu`, `serviceReachable`, `tier`, etc.); delete `RerankModel`, `RerankPreflight`, `RerankAction`, `rerankActionFor`, `tierModels`, `RerankSelectRequest` from the file. `ignoreUnknownKeys=true` (UpdateRepository.kt:35) makes the trimmed decode safe.
- **Delete** `RerankStatusTest.kt` unconditionally (it references the removed symbols/fields → won't compile otherwise).
- **Test:** update `test_android_embeddings_card_parity.py` to assert the picker symbols are GONE (`RerankCard`, `selectRerank`, `/rerank/select`) — target unique tokens.
- **Gate (M6):** `./gradlew :app:compileDebugKotlin` (compiles) + `Orchestrator/venv/bin/pytest Orchestrator/tests/test_android_embeddings_card_parity.py -q`. (NOT `testDebugUnitTest` — the only JVM test was just deleted.)
- **Commit** `refactor(android-updates): remove reranker selector; keep read path for status line`.

### Task 2.2: Read-only reranker status line on the EmbeddingsCard (D2)
**Files:** `UpdatesScreen.kt` (EmbeddingsCard), `UpdatesViewModel.kt` (keep a lightweight `/rerank/status` read), `data/model/RerankStatus.kt` (keep just the fields the status line needs — `enabled`, `provider`, `model`, `available`).
- Add a non-interactive `Text` line inside `EmbeddingsCard`: "Reranking: ON — <provider>/<model>" / "ON — preflight failed" / "OFF", null-hides. Reuse the existing `EmbeddingsManageButton` for "change it in the wizard."
- Keep a minimal `/rerank/status` fetch in the ViewModel for this line (a read, not the picker).
- **Test:** parity test asserts the status `Text` present, no `selectRerank`; unit tests green.
- **Commit** `feat(android-updates): read-only reranker status line (surfaces silent preflight failures)`.

### Task 2.3: LinearProgressIndicator for the embeddings job (the loading bar)
**Files:** `UpdatesScreen.kt` (EmbeddingsCard broken + jobRunning branches).
- Add `LinearProgressIndicator(progress = { if (total>0) done.toFloat()/total else 0f })` beside the existing `embeddingsProgressLine` text, in the `jobRunning` branch (and the `broken` branch when auto-migration is running). `EmbeddingsJob.done/total` already exist. Preserve the 5s poll (`job?.state=="running"`).
- **Test:** parity test asserts `LinearProgressIndicator` in EmbeddingsCard; unit tests green.
- **Commit** `feat(android-updates): visual progress bar for the embeddings job`.

---

# MILESTONE 3 — Android discoverability: top-bar update badge (D1)

### Task 3.0: Hoist `UpdatesViewModel` to activity scope (prerequisite — B5/M2/M3)
**Files:** `NativeMainActivity.kt` (`setContent`), `navigation/NavGraph.kt`, `ui/updates/UpdatesViewModel.kt`.
- The VM is currently `NavBackStackEntry`-scoped (`UpdatesScreen.kt:97 viewModel()`), so the top bar can't read it and it's not alive on the Chat home. In `NativeMainActivity.setContent` create ONE activity-owned `val updatesVm: UpdatesViewModel = viewModel()`, call `updatesVm.initialize(origin)` there.
- Add to `UpdatesViewModel` an `attention: StateFlow<Boolean>` derived from its state (updates-available OR embedding health broken/superseded OR a running job).
- Pass the SAME instance into `composable(Routes.UPDATES){ UpdatesScreen(viewModel = updatesVm, origin = origin) }` (change `UpdatesScreen`'s `viewModel` default to a required/passed param) so screen + badge share one source of truth.
- **Gate:** `./gradlew :app:compileDebugKotlin`.
- **Commit** `refactor(android): hoist UpdatesViewModel to activity scope (shared by badge + wizard-return)`.

### Task 3.1: Surface Updates via a badged top-bar icon (D1)
**Files:** `NativeMainActivity.kt` (line ~560 BlackBoxTopBar call site), `ui/.../TopBar.kt` (`BlackBoxTopBar` signature), `ui/updates/UpdatesViewModel.kt`.
- *(m3)* Extend `BlackBoxTopBar` with additive params `showUpdatesBadge: Boolean = false`, `onUpdatesClick: () -> Unit = {}`; add `Badge`/`BadgedBox` imports. Render an `IconButton` (system-update icon) beside the hamburger; wrap it in `BadgedBox` showing a dot when `showUpdatesBadge`.
- At the call site: `showUpdatesBadge = updatesVm.attention.collectAsState().value`, `onUpdatesClick = { navController.navigate(Routes.UPDATES) }` (navController is in scope at NativeMainActivity:149).
- *(M3)* Drive the badge with a NEW low-frequency activity-scope fetch — a foreground `LaunchedEffect` (next to `chatViewModel.checkHealth()`) calling `updatesVm.refreshStatus(forceFresh=false)`. There is NO existing app-foreground update/embedding poll to reuse.
- Keep the SettingsSheet → Tools → Updates entry (harmless redundancy).
- **Gate (M6):** `./gradlew :app:compileDebugKotlin`; **device-validate on the Fold** in M5.
- **Commit** `feat(android): surface Updates via a badged top-bar icon`.

---

# MILESTONE 4 — Android in-app WebView wizard handoff (D4)

### Task 4.1: In-app WebView destination for the onboarding/manage wizard
**Files:** new `ui/webview/WizardWebViewScreen.kt` (reuse `PortalActivity.kt`'s WebView config — same-origin, JS enabled, cookie/session sharing so the Tailscale origin round-trips), `navigation/NavGraph.kt` (new `Routes.WIZARD` taking a `?step=`/`?mode=` arg), `NativeMainActivity.kt` (host it), Composer/X-close handling.
- The screen loads `$origin/onboarding/?step=<arg>` (or `?mode=manage`) in an in-app `WebView`. Provide a close/back affordance that returns to the previous native screen.
- **Return-refresh:** on close, trigger `UpdatesViewModel.refreshStatus()` (+ the embeddings/rerank status reads) so a model/reranker change made in the WebView is reflected natively.
- **Test:** it builds + a source test asserts the WebView route exists; **device-validate** the round-trip in M5.
- **Commit** `feat(android): in-app WebView wizard destination (replaces external-browser handoff)`.

### Task 4.2: Route all wizard hand-offs through the in-app WebView
**Files:** `UpdatesScreen.kt` (`EmbeddingsManageButton`), `ui/settings/SettingsSheet.kt` ("Manage Setup" + any `?step=` deep-links).
- Replace the `Intent(ACTION_VIEW, …/onboarding/?step=embeddings)` calls with `navController.navigate(Routes.WIZARD + "?step=embeddings")` (and `?mode=manage` for "Manage Setup"). Remove now-dead reranker `?step=api_keys`/`?step=optional_integrations` external intents (the reranker card is gone; key entry happens inside the wizard WebView).
- **Test:** source test asserts no remaining `ACTION_VIEW …/onboarding` for the manage/step paths; unit tests green.
- **Commit** `refactor(android): manage/step hand-offs use the in-app WebView`.

---

# MILESTONE 5 — Validation + snapshot

### Task 5.1: Portal — Chrome-verify
Open the Portal ☰ → Updates: confirm no reranker picker / no compute card, the read-only "Reranking: …" line shows, the embeddings job renders a progress **bar**, `[Manage]` still lands on `/onboarding/?step=embeddings`, and the wizard still has the full reranker + placement selectors. Confirm the git self-update panel is unchanged.

### Task 5.2: Android — build + Fold device-validate
`./gradlew :app:testDebugUnitTest --offline` green, then install on the Fold. Verify: the **top-bar Updates badge** appears/badges correctly and opens Updates; the Updates screen has no reranker picker, shows the read-only reranker status line + a **progress bar** during a re-embed; **[Manage] opens the wizard IN-APP (WebView)**, a change there round-trips back to refreshed native state. Confirm Galaxy XR parity if applicable.

### Task 5.3: Snapshot
`/snapshot-dev` (operator resolved) documenting the cleanup across both surfaces + the locked decisions. Commit/push per Brandon's flow after device acceptance.

---

## Risks
- **Dead-code removal completeness (Android):** removing `RerankCard` orphans imports, ViewModel flows, repo methods, and possibly `RerankStatus.kt` + its unit test — the compiler + `testDebugUnitTest` catch leftovers; grep for every symbol before deleting.
- **WebView session/origin:** the in-app WebView must reach the Tailscale/localhost origin with the app's network + cookies (why WebView over Chrome Custom Tab, per the investigation) — reuse `PortalActivity`'s proven WebView config; device-test the round-trip.
- **Mirrored (not shared) markup:** the wizard and updates panels intentionally duplicate reranker/placement markup with different CSS systems — this plan only *deletes* from the panels; do NOT touch the wizard copies.
- **Status-line honesty:** a reranker with `enabled=true` but failed preflight must read "preflight failed (not in use)" — key the line on `available` (configured AND preflight-ok), not just `enabled`.

## Execution Handoff
After (optional) an adversarial verification pass, execute via **superpowers:subagent-driven-development** (fresh subagent per task + two-stage review), Portal first (M1) then Android (M2→M4), then validation.
