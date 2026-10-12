# Android Terminal Mic — Live Partials + No Cap Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the CLI-agent terminal microphone show transcription live as words are spoken, and stop force-quitting the session after 60 seconds — bringing it to parity with the working Portal-web mic.

**Architecture:** Both bugs live entirely in one file, `WhisperMicButton.kt` (the CLI-agent terminal mic). (1) Remove the self-imposed 60s recording cap so the session runs until the user taps stop (the transport already supports indefinite sessions: `readTimeout(0)`, `pingInterval(30s)`, no `callTimeout`). (2) Render the `stt_delta` interim events — which the client already receives but currently discards — into a floating preview chip anchored above the mic button (a Compose `Popup`, so it floats over the terminal without an editable buffer and without disturbing layout). The final transcript still pastes into the PTY exactly as today; the chip is purely additive visibility.

**Tech Stack:** Kotlin, Jetpack Compose (Material3, `Popup`), OkHttp WebSocket, the shared `SttStreamClient` over the backend `/ws/stt` endpoint. JUnit4 for the one pure-logic unit test; on-device validation for UI + mic behavior.

---

## Context the executing engineer needs

**The shared `/ws/stt` wire contract (do not change the server):**
- Client → server (JSON text frames): `{"type":"stt_start", target, provider:"", lang:"en", sample_rate:24000}` once, then repeated `{"type":"stt_audio","pcm":"<base64 PCM16>"}`, then `{"type":"stt_stop"}`.
- Server → client: `{"type":"stt_delta","text":"<CUMULATIVE interim so far>"}` (partial, cumulative — replace, don't append), `{"type":"stt_final","text":"<final>"}` (emitted only after `stt_stop`), `{"type":"stt_error","message":...}`.
- The backend configures OpenAI realtime with `turn_detection: None` (manual commit), so during a live session there are **zero intermediate `stt_final`s** — only `stt_delta`s stream, and exactly one `stt_final` arrives on `stt_stop`. This is why removing the cap is safe: nothing flips the button to Idle mid-dictation.

**Why this is Android-native-only (the "3 surfaces" rule is satisfied):** The Portal-web mic (`Portal/modules/stt-stream.js`) already renders live partials (`applyDelta` rewrites the input on each `stt_delta`) and has no cap — it is the reference. The WebView wrapper runs that same web code. The native chat composer (`NativeMainActivity.kt:255-282`) already applies deltas live. Only the native terminal mic (`WhisperMicButton.kt`) is broken. No web/WebView/chat-composer changes are needed.

**Provider-agnostic — keep it that way (DO NOT hardcode a provider):** Despite the `Whisper` name, this mic is NOT Whisper-specific. `SttStreamClient.start(provider = "", ...)` is called with NO arguments by `WhisperMicButton.beginStreaming()`, so the `stt_start` frame carries `provider: ""`, and the backend's `resolve_stt_provider("")` selects whatever STT provider the box/onboarding-wizard is configured for (OpenAI realtime, ElevenLabs Scribe, or Google `chirp_2`). `SAMPLE_RATE = 24000` is fixed precisely because it satisfies every provider. The cap-removal and chip changes are provider-neutral (the chip renders `stt_delta`, which is emitted identically by all providers). **Constraint for every task below:** never pass or hardcode a provider name in the terminal-mic path — leave `provider` empty so the system-configured client is always the one used in the CLI.

**Branch/worktree:** Per the project's "staging box run as production" workflow, build off `main` (no test branch, no worktree). Commit frequently as below.

**Build environment:** The Android project root is:
`AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/`
All `./gradlew` commands below run from that directory. The space + parens in the path mean you MUST quote it.

---

## Task 1: Remove the 60-second recording cap (Bug #2)

**Files:**
- Modify: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/cli_agent/WhisperMicButton.kt`

This is a deletion-only task — it removes the timer that force-stops the mic. The `timerJob` and `scope` exist *only* to drive that cap, so they are removed too. There is no automated test for this (it's removing a timer that fires on wall-clock); the compile check is the gate, and the behavior is confirmed in Task 4 on-device.

**Step 1: Delete the cap constants.**

Remove these two lines (currently lines 74-75):

```kotlin
private const val MAX_RECORDING_MS = 60_000L
private const val WARNING_AT_MS = 50_000L
```

**Step 2: Delete the `timerJob` state and the `startCapTimer()` function.**

Remove the `timerJob` declaration (currently line 97):

```kotlin
    // Tracks the auto-stop / warning coroutine so cancel can interrupt it.
    var timerJob by remember { mutableStateOf<Job?>(null) }
```

Remove the entire `startCapTimer()` function (currently lines 104-119):

```kotlin
    // Start the 60s cap + 50s warning timer. Caller has already set Recording.
    fun startCapTimer() {
        timerJob?.cancel()
        timerJob = scope.launch {
            delay(WARNING_AT_MS)
            if (state == MicState.Recording) {
                Toast.makeText(context, "Recording limit approaching", Toast.LENGTH_SHORT).show()
            }
            delay(MAX_RECORDING_MS - WARNING_AT_MS)
            if (state == MicState.Recording) {
                // Hard cap: stop. The trailing Final still flushes via events.
                state = MicState.Transcribing
                sttClient.stop()
            }
        }
    }
```

**Step 3: Remove the `startCapTimer()` call from `beginStreaming()`.**

Replace the current `beginStreaming()` (lines 121-126):

```kotlin
    fun beginStreaming() {
        cancelRequested = false
        state = MicState.Recording
        sttClient.start()
        startCapTimer()
    }
```

with (also resets the interim text used in Task 3 — safe to add now):

```kotlin
    fun beginStreaming() {
        cancelRequested = false
        interimText = ""
        state = MicState.Recording
        sttClient.start()
    }
```

> NOTE: `interimText` is declared in Task 3. If you are executing strictly task-by-task and Task 3 is not yet applied, temporarily omit the `interimText = ""` line here and re-add it in Task 3. Recommended: apply Task 3's state declaration first, or accept a one-line follow-up.

**Step 4: Remove the now-dead `timerJob` references in the event collector, tap, long-press, and dispose handlers.**

In the `SttEvent.Final` branch, remove:
```kotlin
                    timerJob?.cancel()
                    timerJob = null
```
In the `SttEvent.Error` branch, remove:
```kotlin
                    timerJob?.cancel()
                    timerJob = null
```
In the `onTap` → `MicState.Recording` branch, remove:
```kotlin
                                timerJob?.cancel()
                                timerJob = null
```
In `onLongPress`, remove:
```kotlin
                            timerJob?.cancel()
                            timerJob = null
```
In the `DisposableEffect(sttClient) { onDispose { ... } }`, change:
```kotlin
        onDispose {
            timerJob?.cancel()
            sttClient.stop()
        }
```
to:
```kotlin
        onDispose {
            sttClient.stop()
        }
```

**Step 5: Remove the now-unused `scope` and clean imports.**

Remove (currently line 85):
```kotlin
    val scope = rememberCoroutineScope()
```
Remove these imports (no longer referenced after this task):
```kotlin
import androidx.compose.runtime.rememberCoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
```
(Keep `import android.widget.Toast` — it is still used by the permission-denied, error, and cancel toasts.)

**Step 6: Verify it compiles.**

Run (from the Android project root, quoted):
```bash
cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:compileDebugKotlin
```
Expected: `BUILD SUCCESSFUL`. If it fails with "unresolved reference: timerJob/scope/delay/launch/Job", you missed a reference above — grep the file for that symbol and remove the straggler.

**Step 7: Commit.**

```bash
git add "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/cli_agent/WhisperMicButton.kt"
git commit -m "fix(android): remove 60s cap on terminal STT mic (web parity)"
```

---

## Task 2: Add + test the `previewTail` helper (Bug #1, pure logic, TDD)

A live transcript grows long; the chip should always show the most recent words (the *tail*), not the start. `previewTail` is a pure function so it gets a real red-green unit test before any UI code.

**Files:**
- Modify: `.../ui/cli_agent/WhisperMicButton.kt` (add a top-level `internal fun`)
- Create: `AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/test/java/com/aiblackbox/portal/ui/cli_agent/PreviewTailTest.kt`

**Step 1: Write the failing test.**

Create `app/src/test/java/com/aiblackbox/portal/ui/cli_agent/PreviewTailTest.kt`:

```kotlin
package com.aiblackbox.portal.ui.cli_agent

import org.junit.Assert.assertEquals
import org.junit.Test

class PreviewTailTest {

    @Test
    fun short_text_is_returned_unchanged() {
        assertEquals("hello world", previewTail("hello world", max = 160))
    }

    @Test
    fun text_at_exactly_max_is_unchanged() {
        val text = "x".repeat(50)
        assertEquals(text, previewTail(text, max = 50))
    }

    @Test
    fun long_text_keeps_the_tail_with_a_leading_ellipsis() {
        // 21-char tail we want to keep visible
        val tail = "the most recent words"
        val text = "a".repeat(100) + tail
        assertEquals("…$tail", previewTail(text, max = tail.length))
    }
}
```

**Step 2: Run it and watch it fail.**

```bash
cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:testDebugUnitTest --tests "com.aiblackbox.portal.ui.cli_agent.PreviewTailTest"
```
Expected: FAIL — `unresolved reference: previewTail`.

**Step 3: Write the minimal implementation.**

In `WhisperMicButton.kt`, add this top-level function (place it just below the `MicState` enum, near the top of the file — it must be top-level, not nested, so the unit test can call it):

```kotlin
/**
 * Returns the most-recent [max] characters of [text] (prefixed with an ellipsis
 * when truncated) so the live preview chip always shows the latest words as a
 * cumulative transcript grows.
 */
internal fun previewTail(text: String, max: Int = 160): String =
    if (text.length > max) "…" + text.takeLast(max) else text
```

**Step 4: Run the test and watch it pass.**

```bash
./gradlew :app:testDebugUnitTest --tests "com.aiblackbox.portal.ui.cli_agent.PreviewTailTest"
```
Expected: `BUILD SUCCESSFUL`, 3 tests passed.

**Step 5: Commit.**

```bash
git add "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/cli_agent/WhisperMicButton.kt" \
        "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/test/java/com/aiblackbox/portal/ui/cli_agent/PreviewTailTest.kt"
git commit -m "feat(android): add previewTail helper for live STT chip (tested)"
```

---

## Task 3: Render live partials in a floating preview chip (Bug #1, UI)

**Files:**
- Modify: `.../ui/cli_agent/WhisperMicButton.kt`

**Step 1: Add the new imports** (at the top with the other imports):

```kotlin
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntRect
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.LayoutDirection
import androidx.compose.ui.window.Popup
import androidx.compose.ui.window.PopupPositionProvider
import androidx.compose.ui.window.PopupProperties
```

**Step 2: Add the interim-text state.** Just after the existing `var cancelRequested by remember { mutableStateOf(false) }` line, add:

```kotlin
    // Latest CUMULATIVE interim transcript from stt_delta; shown live in the chip.
    var interimText by remember { mutableStateOf("") }
```

**Step 3: Surface the deltas and clear on terminal events.** In the `LaunchedEffect(sttClient)` event collector:

Replace the `SttEvent.Delta` branch:
```kotlin
                is SttEvent.Delta -> {
                    // Cumulative interim — IGNORED. PTY paste is one-shot on Final.
                }
```
with:
```kotlin
                is SttEvent.Delta -> {
                    // Cumulative interim — show it live in the preview chip.
                    interimText = event.text
                }
```

In the `SttEvent.Final` branch, set `interimText = ""` right before `state = MicState.Idle`:
```kotlin
                is SttEvent.Final -> {
                    if (cancelRequested) {
                        cancelRequested = false
                    } else if (event.text.isNotBlank()) {
                        currentOnTranscript(event.text)
                    }
                    interimText = ""
                    state = MicState.Idle
                }
```

In the `SttEvent.Error` branch, set `interimText = ""` before `state = MicState.Idle`:
```kotlin
                is SttEvent.Error -> {
                    cancelRequested = false
                    Toast.makeText(context, event.message, Toast.LENGTH_SHORT).show()
                    interimText = ""
                    state = MicState.Idle
                }
```

In `onLongPress` (the cancel path), add `interimText = ""` before `state = MicState.Idle`:
```kotlin
                    onLongPress = {
                        if (state == MicState.Recording) {
                            cancelRequested = true
                            sttClient.stop()
                            interimText = ""
                            state = MicState.Idle
                            Toast.makeText(context, "Recording cancelled", Toast.LENGTH_SHORT).show()
                        }
                    },
```

> If Task 1 left a `interimText = ""` line in `beginStreaming()` that referenced an undeclared symbol, it now resolves. Confirm `beginStreaming()` reads exactly: `cancelRequested = false; interimText = ""; state = MicState.Recording; sttClient.start()`.

**Step 4: Build the chip position provider** inside the composable body, just before the root `Box(...)`:

```kotlin
    // Anchors the preview chip centered above the mic button (Popup floats over
    // the terminal — no editable buffer needed, no layout disturbance).
    val density = LocalDensity.current
    val gapPx = with(density) { 6.dp.roundToPx() }
    val chipPositionProvider = remember(gapPx) {
        object : PopupPositionProvider {
            override fun calculatePosition(
                anchorBounds: IntRect,
                windowSize: IntSize,
                layoutDirection: LayoutDirection,
                popupContentSize: IntSize,
            ): IntOffset {
                val x = anchorBounds.left + (anchorBounds.width - popupContentSize.width) / 2
                val y = anchorBounds.top - popupContentSize.height - gapPx
                val maxX = (windowSize.width - popupContentSize.width).coerceAtLeast(0)
                return IntOffset(x.coerceIn(0, maxX), y.coerceAtLeast(0))
            }
        }
    }
```

**Step 5: Render the Popup inside the button Box.** At the very start of the root `Box { ... }` content lambda (just before the existing `when { ... }` that draws the icon), add:

```kotlin
        // Live transcription preview — floats above the mic while recording /
        // finalizing. The Popup occupies no layout space in the button Box.
        if ((isRecording || isTranscribing) && interimText.isNotBlank()) {
            Popup(
                popupPositionProvider = chipPositionProvider,
                properties = PopupProperties(focusable = false),
            ) {
                TranscriptPreviewChip(text = interimText)
            }
        }
```

**Step 6: Add the chip composable** at file scope (private), e.g. directly below the `WhisperMicButton` function:

```kotlin
@Composable
private fun TranscriptPreviewChip(text: String) {
    Surface(
        shape = RoundedCornerShape(10.dp),
        color = MaterialTheme.colorScheme.surface,
        contentColor = MaterialTheme.colorScheme.onSurface,
        tonalElevation = 3.dp,
        shadowElevation = 6.dp,
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
        modifier = Modifier.widthIn(max = 280.dp),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
        ) {
            MicIcon(
                modifier = Modifier.size(14.dp),
                color = MaterialTheme.colorScheme.error,
            )
            Spacer(Modifier.width(8.dp))
            Text(
                text = previewTail(text),
                style = MaterialTheme.typography.bodySmall,
                maxLines = 3,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }
}
```

**Step 6b: Correct the stale, Whisper-flavored header comment.** The file header (currently lines 3-15) calls this "Whisper transcription" and states "interim deltas are IGNORED" — both are now wrong (it's provider-agnostic, and deltas are rendered). Replace the header block with:

(Keep the component name `WhisperMicButton` in this header for now — Task 4 renames it to `CliMicButton` along with the function.)
```kotlin
// WhisperMicButton — tap-to-record state machine for the
// CLI-agent terminal. It streams live speech-to-text and pastes the FINAL
// transcript into the active terminal session as one bracketed paste.
//
// State machine: idle 🎤 → recording 🔴 → transcribing ⏳ → idle 🎤
// Long-press during recording cancels (discards transcript).
//
// Transport: SttStreamClient — the unified, PROVIDER-AGNOSTIC live-transcription
// client over the backend's /ws/stt WebSocket (PCM16 @24kHz). It sends
// provider:"" so the backend uses whatever STT provider the box is configured
// for (OpenAI realtime / ElevenLabs Scribe / Google) — this is NOT Whisper-locked.
// This shares ONE transcription path with the chat composer's live dictation.
//
// The terminal PTY has no editable buffer, so the cumulative interim `stt_delta`s
// are shown live in a floating preview chip above the mic, and the FINAL
// transcript is pasted via onTranscript: (String) -> Unit (the caller wraps it in
// a {"type":"paste","text":transcript} text frame).
```

**Step 6c: Fix the stale `cancelRequested` comment** (carried over from Task 1's cap removal). Find the comment above `var cancelRequested by remember { mutableStateOf(false) }`:
```kotlin
    // Set true by long-press / cap-during-cancel: the next Final is discarded.
```
and replace it with (the cap no longer exists; long-press is the only writer):
```kotlin
    // Set true by long-press cancel: the next Final is discarded (not pasted).
```

**Step 6d: Pin the empty-string contract for `previewTail`.** The chip calls `previewTail(interimText)`, and `interimText` is `""` before the first `stt_delta`. Add this one case to `app/src/test/java/com/aiblackbox/portal/ui/cli_agent/PreviewTailTest.kt` (inside the `PreviewTailTest` class):
```kotlin
    @Test
    fun empty_text_returns_empty() {
        assertEquals("", previewTail("", max = 160))
    }
```

**Step 7: Verify it compiles and the unit test passes.**

```bash
cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal"
./gradlew :app:compileDebugKotlin
./gradlew :app:testDebugUnitTest --tests "com.aiblackbox.portal.ui.cli_agent.PreviewTailTest" --rerun-tasks
```
Expected: `BUILD SUCCESSFUL`, 4 tests pass. Common compile failures: a missing import from Step 1, or `interimText` referenced before its declaration (Step 2 must precede the event collector in source order — it does, since the collector is below the state vars).

**Step 8: Commit.** Stage only our two files (working tree has unrelated changes):

```bash
git add "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/main/java/com/aiblackbox/portal/ui/cli_agent/WhisperMicButton.kt" \
        "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal/app/src/test/java/com/aiblackbox/portal/ui/cli_agent/PreviewTailTest.kt"
git status --short   # confirm ONLY these two files are staged
git commit -m "feat(android): live STT preview chip on terminal mic (renders stt_delta)"
```

---

## Task 4: Rename WhisperMicButton → CliMicButton (provider-neutral)

The mic is provider-agnostic; the name shouldn't say "Whisper." This is a mechanical rename. The class lives in package `com.aiblackbox.portal.ui.cli_agent`, and so do both call sites — so NO import changes are needed, only the call name. (Other files reference `WhisperMicButton` in comments only; update those for cleanliness, but they're non-functional.)

**Files:**
- Rename: `.../ui/cli_agent/WhisperMicButton.kt` → `.../ui/cli_agent/CliMicButton.kt`
- Modify (call sites): `.../ui/cli_agent/TerminalScreen.kt` (~line 607), `.../ui/cli_agent/ZellijTerminalScreen.kt` (~line 627)

**Step 1: Rename the file (preserve git history).**

```bash
cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal"
git mv app/src/main/java/com/aiblackbox/portal/ui/cli_agent/WhisperMicButton.kt \
       app/src/main/java/com/aiblackbox/portal/ui/cli_agent/CliMicButton.kt
```

**Step 2: Rename the composable function.** In `CliMicButton.kt`, change the declaration:
```kotlin
fun WhisperMicButton(
```
to:
```kotlin
fun CliMicButton(
```
(The header comment was already updated to `CliMicButton` in Task 3 Step 6b; confirm it reads `CliMicButton`. `TranscriptPreviewChip` and `previewTail` keep their neutral names.)

**Step 3: Update the two call sites.** In both `TerminalScreen.kt` (~line 607) and `ZellijTerminalScreen.kt` (~line 627), change the call `WhisperMicButton(` → `CliMicButton(`. The argument list is unchanged.

**Step 4: Find and tidy remaining references.**

```bash
grep -rn "WhisperMicButton" app/src/main --include=*.kt
```
Expected after Steps 2-3: only **comment** mentions remain (e.g. `ExtraKeysBar.kt`, `NativeMainActivity.kt:181`, `VoiceLabScreen.kt:234`, and the two terminal-screen header comments). There must be ZERO remaining `WhisperMicButton(` *calls*. Update the comment mentions to `CliMicButton` for cleanliness.

**Step 5: Verify it compiles.**

```bash
./gradlew :app:compileDebugKotlin
```
Expected: `BUILD SUCCESSFUL`. A failure here means a call site was missed (Step 3/4).

**Step 6: Commit.** The working tree has UNRELATED pre-existing changes — stage only our files, never `git add -A`.

```bash
git add app/src/main/java/com/aiblackbox/portal/ui/cli_agent/ \
        app/src/main/java/com/aiblackbox/portal/NativeMainActivity.kt \
        app/src/main/java/com/aiblackbox/portal/ui/voicelab/VoiceLabScreen.kt
git status --short   # confirm ONLY cli_agent/*.kt + the two comment files are staged
git commit -m "refactor(android): rename WhisperMicButton → CliMicButton (provider-agnostic name)"
```
(The `git mv` in Step 1 already stages the rename; re-adding the directory picks up the in-place edits. Verify with `git status --short` that no landing-page/embeddings files are staged.)

---

## Task 5: On-device validation + snapshot

Compose UI + live-mic behavior cannot be meaningfully unit-tested (it depends on real `AudioRecord` capture and a live `/ws/stt` session), so this is validated on the device, matching the project's Fold-validation workflow.

**Step 1: Install the debug build on the device.**

```bash
cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal" && ./gradlew :app:installDebug
```
Expected: `BUILD SUCCESSFUL` and the app updates on the connected device (`adb devices` shows it).

**Step 2: Validate the three behaviors in a CLI-agent terminal screen.**

Open a terminal (the standard `TerminalScreen` or the `ZellijTerminalScreen`) so the `WhisperMicButton` is visible, then:

1. **Live partials:** Tap the mic and speak a sentence. CONFIRM a preview chip appears above the mic and its text grows/updates *as you speak* — before you stop.
1b. **Long-dictation tail (tune `PREVIEW_CHIP_MAX_CHARS`):** Speak a long, continuous run (3+ lines' worth) without pausing. CONFIRM the chip keeps the **newest** words visible (the bottom/most-recent text), not stale earlier words. If the newest words get clipped by the 3-line limit, lower the `private const val PREVIEW_CHIP_MAX_CHARS` in `CliMicButton.kt` until the retained tail reliably fits 3 lines at the 280.dp chip width (the constant feeds the chip's `previewTail(text, max = …)` call). This is the one number the code-review flagged for device tuning.
2. **No premature kick:** Keep talking (or hold the session) past 60 seconds. CONFIRM there is NO "Recording limit approaching" toast at ~50s and the mic does NOT auto-stop at ~60s — it stays recording until you tap stop.
3. **Final still pastes:** Tap stop. CONFIRM the chip disappears and the final transcript is pasted into the terminal prompt as one bracketed paste (unchanged from before).
4. **Cancel path:** Start again, speak, then long-press to cancel. CONFIRM the chip disappears immediately and nothing is pasted.
5. **Provider-agnostic:** While recording, confirm the configured provider (not a hardcoded "whisper") is what's used. On the device: `adb logcat -s SttStreamClient` should show `Sent stt_start (provider='', ...)` (empty provider). On the server: `journalctl -u blackbox.service -f | grep '\[STT/WS\] start'` should print `provider=<the box's configured provider>`. If the box is reconfigured to a different STT provider, the CLI mic must follow it with no code change.

**Step 3: Confirm the other surfaces are unaffected (sanity, no code changed there).** The Portal-web mic and native chat-composer mic were not modified; a quick check that web live-dictation still works is sufficient. No action if unchanged.

**Step 4: Mint a development snapshot** of the completed work via `/snapshot-dev` (operator resolved dynamically per the `resolve-operator` procedure). Record: both bugs, the one-file fix, the floating-chip decision, cap removal for web parity, and device-validation result.

---

## Definition of done

- [ ] 60s cap fully removed; mic runs until user stop (Task 1).
- [ ] `previewTail` unit test green (Task 2).
- [ ] Live preview chip shows growing `stt_delta` text above the mic; clears on final/error/cancel (Task 3).
- [ ] Provider stays agnostic: `stt_start` carries `provider: ""`; no provider name hardcoded anywhere in the mic path; stale "Whisper"/"deltas ignored" comments corrected (Tasks 3–4).
- [ ] `WhisperMicButton` renamed to `CliMicButton` (file + function + both call sites); no `WhisperMicButton(` calls remain (Task 4).
- [ ] `./gradlew :app:compileDebugKotlin` and `:app:testDebugUnitTest` both pass.
- [ ] On-device: live chip ✔, no kick past 60s ✔, final still pastes ✔, cancel discards ✔, configured-provider used (not hardcoded whisper) ✔ (Task 5).
- [ ] Dev snapshot minted.
