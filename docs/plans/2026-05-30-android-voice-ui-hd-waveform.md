# Android Voice Agent UI — HD Waveform + Collapsible Settings Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Upgrade the Android Voice Agent screen with a gorgeous HD flowing-ribbon waveform driven by real audio amplitude, a collapsible per-setting-dropdown settings pane that auto-collapses to a summary pill on connect, and a default Gemini Live model of `gemini-3.1-flash-live-preview`.

**Architecture:** Compute real **RMS amplitude** from PCM buffers that already pass through `VoiceViewModel` (mic read loop + playback drain), surface it as `amplitude`/`waveSpeaker` `StateFlow`s, and render a layered translucent gradient sine ribbon in a Compose `Canvas`. Convert the flat chip-row settings into Material3 dropdowns wrapped in an `AnimatedVisibility` glass card that auto-collapses on connect. Flip one constant for the model default.

**Tech Stack:** Kotlin, Jetpack Compose (Material3 `ExposedDropdownMenuBox`, `Canvas`, `animateFloatAsState`/`animateColorAsState`, `rememberInfiniteTransition`), Kotlin coroutines `StateFlow`, JUnit4 + kotlinx-coroutines-test.

**Design doc:** `docs/plans/2026-05-30-android-voice-ui-hd-waveform-design.md`

---

## Working directory & path conventions

All commands run from the **module root** (the directory containing `gradlew`):

```bash
cd "/home/ai-black-box-fc/Desktop/blackbox_poc./blackbox_poc/AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal"
```

Path shorthands used below (relative to module root):
- `SRC` = `app/src/main/java/com/aiblackbox/portal`
- `TEST` = `app/src/test/java/com/aiblackbox/portal`

> ⚠️ The repo path contains a space and parentheses. Always quote paths in `cd`, `git add`, and `./gradlew` invocations.

**Key commands:**
- Fast unit tests (JVM): `./gradlew test`
- Single test class: `./gradlew test --tests "com.aiblackbox.portal.<ClassName>"`
- Compile check (fast): `./gradlew compileDebugKotlin`
- Full debug build / APK: `./gradlew assembleDebug`

---

## Task 0: Baseline verification

**Step 1:** Confirm the project builds and existing tests pass before changing anything.

Run:
```bash
./gradlew test
```
Expected: BUILD SUCCESSFUL (existing Provenance/WebSocket/ChatViewModel tests pass).

**Step 2:** If baseline is red, STOP and report — do not build on a broken baseline.

---

## Task 1: Default Gemini Live model → 3.1 preview

**Files:**
- Modify: `SRC/util/Constants.kt` (`LIVE_MODEL_DEFAULTS` ~line 150; `MODEL_CONFIG["gemini-live"]` ~lines 123-127)
- Test: `TEST/util/ConstantsLiveDefaultsTest.kt` (create)

**Step 1: Write the failing test**

Create `TEST/util/ConstantsLiveDefaultsTest.kt`:
```kotlin
package com.aiblackbox.portal.util

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ConstantsLiveDefaultsTest {
    @Test fun `gemini live default is 3_1 preview`() {
        assertEquals(
            "gemini-3.1-flash-live-preview",
            Constants.LIVE_MODEL_DEFAULTS["gemini-live"]
        )
    }

    @Test fun `gemini live default is thinking capable`() {
        val default = Constants.LIVE_MODEL_DEFAULTS["gemini-live"]
        assertTrue(default in Constants.GEMINI_LIVE_THINKING_CAPABLE_MODELS)
    }

    @Test fun `gemini live default is present in model config list`() {
        val ids = Constants.MODEL_CONFIG["gemini-live"].orEmpty().map { it.first }
        assertTrue("gemini-3.1-flash-live-preview" in ids)
    }
}
```

**Step 2: Run test to verify it fails**

Run: `./gradlew test --tests "com.aiblackbox.portal.util.ConstantsLiveDefaultsTest"`
Expected: FAIL on `gemini live default is 3_1 preview` (currently `gemini-2.5-flash-native-audio-latest`).

**Step 3: Make the change**

In `SRC/util/Constants.kt`, change the `gemini-live` default:
```kotlin
    val LIVE_MODEL_DEFAULTS: Map<String, String> = mapOf(
        "realtime" to "gpt-realtime-2",
        "gemini-live" to "gemini-3.1-flash-live-preview",
    )
```

