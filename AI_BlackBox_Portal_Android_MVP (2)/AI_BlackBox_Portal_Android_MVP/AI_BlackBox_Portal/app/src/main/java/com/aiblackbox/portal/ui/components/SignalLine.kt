package com.aiblackbox.portal.ui.components

// =============================================================================
// SignalLine — "The Signal" (Phase 2, Android native).
//
// ONE red, monospace line that WAVES and MORPHS through the REAL system telemetry
// of a chat turn (embed / search / rank / context / tool / generating). It
// replaces the old ThinkingIndicator (fake cycling "thinking" phrases).
//
// PRESENTATION-ONLY. This composable renders whatever label string it is handed
// via [label] (fed from ChatViewModel._signalLabel — the transient, per-turn
// telemetry flow). It NEVER reads, mutates, or persists conversation content.
//
// The morph + wave algorithm is ported verbatim from the approved prototype
// (the-signal-prototype.html) and the shipped web module (Portal/signal-feed.js):
//   • MORPH — on each new label, diff per POSITION vs the previous line; only
//     positions whose character CHANGED animate (a brief fade-out → scramble →
//     settle, staggered by index). Shared characters hold in place.
//   • WAVE  — a continuous per-character sine offset on the baseline.
//
// Architecture mirrors EmberParticles.kt: the timing/morph state is UI-FREE plain
// Kotlin (SignalMorph), and a single Canvas driven by a battery-safe withFrameNanos
// loop reads a frame clock so only the DRAW phase invalidates each frame — the
// enclosing chat bubble never recomposes per frame. Reduced-motion users get an
// instant set (no scramble, no wave).
// =============================================================================

import android.content.Context
import android.graphics.Paint
import android.graphics.Typeface
import android.provider.Settings
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlin.math.PI
import kotlin.math.sin

// hsl(2 100% 62%) — the exact "signal red" of the prototype / web module.
private val SignalRed = Color(0xFFFF443D)

// Charset for the brief scramble/decode tick on changed chars (ported verbatim).
private val SIGNAL_SCRAMBLE = "ABCDEF0123456789·→λ#@%$".toCharArray()

private const val SIGNAL_WAVE_SPEED = 1.1f

// =============================================================================
// SignalMorph — UI-free per-character morph state machine. Plain Kotlin (no
// Compose), so the diff/timing logic is deterministic and JVM-testable. Ported
// from the prototype's `setLine`: push() diffs the new label against the previous
// one and marks which cells animate; the *At() readers compute the glyph, opacity,
// hot-glow and lift for cell i at a given millis-like time.
// =============================================================================
private class SignalMorph(private val reduceMotion: Boolean) {

    private class Cell {
        var from = ' '
        var target = ' '
        var scramble = ' '
        var changed = false
        var delayMs = 0.0
    }

    private var curText = ""
    private var morphStartMs = 0.0
    private val cells = ArrayList<Cell>()

    val size: Int get() = cells.size

    /** The final (target) glyph for cell [i] — stable across the whole morph. */
    fun targetAt(i: Int): Char = cells[i].target

    /**
     * Diff to [label] at [nowMs]. Grows/shrinks the cell row to the longer of the
     * new/old text; unchanged positions hold, changed positions get a staggered
     * fade→scramble→settle. Under reduced motion every cell is an instant set.
     */
    fun push(label: String, nowMs: Double) {
        val l = maxOf(label.length, curText.length)
        while (cells.size < l) cells.add(Cell())
        while (cells.size > l) cells.removeAt(cells.size - 1)
        // Faster per-char cascade for longer lines (ported: stagger = clamp(520/L)).
        val stagger = maxOf(11.0, minOf(34.0, Math.round(520.0 / maxOf(l, 1)).toDouble()))
        for (i in 0 until l) {
            val c = cells[i]
            c.from = if (i < curText.length) curText[i] else ' '
            c.target = if (i < label.length) label[i] else ' '
            c.delayMs = i * stagger
            c.changed = !reduceMotion && c.target != c.from
            if (c.changed) {
                c.scramble = SIGNAL_SCRAMBLE[(Math.random() * SIGNAL_SCRAMBLE.size).toInt()]
            }
        }
        curText = label
        morphStartMs = nowMs
    }

    private fun local(i: Int, nowMs: Double) = (nowMs - morphStartMs) - cells[i].delayMs

    /** Glyph shown for cell [i] at [nowMs]: old → scramble → target. */
    fun glyphAt(i: Int, nowMs: Double): Char {
        val c = cells[i]
        if (!c.changed) return c.target
        val t = local(i, nowMs)
        return when {
            t < 70.0 -> c.from       // holding / fading the old char out
            t < 150.0 -> c.scramble  // brief decode scramble
            else -> c.target         // settled
        }
    }

    /** Opacity envelope: fade old out (0–150ms) then new in (150–310ms). */
    fun alphaAt(i: Int, nowMs: Double): Float {
        if (!cells[i].changed) return 1f
        val t = local(i, nowMs)
        return when {
            t < 0.0 -> 1f
            t < 150.0 -> 1f - 0.85f * (t / 150.0).toFloat()
            t < 310.0 -> 0.15f + 0.85f * ((t - 150.0) / 160.0).toFloat()
            else -> 1f
        }.coerceIn(0f, 1f)
    }

