package com.aiblackbox.portal.ui.components.wave

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableDoubleStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Modifier
import com.aiblackbox.portal.ui.components.aurora.Aurora

// =============================================================================
// WaveformStylePreview — the live, real-renderer preview the Settings picker
// shows above the style list. It is the ACTUAL [VoiceVisualizer] at real size,
// fed the deterministic [WaveformDemoDriver] instead of a live mic/model — so
// what an operator previews is pixel-for-pixel what they will see in the composer,
// the player bar and the voice screen, never a mock drawing.
//
// The only thing this composable owns is a wall-clock second counter driven by
// withFrameNanos; every frame it asks the demo driver for the voices at that
// instant and hands them to VoiceVisualizer, which owns all the real drawing,
// feed and idle discipline. All the SHAPE lives in the (JVM-tested) driver, so
// this composable is deliberately thin — there is nothing here to unit-test that
// the driver and VoiceVisualizer tests do not already cover.
//
// `pauseWhenIdle = false`: a preview should keep flowing while a voice is present
// rather than parking between the driver's frames; the driver's own pause phase
// (an empty voice list) still lets it settle, matching a real conversation's
// silence.
// =============================================================================

/**
 * Animate the selected [styleId] with the synthetic demo conversation, filling
 * [modifier]'s bounds. Put it on a dark background so the additive glow reads.
 *
 * @param styleId the persisted style id to preview; unknown/absent → aurora, via
 *   VoiceVisualizer's own resolve, so the preview can never crash on a stale id.
 */
@Composable
fun WaveformStylePreview(
    styleId: String,
    modifier: Modifier = Modifier,
) {
    val driver = remember { WaveformDemoDriver() }
    // Seconds since this preview's first frame — the clock injected into the driver.
    var timeSec by remember { mutableDoubleStateOf(0.0) }
    // First-frame nanos, boxed so the frame callback can seed it without recomposing.
    val startNanos = remember { longArrayOf(0L) }

    LaunchedEffect(Unit) {
        startNanos[0] = 0L
        while (true) {
            withFrameNanos { t ->
                if (startNanos[0] == 0L) startNanos[0] = t
                timeSec = (t - startNanos[0]) / 1_000_000_000.0
            }
        }
    }

    VoiceVisualizer(
        styleId = styleId,
        voices = driver.voicesAt(timeSec),
        modifier = modifier,
        pauseWhenIdle = false,
        restLevel = Aurora.BASELINE,
        // BOTH channels, or the pixel-for-pixel promise above stops holding: six of the ten styles
        // read the spectrum on a live mic, and a preview without one would run their four-band
        // FALLBACK — an operator would be choosing between looks the composer does not draw. The
        // driver synthesises the second channel in the LIVE channel's own range; see
        // WaveformDemoDriver.binsAt for what makes it a spectrum rather than four widened bands.
        spectra = driver.binsAt(timeSec),
    )
}