And reorder `MODEL_CONFIG["gemini-live"]` so the new default is first (the dropdown should surface it at top):
```kotlin
        "gemini-live" to listOf(
            "gemini-3.1-flash-live-preview" to "Gemini 3.1 Flash Live (Preview, thinkingLevel)",
            "gemini-2.5-flash-native-audio-latest" to "Gemini 2.5 Flash Live (Latest GA-track)",
            "gemini-2.5-flash-native-audio-preview-12-2025" to "Gemini 2.5 Flash Live (Dec 2025 pin)"
        ),
```

**Step 4: Run test to verify it passes**

Run: `./gradlew test --tests "com.aiblackbox.portal.util.ConstantsLiveDefaultsTest"`
Expected: PASS (3 tests).

**Step 5: Commit**

```bash
git add "app/src/main/java/com/aiblackbox/portal/util/Constants.kt" "app/src/test/java/com/aiblackbox/portal/util/ConstantsLiveDefaultsTest.kt"
git commit -m "feat(android-voice): default Gemini Live to gemini-3.1-flash-live-preview"
```

---

## Task 2: Pure RMS amplitude function (test seam)

**Files:**
- Create: `SRC/ui/voice/AudioAmplitude.kt`
- Test: `TEST/ui/voice/AudioAmplitudeTest.kt`

**Step 1: Write the failing test**

Create `TEST/ui/voice/AudioAmplitudeTest.kt`:
```kotlin
package com.aiblackbox.portal.ui.voice

import org.junit.Assert.assertEquals
import org.junit.Test

class AudioAmplitudeTest {
    @Test fun `silence is zero`() {
        assertEquals(0f, rmsAmplitude(ShortArray(256), 256), 0.0001f)
    }

    @Test fun `full scale is approximately one`() {
        val buf = ShortArray(256) { Short.MAX_VALUE }
        assertEquals(1f, rmsAmplitude(buf, 256), 0.001f)
    }

    @Test fun `half scale is approximately one half`() {
        val buf = ShortArray(256) { (Short.MAX_VALUE / 2).toShort() }
        assertEquals(0.5f, rmsAmplitude(buf, 256), 0.01f)
    }

    @Test fun `zero count returns zero and does not divide by zero`() {
        val buf = ShortArray(256) { Short.MAX_VALUE }
        assertEquals(0f, rmsAmplitude(buf, 0), 0.0001f)
    }

    @Test fun `respects count smaller than buffer`() {
        // First 4 loud, rest silent; count=4 -> ~1.0
        val buf = ShortArray(256)
        for (i in 0 until 4) buf[i] = Short.MAX_VALUE
        assertEquals(1f, rmsAmplitude(buf, 4), 0.001f)
    }
}
```

**Step 2: Run test to verify it fails**

Run: `./gradlew test --tests "com.aiblackbox.portal.ui.voice.AudioAmplitudeTest"`
Expected: FAIL — `rmsAmplitude` unresolved reference.

**Step 3: Write minimal implementation**

Create `SRC/ui/voice/AudioAmplitude.kt`:
```kotlin
package com.aiblackbox.portal.ui.voice

import kotlin.math.min
import kotlin.math.sqrt

/**
 * Root-mean-square loudness of a PCM16 buffer, normalized to 0f..1f.
 *
 * Pure function (no Android deps) so it is unit-testable on the JVM and can be
 * called cheaply from the mic read loop and the playback drain. One pass over
 * samples we already hold — no extra audio reads.
 *
 * @param buffer signed PCM16 samples.
 * @param count number of valid samples in [buffer] (e.g. AudioRecord.read return).
 */
fun rmsAmplitude(buffer: ShortArray, count: Int): Float {
    val n = min(count, buffer.size)
    if (n <= 0) return 0f
    var sumSquares = 0.0
    for (i in 0 until n) {
        val s = buffer[i] / 32768.0  // normalize to -1.0..1.0
        sumSquares += s * s
    }
    return sqrt(sumSquares / n).toFloat().coerceIn(0f, 1f)
}
```

**Step 4: Run test to verify it passes**

Run: `./gradlew test --tests "com.aiblackbox.portal.ui.voice.AudioAmplitudeTest"`
Expected: PASS (5 tests).

**Step 5: Commit**