    /** White-hot glow factor (1→0) over the 420ms after a char changes. */
    fun hotAt(i: Int, nowMs: Double): Float {
        if (!cells[i].changed) return 0f
        val t = local(i, nowMs)
        if (t < 0.0 || t > 420.0) return 0f
        return (1.0 - t / 420.0).toFloat().coerceIn(0f, 1f)
    }

    /** Upward lift (in em) during the swap (ported translateY(-.28em)). */
    fun liftEmAt(i: Int, nowMs: Double): Float {
        if (!cells[i].changed) return 0f
        val t = local(i, nowMs)
        return when {
            t < 0.0 -> 0f
            t < 150.0 -> (-0.28 * (t / 150.0)).toFloat()
            t < 310.0 -> (-0.28 * (1.0 - (t - 150.0) / 160.0)).toFloat()
            else -> 0f
        }
    }
}

/** Continuous per-character sine wave offset (px). 0 under reduced motion. */
private fun signalWaveOffsetPx(i: Int, nowMs: Double, ampPx: Float): Float {
    if (ampPx == 0f) return 0f
    val ph = nowMs * 0.001 * SIGNAL_WAVE_SPEED * PI
    return (ampPx * sin(ph - i * 0.30)).toFloat()
}

/** True when the OS "remove animations" / animator-scale-0 accessibility path is on. */
private fun signalReduceMotion(context: Context): Boolean = try {
    Settings.Global.getFloat(
        context.contentResolver,
        Settings.Global.ANIMATOR_DURATION_SCALE,
        1f,
    ) == 0f
} catch (_: Exception) {
    false
}

// =============================================================================
// SignalLine — the Compose surface. Renders one waving/morphing red monospace
// line for [label]. No bubble, no outline — just the glowing line. When [label]
// is null/blank the row is empty (the dissolve), reserving its height so the
// bubble doesn't jump.
// =============================================================================
@Composable
fun SignalLine(label: String?, modifier: Modifier = Modifier) {
    val density = LocalDensity.current
    val context = LocalContext.current
    val reduceMotion = remember { signalReduceMotion(context) }
    val morph = remember { SignalMorph(reduceMotion) }
    val frame = remember { mutableLongStateOf(0L) }
    // The loop is keyed on `label`; rememberUpdatedState makes sure the push reads
    // the CURRENT value even if it changed between launch and the first frame.
    val currentLabel by rememberUpdatedState(label)

    val fontSizePx = with(density) { 13.sp.toPx() }
    val waveAmpPx = if (reduceMotion) 0f else with(density) { 4.dp.toPx() }
    val glowPx = with(density) { 7.dp.toPx() }

    // One reusable text paint (monospace, soft red glow). Built once per size.
    val paint = remember(fontSizePx, glowPx) {
        Paint(Paint.ANTI_ALIAS_FLAG).apply {
            typeface = Typeface.MONOSPACE
            textSize = fontSizePx
            setShadowLayer(glowPx, 0f, 0f, SignalRed.copy(alpha = 0.55f).toArgb())
        }
    }
    // Monospace → uniform advance (matches the web module's ui-monospace cells).
    val charWidth = remember(paint) { paint.measureText("0") }
    val ascent = remember(paint) { paint.fontMetrics.ascent }
    val descent = remember(paint) { paint.fontMetrics.descent }

    // Single frame loop drives BOTH the morph consume and the wave. A new [label]
    // restarts the effect → morph.push diffs it against the previous line. Reduced
    // motion sets the row once and idles until the next label (no 60fps loop).
    LaunchedEffect(label) {
        val startNs = withFrameNanos { it }
        morph.push(currentLabel ?: "", startNs / 1_000_000.0)
        frame.longValue = startNs
        if (reduceMotion) return@LaunchedEffect
        while (true) {
            withFrameNanos { t -> frame.longValue = t }
        }
    }

    Canvas(
        modifier = modifier
            .fillMaxWidth()
            .height(with(density) { (fontSizePx * 1.8f + waveAmpPx * 2f).toDp() }),
    ) {
        // Reading the frame clock HERE invalidates only the DRAW phase each frame
        // (never recomposition) — the enclosing bubble does not recompose per frame.
        val nowMs = frame.longValue / 1_000_000.0
        val n = morph.size
        if (n > 0) {
            val canvas = drawContext.canvas.nativeCanvas
            val baseY = size.height / 2f - (ascent + descent) / 2f
            val buf = CharArray(1)
            for (i in 0 until n) {
                val ch = morph.glyphAt(i, nowMs)
                if (ch == ' ') continue
                val hot = morph.hotAt(i, nowMs)
                val col = lerp(SignalRed, Color.White, hot).copy(alpha = morph.alphaAt(i, nowMs))
                paint.color = col.toArgb()
                val y = baseY +
                    signalWaveOffsetPx(i, nowMs, waveAmpPx) +
                    morph.liftEmAt(i, nowMs) * fontSizePx
                buf[0] = ch
                canvas.drawText(buf, 0, 1, i * charWidth, y, paint)
            }
        }
    }
}
