# Android Page-Fill Live Follow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let streamed thinking and answers fill the available page before following line-for-line, eliminate accumulated scroll lag, and place a reliable return arrow directly above the prompt.

**Architecture:** Extend the pure follow policy with explicit fill/follow/suspend/return states. Replace cancel-and-restart token animations with one frame-conflated correction loop, keep return as a separately verified smooth glide, and expose the measured prompt top as the arrow anchor through the existing bottom focal geometry.

**Tech Stack:** Kotlin, Kotlin coroutines, Jetpack Compose, JUnit, Compose UI tests, Android Gradle Plugin.

## Global Constraints

- Preserve the permanent bottom Signal residence and its IME/navigation-inset behavior.
- Do not change provider transports, message persistence, reasoning storage, or rendered response content.
- Thinking and answer text do not move before crossing the reading boundary.
- Once following, rapid deltas must not queue or restart a per-token animation.
- Manual input suspends immediately; five uninterrupted seconds or arrow tap starts return.
- The arrow remains visible until measured arrival at the current live edge.
- The arrow's bottom edge is directly adjacent to the measured prompt-window top.
- Apply equivalent behavior to main provider chat, Claude Code, and Gemini CLI.
- Never uninstall the phone app; final delivery permits exactly one in-place `adb install -r` after verification.

---

### Task 1: Explicit page-fill follow policy

**Files:**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/chat/LiveStreamFollow.kt`
- Test: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/test/java/com/aiblackbox/portal/ui/chat/LiveStreamFollowPolicyTest.kt`

**Interfaces:**
- Produces: `LiveFollowMode` with `FILLING`, `FOLLOWING`, `SUSPENDED`, and `RETURNING`; policy decisions for overflow, arrival, user input, idle expiry, and completion.

- [ ] **Step 1: Write failing unit tests** for no correction before positive overflow, first-overflow transition, thinking-to-answer continuity, immediate suspension, deadline reset, returning arrow visibility, measured arrival, interrupted return, and completion returnability.
- [ ] **Step 2: Run** `./gradlew testDebugUnitTest --tests '*LiveStreamFollowPolicyTest'` and confirm failures are caused by the missing modes/transitions.
- [ ] **Step 3: Implement the minimal state machine** in `LiveStreamFollowPolicy`, keeping time and geometry inputs explicit and independent from Compose.
- [ ] **Step 4: Re-run the focused test** and confirm all policy tests pass.
- [ ] **Step 5: Commit** with `feat(android): model page-fill follow states`.

### Task 2: Frame-conflated continuous following

**Files:**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/chat/LiveStreamFollow.kt`
- Test: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/androidTest/java/com/aiblackbox/portal/ui/chat/LiveStreamFocalFollowTest.kt`

**Interfaces:**
- Consumes: Task 1 policy transitions.
- Produces: one active correction loop that consumes the newest measured overflow and a separate measurement-verified return glide.

- [ ] **Step 1: Add failing Compose tests** showing short content causes zero list movement, first boundary crossing starts movement, high-frequency reasoning and answer bursts stay within a small live-edge tolerance, and thinking-to-answer handoff remains stable.
- [ ] **Step 2: Add a failing coordinator test** proving repeated deltas do not create a series of cancelled 180 ms animations or leave stale displacement after layout settles.
- [ ] **Step 3: Compile/run the focused Android test when available** and record RED evidence; if no device is used at this gate, compile androidTest and preserve the structural RED evidence from the new coordinator seam.
- [ ] **Step 4: Replace `correctionJob` restart behavior** with one conflated loop: suppress negative/before-boundary displacement in `FILLING`, transition on positive overflow, and use direct frame-aligned `scrollBy` while `FOLLOWING`.
- [ ] **Step 5: Implement return separately** as one quick smooth glide that remeasures until within arrival tolerance; keep the arrow visible during `RETURNING`, and use immediate correction under reduced motion.
- [ ] **Step 6: Run focused unit tests, Android-test compilation, and available Compose tests.**
- [ ] **Step 7: Commit** with `fix(android): keep live stream pinned after page fill`.

### Task 3: Prompt-edge arrow placement and reliable return