```bash
git add "app/src/main/java/com/aiblackbox/portal/ui/voice/AudioAmplitude.kt" "app/src/test/java/com/aiblackbox/portal/ui/voice/AudioAmplitudeTest.kt"
git commit -m "feat(android-voice): pure rmsAmplitude() helper with unit tests"
```

---

## Task 3: Surface amplitude + speaker state from VoiceViewModel

**Files:**
- Modify: `SRC/ui/voice/VoiceScreen.kt` (VoiceViewModel: add state ~after line 129; mic loop ~line 322; playback drain ~line 501)

**Step 1: Add the speaker enum + flows**

In `VoiceScreen.kt`, add near the top of the file (outside the class, with other top-level declarations) :
```kotlin
enum class WaveSpeaker { USER, AI, IDLE }
```

Inside `VoiceViewModel`, after the `isMicActive` flow (~line 129):
```kotlin
    // Live waveform inputs — real RMS amplitude (0f..1f) + who is speaking.
    private val _amplitude = MutableStateFlow(0f)
    val amplitude: StateFlow<Float> = _amplitude.asStateFlow()

    private val _waveSpeaker = MutableStateFlow(WaveSpeaker.IDLE)
    val waveSpeaker: StateFlow<WaveSpeaker> = _waveSpeaker.asStateFlow()
```

**Step 2: Feed amplitude from the mic read loop**

In `startMic()`'s read loop, inside `if (readCount > 0)` BEFORE the auto-mute `continue` checks, compute amplitude. After the existing `val readCount = ...` line (~324):
```kotlin
                        if (readCount > 0) {
                            // Waveform: user-side loudness (computed even while muted-by-AEC
                            // so the ribbon reflects the mic, but speaker is USER only when
                            // we are actually sending).
                            val amp = rmsAmplitude(buffer, readCount)
```
Then, where the code currently `continue`s during AI speech / post-speech delay, set IDLE-ish behavior and where it actually sends audio set USER:
- In the `if (isSpeaking || inPostSpeechDelay)` branch (before `continue`), do NOT claim USER (leave speaker as-is; AI path owns it). Optionally `_amplitude.value = amp * 0.0f` is unnecessary — just skip.
- After a successful `voiceClient?.sendAudioChunk(base64)` (~line 350), add:
```kotlin
                                _amplitude.value = amp
                                if (voiceClient?.isAISpeaking?.value != true) {
                                    _waveSpeaker.value = WaveSpeaker.USER
                                }
```
When the mic loop is muted during AI speech, the AI collector (Step 3) drives amplitude instead.

In `stopMic()` (~line 373), reset:
```kotlin
        _amplitude.value = 0f
        if (_waveSpeaker.value == WaveSpeaker.USER) _waveSpeaker.value = WaveSpeaker.IDLE
```

**Step 3: Feed amplitude from the playback drain**

In the audio collector that decodes AI PCM (`initAudioPlayback()`, inside `client.audioOutput.collect { ... }`, ~line 459 after `val pcmBytes = Base64.decode(...)`):
```kotlin
                        // Waveform: AI-side loudness from decoded PCM16 (little-endian bytes).
                        _waveSpeaker.value = WaveSpeaker.AI
                        _amplitude.value = rmsAmplitudeFromBytes(pcmBytes)
```
Add a small byte-oriented helper in `AudioAmplitude.kt` (extend Task 2 file — but since Task 2 is committed, add here as a new function and a quick test in Task 3 commit):

In `SRC/ui/voice/AudioAmplitude.kt` add:
```kotlin
/** RMS for little-endian PCM16 bytes (AI output chunks arrive as ByteArray). */
fun rmsAmplitudeFromBytes(bytes: ByteArray): Float {
    val n = bytes.size / 2
    if (n <= 0) return 0f
    var sumSquares = 0.0
    var i = 0
    while (i + 1 < bytes.size) {
        val lo = bytes[i].toInt() and 0xFF
        val hi = bytes[i + 1].toInt()  // signed high byte
        val sample = (hi shl 8) or lo
        val s = sample / 32768.0
        sumSquares += s * s
        i += 2
    }
    return kotlin.math.sqrt(sumSquares / n).toFloat().coerceIn(0f, 1f)
}
```

