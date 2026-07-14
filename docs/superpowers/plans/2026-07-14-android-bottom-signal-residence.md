# Android Bottom Signal Residence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the live Signal a permanent bottom residence above Android navigation, lift the composer controls above it, and keep streamed prose focused above the composer.

**Architecture:** Add a shared Compose geometry contract derived from measured bottom-stack bounds. The activity shell owns the main-chat Signal residence and composer placement; chat screens consume its focal target, while the CLI surface applies the same residence locally because it hides the global composer.

**Tech Stack:** Kotlin, Jetpack Compose, Compose UI tests, JUnit, Android Gradle Plugin.

## Global Constraints

- Never uninstall the app from the user's phone; device delivery uses one in-place `adb install -r` only after verification.
- The Signal residence is the lowest app-owned row, directly above Android system navigation.
- The residence remains reserved when its label is absent; the composer stack must not jump.
- Message words and controls never render behind the Signal residence; particles may remain visible.
- Manual scrolling pauses follow immediately, shows the existing down arrow, and resumes after five uninterrupted seconds or an arrow tap.
- Main provider chat and Claude Code/Gemini CLI-agent chat receive equivalent behavior.

---

### Task 1: Shared bottom focal-zone geometry

**Files:**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/chat/LiveStreamFollow.kt`
- Test: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/test/java/com/aiblackbox/portal/ui/chat/LiveStreamFollowPolicyTest.kt`

**Interfaces:**
- Produces: pure bottom-zone geometry calculation plus Compose geometry consumed by shell and chat screens.

- [ ] **Step 1: Write failing tests** proving the residence is below controls, the live target is above the composer by the breathing gap, and unmeasured bounds have a safe fallback.
- [ ] **Step 2: Run** `./gradlew testDebugUnitTest --tests '*LiveStreamFollowPolicyTest'` and confirm failure caused by missing geometry.
- [ ] **Step 3: Implement** the minimal immutable geometry model and pure calculation using measured window coordinates and density-independent fallbacks, without changing the five-second policy.
- [ ] **Step 4: Re-run the focused test** and confirm it passes.
- [ ] **Step 5: Commit** with `test: define bottom signal focal geometry`.

### Task 2: Main shell residence and composer placement

**Files:**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/NativeMainActivity.kt`
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/navigation/NavGraph.kt`
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/chat/ChatScreen.kt`
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/chat/LiveStreamFollow.kt`
- Test: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/androidTest/java/com/aiblackbox/portal/ui/chat/LiveStreamFocalFollowTest.kt`

**Interfaces:**
- Consumes: geometry contract from Task 1.
- Produces: stable main residence, measured composer clearance, and main-chat focal target.

- [ ] **Step 1: Add failing Compose assertions** for residence ordering, stable reserved height with no label, no control overlap, and live-edge placement above the composer.
- [ ] **Step 2: Run the focused connected test when a device is available**, otherwise compile androidTest and retain the expected RED evidence.
- [ ] **Step 3: Move main Signal rendering into the bottom shell**, reserve its row unconditionally on chat routes, measure the composer/control bounds, pass geometry through navigation, and remove the centered rail plus fixed `200.dp` padding.
- [ ] **Step 4: Preserve the return-to-live arrow** inside the chat viewport and its current state behavior.
- [ ] **Step 5: Run focused unit tests, androidTest compilation, and available Compose tests.**
- [ ] **Step 6: Commit** with `feat: add bottom signal residence to main chat`.

### Task 3: CLI-agent parity

**Files:**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/chat/AgentChatScreen.kt`
- Test: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/androidTest/java/com/aiblackbox/portal/ui/chat/LiveStreamFocalFollowTest.kt`

**Interfaces:**
- Consumes: shared geometry and residence UI from Tasks 1–2.
- Produces: equivalent Claude Code and Gemini CLI layout behavior.

- [ ] **Step 1: Add failing tests** for both agents' residence placement, reserved empty height, and live targeting.
- [ ] **Step 2: Verify RED** with the focused connected test or androidTest compilation evidence.
- [ ] **Step 3: Add the local agent residence**, reserve it independently of label visibility, and replace fixed `180.dp` padding with measured clearance.
- [ ] **Step 4: Run focused tests,** `compileDebugAndroidTestKotlin`, and `assembleDebug`.
- [ ] **Step 5: Commit** with `feat: align agent streams with bottom signal residence`.

### Task 4: Final review and one-time delivery

**Files:**
- Verify only unless review identifies a defect.

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: reviewed APK and a single in-place device update.

- [ ] **Step 1: Run** `./gradlew testDebugUnitTest compileDebugAndroidTestKotlin assembleDebug` and retain fresh output.
- [ ] **Step 2: Review the complete diff** for spec compliance, state ownership, inset/keyboard handling, and regressions.
- [ ] **Step 3: Fix Critical or Important findings test-first** and repeat verification.
- [ ] **Step 4: If exactly one authorized phone is connected, run one** `adb install -r app/build/outputs/apk/debug/app-debug.apk`; never uninstall or automatically retry.
- [ ] **Step 5: Launch once and confirm process health**, then hand visual testing to the user.