**Files:**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/chat/LiveStreamFollow.kt`
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/chat/ChatScreen.kt`
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/chat/AgentChatScreen.kt`
- Modify as needed: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/NativeMainActivity.kt`
- Test: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/androidTest/java/com/aiblackbox/portal/ui/chat/LiveStreamFocalFollowTest.kt`

**Interfaces:**
- Consumes: `BottomFocalGeometry.composerTopPx` and Task 2 return state.
- Produces: a prompt-anchored arrow shared by main and agent chat.

- [ ] **Step 1: Add failing shell-level assertions** that the arrow bottom equals the real prompt top within tolerance in normal, expanded-composer, attachment-preview, keyboard-open, and effective-inset changes.
- [ ] **Step 2: Add failing interaction assertions** that arrow tap and five-second idle keep the arrow visible during transit, reach the current live edge, then hide it; user input during return must cancel transit and show the arrow again.
- [ ] **Step 3: Verify RED** with the focused connected test or compile/test seam evidence.
- [ ] **Step 4: Replace broad padding placement** with an explicit measured prompt-top anchor and ensure the clickable region remains right-aligned outside the primary reading column.
- [ ] **Step 5: Wire main and CLI surfaces** to the same placement and return contract, including completed-stream return.
- [ ] **Step 6: Run focused tests, full JVM tests, `compileDebugAndroidTestKotlin`, and `assembleDebug`.**
- [ ] **Step 7: Commit** with `fix(android): anchor return control above prompt`.

### Task 4: Review, verification, and one-time device delivery

**Files:**
- Verify only unless review identifies a defect.

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: reviewed APK and one in-place phone update.

- [ ] **Step 1: Run fresh** `./gradlew testDebugUnitTest compileDebugAndroidTestKotlin assembleDebug` and `git diff --check`.
- [ ] **Step 2: Request an independent whole-diff review** against the approved design, focusing on no-before-fill movement, burst convergence, mode transitions, return arrival, prompt anchoring, and preserved bottom geometry.
- [ ] **Step 3: Fix all Critical or Important findings test-first** and repeat review and verification.
- [ ] **Step 4: If exactly one authorized phone is connected, perform exactly one** `adb install -r app/build/outputs/apk/debug/app-debug.apk`; do not uninstall or retry automatically.
- [ ] **Step 5: Launch once, verify process health, and hand live thinking/answer acceptance testing to the user.**

### Task 5: Completed-history scroll-to-bottom shortcut

**Files:**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/chat/LiveStreamFollow.kt`
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/chat/ChatScreen.kt`
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/chat/AgentChatScreen.kt`
- Test: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/test/java/com/aiblackbox/portal/ui/chat/LiveStreamFollowPolicyTest.kt`
- Test: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/androidTest/java/com/aiblackbox/portal/ui/chat/LiveStreamFocalFollowTest.kt`

**Interfaces:**
- Consumes: `LazyListState.canScrollForward`, completed-response destination anchor, prompt-edge arrow, and measurement-verified return.
- Produces: tap-only completed-history shortcut independent of active-stream state.

- [ ] **Step 1: Write failing policy tests** proving inactive completed history can show a return control, does not schedule five-second return, enters returning only on tap, and clears when the true bottom is reached.
- [ ] **Step 2: Write failing Compose tests** for main, Claude, and Gemini: finish a long response, scroll up one page, assert the prompt-anchored arrow appears, advance beyond five seconds with no movement, tap, verify measured arrival, and assert arrow plus completed anchor disappear. Also verify manually reaching bottom clears the arrow.
- [ ] **Step 3: Run focused tests/compilation** and retain RED evidence caused by inactive user scroll being ignored.
- [ ] **Step 4: Add an explicit completed-history mode/input** driven by `canScrollForward`, distinct from active-stream suspension. Keep its timer disabled and reuse the existing completed destination plus return glide on tap.
- [ ] **Step 5: Wire main and agent screens** so list position changes update idle shortcut visibility after completion without affecting live five-second behavior.
- [ ] **Step 6: Run focused tests, full JVM tests, `compileDebugAndroidTestKotlin`, `assembleDebug`, and `git diff --check`.**
- [ ] **Step 7: Request independent review**, fix all blocking findings test-first, then perform exactly one in-place install and one launch-health check.