Add to `AudioAmplitudeTest.kt`:
```kotlin
    @Test fun `bytes silence is zero`() {
        assertEquals(0f, rmsAmplitudeFromBytes(ByteArray(512)), 0.0001f)
    }

    @Test fun `bytes full scale is approximately one`() {
        val bytes = ByteArray(512)
        var i = 0
        while (i + 1 < bytes.size) { bytes[i] = 0xFF.toByte(); bytes[i + 1] = 0x7F.toByte(); i += 2 } // 0x7FFF
        assertEquals(1f, rmsAmplitudeFromBytes(bytes), 0.001f)
    }
```

When AI finishes (in `stopAudioPlayback()` ~line 528), reset:
```kotlin
        _amplitude.value = 0f
        _waveSpeaker.value = WaveSpeaker.IDLE
```
Also in `VoiceClient`'s `response_complete` path the VM already flips `isAISpeaking=false`; the next mic buffer or idle reset will move speaker off AI. For a crisp return to idle, in the playback drain when `isAISpeaking` becomes false and queue empty, set `_amplitude.value = 0f`.

**Step 4: Verify it compiles**

Run: `./gradlew test --tests "com.aiblackbox.portal.ui.voice.AudioAmplitudeTest"` (covers new byte helper)
Then: `./gradlew compileDebugKotlin`
Expected: tests PASS, compile SUCCESSFUL.

**Step 5: Commit**

```bash
git add "app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt" "app/src/main/java/com/aiblackbox/portal/ui/voice/AudioAmplitude.kt" "app/src/test/java/com/aiblackbox/portal/ui/voice/AudioAmplitudeTest.kt"
git commit -m "feat(android-voice): surface real RMS amplitude + waveSpeaker from VoiceViewModel"
```

---

## Task 4: VoiceWaveform composable (layered ribbon)

**Files:**
- Create: `SRC/ui/voice/VoiceWaveform.kt`

**Step 1: Implement the composable**

Create `SRC/ui/voice/VoiceWaveform.kt`:
```kotlin
package com.aiblackbox.portal.ui.voice

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxRed
import com.aiblackbox.portal.ui.theme.SolidGreen
import kotlin.math.PI
import kotlin.math.sin

private const val VISUAL_GAIN = 2.2f       // speech RMS is quiet; lift it perceptually
private const val IDLE_LEVEL = 0.08f       // gentle breathing baseline when silent
private val AI_TEAL = Color(0xFF1FB5A6)

/**
 * Flowing "ribbon" waveform: three layered translucent sine paths whose height
 * tracks [amplitude] (0f..1f) and whose palette tracks [speaker]. Phase animates
 * continuously so the ribbon always flows; amplitude is spring-eased so loud
 * transients glide instead of snapping.
 */
@Composable
fun VoiceWaveform(
    amplitude: Float,
    speaker: WaveSpeaker,
    modifier: Modifier = Modifier,
) {
    val eased by animateFloatAsState(
        targetValue = (amplitude * VISUAL_GAIN).coerceIn(0f, 1f),
        animationSpec = tween(120),
        label = "amp",
    )

    val phase by rememberInfiniteTransition(label = "wave").animateFloat(
        initialValue = 0f,
        targetValue = (2f * PI).toFloat(),
        animationSpec = infiniteRepeatable(tween(2200, easing = LinearEasing), RepeatMode.Restart),
        label = "phase",
    )

    val (c1, c2) = when (speaker) {
        WaveSpeaker.USER -> BbxAccent to BbxRed
        WaveSpeaker.AI -> SolidGreen to AI_TEAL
        WaveSpeaker.IDLE -> BbxDim to BbxDim
    }
    val color1 by animateColorAsState(c1, tween(400), label = "c1")
    val color2 by animateColorAsState(c2, tween(400), label = "c2")

    val level = if (speaker == WaveSpeaker.IDLE) IDLE_LEVEL else (IDLE_LEVEL + eased).coerceIn(0f, 1f)

    Canvas(modifier = modifier.fillMaxWidth().height(140.dp)) {
        val brush = Brush.horizontalGradient(listOf(color1.copy(alpha = 0.0f), color2, color1.copy(alpha = 0.0f)))
        // 3 layers: (heightFraction, alpha, freq, phaseShift)
        drawRibbon(level * 0.9f, 0.9f, 1.6f, phase, brush)
        drawRibbon(level * 0.6f, 0.5f, 2.4f, phase + 1.1f, brush)
        drawRibbon(level * 0.35f, 0.3f, 3.3f, phase + 2.3f, brush)
    }
}

private fun DrawScope.drawRibbon(
    heightFraction: Float,
    alpha: Float,
    freq: Float,
    phase: Float,
    brush: Brush,
) {
    val midY = size.height / 2f
    val amp = (size.height / 2f) * heightFraction
    val path = Path().apply {
        moveTo(0f, midY)
        val steps = 64
        for (i in 0..steps) {
            val x = size.width * i / steps
            val t = i.toFloat() / steps
            // Envelope so ends taper to the center line (ribbon look).
            val envelope = sin(PI * t).toFloat()
            val y = midY + sin(t * freq * 2f * PI.toFloat() + phase) * amp * envelope
            lineTo(x, y)
        }
    }
    drawPath(path = path, brush = brush, alpha = alpha, style = androidx.compose.ui.graphics.drawscope.Stroke(width = 4f))
}
```
> Note: `Stroke` import is inline-qualified to keep the import block short; the executor may hoist it.

