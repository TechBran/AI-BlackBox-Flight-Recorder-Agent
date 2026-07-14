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

// Readability fix (mirrors web signal-feed.js @ 6e5102a): the wave is NOT a
// continuous ripple — it is a per-line ENTRANCE that eases out to FLAT and holds.
// On each new line it decays over WAVE_DECAY_MS then sits perfectly still so the
// line is readable until the next label morphs in.
private const val WAVE_DECAY_MS = 700.0

/**
 * Per-line wave amplitude envelope at [elapsedMs] since the line arrived: an
 * ease-out (k²) from [peakPx] to 0 over [WAVE_DECAY_MS], then flat 0 forever.
 * Pure — pinned by SignalWaveEnvelopeTest. Reduced motion passes peak 0 → always 0.
 */
internal fun signalWaveEnvelope(elapsedMs: Double, peakPx: Float): Float {
    if (peakPx == 0f) return 0f
    val k = (1.0 - elapsedMs / WAVE_DECAY_MS).coerceIn(0.0, 1.0)
    return (peakPx * k * k).toFloat()
}

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
    /** Frame time (ms) this line began — the shared origin for the morph AND the
     *  wave-decay envelope (the wave and the scramble both start here). */
    var morphStartMs = 0.0
        private set
    /** ms after [morphStartMs] by which every changed cell has fully settled
     *  (last changed-cell stagger + its 420ms hot fade). 0 when nothing animates.
     *  The frame loop must run at least this long so stopping never freezes a
     *  half-scrambled glyph. */
    var morphSettleMs = 0.0
        private set
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
        // Longest changed-cell animation = its stagger delay + the 420ms hot fade.
        var maxChangedDelay = -1.0
        for (i in 0 until l) if (cells[i].changed) maxChangedDelay = maxOf(maxChangedDelay, cells[i].delayMs)
        morphSettleMs = if (maxChangedDelay < 0.0) 0.0 else maxChangedDelay + 420.0
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

/** Per-character sine wave offset (px) at the already-decayed [amp]. 0 → flat. */
private fun signalWaveOffsetPx(i: Int, nowMs: Double, amp: Float): Float {
    if (amp == 0f) return 0f
    val ph = nowMs * 0.001 * SIGNAL_WAVE_SPEED * PI
    return (amp * sin(ph - i * 0.30)).toFloat()
}

private const val SIGNAL_SWEEP_SPEED = 0.85
private const val SIGNAL_SWEEP_WL = 13.0

/** Continuous highlight SWEEP crest (0..1) for cell [i] at [nowMs]: a bright band
 *  travels left→right so the line reads as live/streaming (mirrors web ea06cbf).
 *  Only the leading half of the sine lights (s²) → a sharp crest. */
internal fun signalSweep(i: Int, nowMs: Double): Float {
    val t = nowMs * 0.001 * SIGNAL_SWEEP_SPEED
    val s = sin((i / SIGNAL_SWEEP_WL - t) * 2.0 * PI)
    return (if (s > 0.0) s * s else 0.0).toFloat()
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
    // Peak wave amplitude HALVED (was 4dp) → a subtle entrance, not a busy ripple.
    // 0 under reduced motion (always flat).
    val peakAmpPx = if (reduceMotion) 0f else with(density) { 2.dp.toPx() }
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
    // restarts the effect → morph.push diffs it against the previous line and stamps
    // a fresh wave origin. The loop STOPS once the wave has decayed to flat AND the
    // morph has fully settled — then the line HOLDS motionless and readable until the
    // next label re-arms it (LaunchedEffect(label) restarts). Reduced motion sets the
    // row once and idles immediately (no 60fps loop, always flat).
    LaunchedEffect(label) {
        val startNs = withFrameNanos { it }
        morph.push(currentLabel ?: "", startNs / 1_000_000.0)
        frame.longValue = startNs
        if (reduceMotion) return@LaunchedEffect
        // Run CONTINUOUSLY while this line is shown so the highlight sweep keeps
        // scanning (Brandon: the flat-hold looked stagnant). The loop is cancelled
        // automatically when the label changes (LaunchedEffect restarts) or the line
        // is removed from composition.
        while (true) {
            val t = withFrameNanos { it }
            frame.longValue = t
        }
    }

    Canvas(
        modifier = modifier
            .fillMaxWidth()
            .height(with(density) { (fontSizePx * 1.8f + peakAmpPx * 2f).toDp() }),
    ) {
        // Reading the frame clock HERE invalidates only the DRAW phase each frame
        // (never recomposition) — the enclosing bubble does not recompose per frame.
        val nowMs = frame.longValue / 1_000_000.0
        val n = morph.size
        if (n > 0) {
            // Continuous highlight SWEEP: a bright crest scans across the line,
            // brightening (toward white) + lifting each char as it passes, so the
            // line reads as live/streaming while the text stays put and readable.
            val canvas = drawContext.canvas.nativeCanvas
            val baseY = size.height / 2f - (ascent + descent) / 2f
            val liftPx = peakAmpPx * 1.1f   // small upward lift at the crest
            val buf = CharArray(1)
            for (i in 0 until n) {
                val ch = morph.glyphAt(i, nowMs)
                if (ch == ' ') continue
                val hot = morph.hotAt(i, nowMs)
                val sweep = if (reduceMotion) 0f else signalSweep(i, nowMs)
                // brighten toward white by the morph hot-glow AND the sweep crest.
                val bright = (hot + sweep * 0.7f).coerceIn(0f, 1f)
                val col = lerp(SignalRed, Color.White, bright).copy(alpha = morph.alphaAt(i, nowMs))
                paint.color = col.toArgb()
                val y = baseY - sweep * liftPx + morph.liftEmAt(i, nowMs) * fontSizePx
                buf[0] = ch
                canvas.drawText(buf, 0, 1, i * charWidth, y, paint)
            }
        }
    }
}