**Step 2: Verify it compiles**

Run: `./gradlew compileDebugKotlin`
Expected: BUILD SUCCESSFUL.

**Step 3: Commit**

```bash
git add "app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceWaveform.kt"
git commit -m "feat(android-voice): add layered flowing-ribbon VoiceWaveform composable"
```

---

## Task 5: Mount the waveform in VoiceScreen

**Files:**
- Modify: `SRC/ui/voice/VoiceScreen.kt` (collect flows ~line 592; place composable between the mic-row/status and the transcript ~line 898)

**Step 1: Collect the new state** (in `VoiceScreen`, with the other `collectAsState()` calls ~line 592):
```kotlin
    val amplitude by viewModel.amplitude.collectAsState()
    val waveSpeaker by viewModel.waveSpeaker.collectAsState()
```

**Step 2: Render it.** Insert above the transcript `LazyColumn` (after the mic/status row `Spacer(Modifier.height(16.dp))` ~line 898), so the ribbon sits between controls and transcript:
```kotlin
        // ── HD flowing-ribbon waveform (real amplitude) ──
        VoiceWaveform(
            amplitude = amplitude,
            speaker = waveSpeaker,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(12.dp))
```

**Step 3: Verify build**

Run: `./gradlew assembleDebug`
Expected: BUILD SUCCESSFUL; APK at `app/build/outputs/apk/debug/`.

**Step 4: Visual verification (screenshot)**

Install/run on device or emulator, connect a Gemini Live session, and confirm:
- Ribbon flows continuously (idle breathing) once connected.
- Warm red ribbon while you speak; cool green/teal while the AI speaks.
- No audio glitches/latency vs. before.

Capture a screenshot for review per [[feedback_screenshot_for_ui]].

**Step 5: Commit**

```bash
git add "app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt"
git commit -m "feat(android-voice): mount HD ribbon waveform in Voice screen"
```

---

## Task 6: LabeledDropdown component

**Files:**
- Create: `SRC/ui/voice/LabeledDropdown.kt`

**Step 1: Implement** an `ExposedDropdownMenuBox` wrapper matching the BlackBox aesthetic:
```kotlin
package com.aiblackbox.portal.ui.voice

import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxDim
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.Neutral500

/**
 * Labeled Material3 dropdown. [options] are (id, displayName); [selectedId] is the
 * current id. Disabled state dims and blocks selection (used for connect-bound
 * settings while CONNECTED — audit I4).
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LabeledDropdown(
    label: String,
    options: List<Pair<String, String>>,
    selectedId: String?,
    enabled: Boolean = true,
    onSelect: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    var expanded by remember { mutableStateOf(false) }
    val selectedLabel = options.firstOrNull { it.first == selectedId }?.second ?: ""

    Text(label, style = MaterialTheme.typography.labelLarge, color = BbxDim)
    ExposedDropdownMenuBox(
        expanded = expanded && enabled,
        onExpandedChange = { if (enabled) expanded = !expanded },
        modifier = modifier.fillMaxWidth().padding(top = 4.dp, bottom = 10.dp),
    ) {
        OutlinedTextField(
            value = selectedLabel,
            onValueChange = {},
            readOnly = true,
            enabled = enabled,
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = expanded && enabled) },
            colors = OutlinedTextFieldDefaults.colors(
                focusedTextColor = BbxWhite,
                unfocusedTextColor = if (enabled) BbxWhite else Neutral500,
                disabledTextColor = Neutral500,
            ),
            modifier = Modifier.menuAnchor().fillMaxWidth(),
        )
        ExposedDropdownMenu(expanded = expanded && enabled, onDismissRequest = { expanded = false }) {
            options.forEach { (id, name) ->
                DropdownMenuItem(
                    text = { Text(name, color = if (id == selectedId) BbxAccent else BbxWhite) },
                    onClick = { onSelect(id); expanded = false },
                )
            }
        }
    }
}
```

**Step 2: Verify build**

Run: `./gradlew compileDebugKotlin`
Expected: BUILD SUCCESSFUL. (If `menuAnchor()` is deprecated in the resolved Material3 version, use the `MenuAnchorType` overload — executor adjusts.)

**Step 3: Commit**

```bash
git add "app/src/main/java/com/aiblackbox/portal/ui/voice/LabeledDropdown.kt"
git commit -m "feat(android-voice): add LabeledDropdown (ExposedDropdownMenuBox) component"
```

---

## Task 7: Replace chip-rows with dropdowns

**Files:**
- Modify: `SRC/ui/voice/VoiceScreen.kt` (backend selector ~717; voice selector ~744; `RealtimeConfigBlock` ~1023; `GeminiConfigBlock` ~1083)

**Step 1:** Replace the **backend** chip `Row` (~717-735) with:
```kotlin
        LabeledDropdown(
            label = "Backend",
            options = VoiceBackend.entries.map { it.id to it.displayName },
            selectedId = backend.id,
            enabled = !isConnected,
            onSelect = { id -> VoiceBackend.entries.firstOrNull { it.id == id }?.let(viewModel::setBackend) },
        )
```

**Step 2:** Replace the **voice** chip `Row` (~744-769) with:
```kotlin
        LabeledDropdown(
            label = "Voice",
            options = voicesForBackend(backend).map { it to voiceLabel(backend, it) },
            selectedId = voice,
            enabled = !isConnected,
            onSelect = viewModel::setVoice,
        )
```

**Step 3:** In `RealtimeConfigBlock` and `GeminiConfigBlock`, replace each `ChipRowPicker(...)` call with `LabeledDropdown(...)` (same args: label/options/selectedId/enabled/onSelect). The `__auto__` nullable mapping in `GeminiConfigBlock` stays identical — only the rendering component changes. Keep the `OutlinedTextField` idle-timeout field and the conditional `if (vadType == ...)` / `if (model in THINKING_CAPABLE)` logic unchanged.

**Step 4:** Delete the now-unused `ChipRowPicker` composable (~965-1020) if no references remain. Verify with:
```bash
grep -rn "ChipRowPicker" "app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt"
```
Expected: no matches after replacement.

**Step 5: Verify build + screenshot**

Run: `./gradlew assembleDebug` → BUILD SUCCESSFUL. Screenshot the settings with dropdowns open for review.

**Step 6: Commit**

```bash
git add "app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt"
git commit -m "feat(android-voice): convert voice settings chip-rows to dropdowns"
```

---

## Task 8: SettingsPane — collapsible glass card + auto-collapse + summary pill

**Files:**
- Modify: `SRC/ui/voice/VoiceScreen.kt` (wrap the settings block; add `expanded` state + auto-collapse effect)

**Step 1:** Add settings-expanded state near the other `remember`s in `VoiceScreen` (~650):
```kotlin
    var settingsExpanded by remember { mutableStateOf(true) }
    LaunchedEffect(isConnected) { if (isConnected) settingsExpanded = false }
```

**Step 2:** Wrap the Backend/Voice/config dropdowns (everything between the header and the mic-row) in a glass card with a tappable header and `AnimatedVisibility` body. Replace the start of that block with:
```kotlin
        // ── Collapsible settings pane ──
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .glassSurface(shape = RoundedCornerShape(16.dp), bg = Neutral100)
                .padding(12.dp)
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.fillMaxWidth().clickable {
                    view.performHapticFeedback(HapticFeedbackConstants.CONTEXT_CLICK)
                    settingsExpanded = !settingsExpanded
                }
            ) {
                Text("⚙️", style = MaterialTheme.typography.titleMedium)
                Spacer(Modifier.width(8.dp))
                Text(
                    if (settingsExpanded) "Settings"
                    else "${backend.displayName} · $voice",
                    style = MaterialTheme.typography.titleSmall,
                    color = BbxWhite,
                    modifier = Modifier.weight(1f),
                )
                Text(if (settingsExpanded) "▴" else "▾", color = BbxDim)
            }
            androidx.compose.animation.AnimatedVisibility(visible = settingsExpanded) {
                Column(modifier = Modifier.fillMaxWidth().padding(top = 12.dp)) {
                    // ⟵ MOVE the Backend / Voice / RealtimeConfigBlock / GeminiConfigBlock
                    //     dropdowns (from Task 7) inside here.
                }
            }
        }
        Spacer(Modifier.height(16.dp))
```

**Step 3:** Move the dropdown calls (backend, voice, and the `when (backend) { ... }` config block) into the `AnimatedVisibility` `Column`. Remove the old standalone `error?.let { }` only if relocating; keep error display visible above the pane.

**Step 4: Verify build + behavior**

Run: `./gradlew assembleDebug` → BUILD SUCCESSFUL.
Manual check: pane open while disconnected; **auto-collapses to `⚙ Gemini Live · Orus ▾` on connect**; tap re-expands; chevron toggles. Screenshot both states for review.

**Step 5: Commit**

```bash
git add "app/src/main/java/com/aiblackbox/portal/ui/voice/VoiceScreen.kt"
git commit -m "feat(android-voice): collapsible settings pane with auto-collapse + summary pill"
```

---

## Task 9: Final integration QA + version bump

**Files:**
- Modify: `SRC/.../build.gradle` or `app/build.gradle` (`versionCode` / `versionName`) per CLAUDE.md release ritual.

**Step 1: Full build + all unit tests**

Run:
```bash
./gradlew test assembleDebug
```
Expected: BUILD SUCCESSFUL; all unit tests pass.

**Step 2: End-to-end manual QA checklist** (on device, Gemini Live session):
- [ ] Fresh launch → Gemini Live default model is **Gemini 3.1 Flash Live (Preview)**; Thinking level dropdown visible.
- [ ] Settings pane open while disconnected; all settings are dropdowns.
- [ ] Connect → pane auto-collapses to summary pill; waveform takes the stage.
- [ ] Ribbon breathes at idle; **warm** on user speech; **cool** on AI speech.
- [ ] No regressions: transcript scrolls, provenance, disconnect, mic mute all work.
- [ ] Audio quality unchanged (no glitch/latency from amplitude taps).

**Step 3: Bump version** (per CLAUDE.md "Version bump") in `app/build.gradle`:
```
versionCode <prev+1>
versionName "<bumped>"
```

**Step 4: Commit**

```bash
git add "app/build.gradle"
git commit -m "chore(android): bump version for voice UI HD waveform release"
```

**Step 5:** Capture final screenshots/short screen recording of all three states (idle / user / AI) for the BlackBox snapshot.

---

## Definition of done

- All unit tests pass (`./gradlew test`); debug APK builds (`./gradlew assembleDebug`).
- Gemini Live defaults to `gemini-3.1-flash-live-preview` on fresh install.
- Every voice setting is a dropdown inside a collapsible glass pane that auto-collapses to a summary pill on connect.
- HD flowing-ribbon waveform reacts to real amplitude with warm (user) / cool (AI) palettes and an idle breathing baseline.
- Web Portal untouched.
- Manual QA checklist (Task 9) fully green, screenshots captured.

## Risks / notes

- **Material3 `menuAnchor()` deprecation:** newer Material3 wants `menuAnchor(MenuAnchorType.PrimaryNotEditable)`. Executor adjusts to the resolved version; build will flag it.
- **Amplitude liveliness:** `VISUAL_GAIN`/`IDLE_LEVEL` in `VoiceWaveform.kt` are tuning knobs — adjust after first on-device look (Brandon reviews via screenshot).
- **Speaker flapping:** during the 1.2s post-speech echo window the mic is muted, so AI→IDLE→USER transitions stay clean; verify no rapid color flicker on device.
- **No instrumented UI tests:** Canvas/animation/dropdown behavior is verified by build + screenshot, not JVM unit tests (would require an emulator + Compose UI test harness not currently wired for these screens).
