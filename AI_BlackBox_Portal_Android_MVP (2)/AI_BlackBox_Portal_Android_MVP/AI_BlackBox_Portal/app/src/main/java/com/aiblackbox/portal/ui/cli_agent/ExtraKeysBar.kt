package com.aiblackbox.portal.ui.cli_agent

// ExtraKeysBar — Termux-style fixed key bar above the soft keyboard.
//
// Default keys: Esc | Tab | Ctrl | Alt | ← ↓ ↑ → | / | @ | 🎤 | -
// Horizontal scroll on the same LazyRow exposes Home | End | PgUp | PgDn.
// Tap Ctrl/Alt → sticky modifier for next key only (Pending).
// Long-press Ctrl/Alt → locked sticky modifier until tapped again (Locked).
// Auto-collapse when LocalConfiguration.current.keyboard != KEYBOARD_NOKEYS
// (Bluetooth/USB hardware keyboard attached).
//
// Public API:
//   ExtraKeysBar(
//     onKeyBytes: (ByteArray) -> Unit,           // forwarded to ws.sendBytes(...)
//     micSlot: @Composable () -> Unit = {},      // CliMicButton (Task 6.2)
//     attachSlot: (@Composable () -> Unit)? = null, // CliAttachButton (zellij only)
//     modifier: Modifier = Modifier,
//   )
// attachSlot is NULLABLE (not an empty-lambda default like micSlot) so call
// sites without an attach control — the legacy tmux TerminalScreen — render
// NO slot at all instead of a blank 44dp min-size gap in the bar.

import android.content.res.Configuration
import androidx.compose.foundation.clickable
import com.aiblackbox.portal.ui.feedback.clickFeedback
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border

/**
 * Sticky modifier state for Ctrl/Alt:
 *   Off     — modifier inactive
 *   Pending — armed for the next key tap only (one-shot)
 *   Locked  — armed until the user taps the modifier again
 */
private enum class StickyState { Off, Pending, Locked }

/**
 * One byte sequence for the terminal. `isLetter = true` means the bytes
 * represent a single ASCII letter (a-z / A-Z) and should combine with
 * Ctrl/Alt modifiers if armed. `shiftBytes` provides the alternate byte
 * sequence sent when Shift is armed (xterm modifier encoding — e.g.
 * Shift+PgUp = ESC[5;2~ instead of bare ESC[5~). When null and Shift is
 * armed, the key falls back to its bare bytes.
 */
private data class KeySpec(
    val label: String,
    val bytes: ByteArray,
    val isLetter: Boolean = false,
    val widthDp: Int = 44,
    val shiftBytes: ByteArray? = null,
)

private val ESC = byteArrayOf(0x1b.toByte())

/**
 * Resolve the bytes an ExtraKeysBar key emits, applying any armed sticky
 * modifiers. Pure + [internal] so it's unit-testable without a Composable
 * host — the regression guard for the "dead Esc" bug pins Esc → exactly
 * `[0x1b]` under every modifier combination here.
 *
 *   - Ctrl + single ASCII letter → the control character (letter & 0x1f).
 *   - Alt  + letter              → ESC-prefixed (meta) sequence.
 *   - Shift + a key that defines [shiftBytes] → the xterm modified sequence.
 *   - otherwise                  → the key's bare bytes (Esc, Tab, /, …).
 *
 * Esc carries `isLetter = false` and `shiftBytes = null`, so none of the
 * modifier branches can ever fire for it — it resolves to its bare `[0x1b]`
 * regardless of which modifiers are armed.
 */
internal fun resolveKeyBytes(
    bytes: ByteArray,
    isLetter: Boolean,
    shiftBytes: ByteArray?,
    ctrlArmed: Boolean,
    altArmed: Boolean,
    shiftArmed: Boolean,
): ByteArray = when {
    ctrlArmed && isLetter && bytes.size == 1 ->
        byteArrayOf((bytes[0].toInt() and 0x1f).toByte())
    altArmed && isLetter -> ESC + bytes
    shiftArmed && shiftBytes != null -> shiftBytes
    else -> bytes
}

@Composable
fun ExtraKeysBar(
    onKeyBytes: (ByteArray) -> Unit,
    onScrollLines: (Int) -> Unit = {},  // -ve = into history; +ve = toward live
    micSlot: @Composable () -> Unit = {},
    attachSlot: (@Composable () -> Unit)? = null,
    modifier: Modifier = Modifier,
) {
    // Bluetooth / USB hardware keyboard attached → no soft-key bar needed.
    val configuration = LocalConfiguration.current
    if (configuration.keyboard != Configuration.KEYBOARD_NOKEYS) {
        return
    }

    var ctrl by remember { mutableStateOf(StickyState.Off) }
    var alt by remember { mutableStateOf(StickyState.Off) }
    var shift by remember { mutableStateOf(StickyState.Off) }

    /**
     * Apply armed Ctrl/Alt/Shift to outgoing bytes and clear Pending state.
     * Locked state survives.
     */
    fun fireKey(spec: KeySpec) {
        val out = resolveKeyBytes(
            bytes = spec.bytes,
            isLetter = spec.isLetter,
            shiftBytes = spec.shiftBytes,
            ctrlArmed = ctrl != StickyState.Off,
            altArmed = alt != StickyState.Off,
            shiftArmed = shift != StickyState.Off,
        )
        onKeyBytes(out)
        if (ctrl == StickyState.Pending) ctrl = StickyState.Off
        if (alt == StickyState.Pending) alt = StickyState.Off
        if (shift == StickyState.Pending) shift = StickyState.Off
    }

    /** Tap on Ctrl/Alt: Off → Pending; Pending → Off; Locked → Off. */
    fun toggleSticky(current: StickyState): StickyState = when (current) {
        StickyState.Off -> StickyState.Pending
        StickyState.Pending -> StickyState.Off
        StickyState.Locked -> StickyState.Off
    }

    /** Long-press on Ctrl/Alt: any state → Locked (or Off if already Locked). */
    fun lockSticky(current: StickyState): StickyState = when (current) {
        StickyState.Locked -> StickyState.Off
        else -> StickyState.Locked
    }

    Surface(
        modifier = modifier.fillMaxWidth(),
        color = MaterialTheme.colorScheme.surfaceVariant,
        tonalElevation = 2.dp,
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .height(44.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            // --- Esc — PINNED outside the scrollable LazyRow ------------------
            // Root cause of the "dead Esc" regression: as LazyRow item[0] at the
            // left scroll edge, Esc's tap was arbitrated by the row's horizontal
            // drag/over-scroll detector — a tap with the tiniest sideways drift
            // (or one landing while a busy claude TUI churns recomposition) got
            // claimed as a scroll and the click never fired, so no 0x1b was sent.
            // (The byte path itself is sound: fireKey → onKeyBytes →
            // client.sendBytes emits exactly [0x1b] for Esc under every modifier
            // state — see resolveKeyBytes.) A fixed slot never competes with the
            // row's scroll, so a tap always lands as one 0x1b, and the interrupt
            // key stays permanently visible.
            Box(modifier = Modifier.padding(start = 6.dp, end = 4.dp)) {
                ExtraKey(label = "Esc", widthDp = 48) {
                    fireKey(KeySpec("Esc", ESC))
                }
            }
            LazyRow(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxHeight(),
                contentPadding = PaddingValues(start = 0.dp, top = 4.dp, end = 6.dp, bottom = 4.dp),
                horizontalArrangement = Arrangement.spacedBy(4.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
            // --- Tab ---
            item {
                ExtraKey(label = "Tab", widthDp = 48) {
                    fireKey(KeySpec("Tab", byteArrayOf(0x09)))
                }
            }
            // --- Enter (CR) ---
            item {
                ExtraKey(label = "↵", widthDp = 48) {
                    fireKey(KeySpec("↵", byteArrayOf(0x0d)))
                }
            }
            // --- Backspace (DEL = 0x7F, the convention modern shells expect) ---
            item {
                ExtraKey(label = "⌫", widthDp = 48) {
                    fireKey(KeySpec("⌫", byteArrayOf(0x7f)))
                }
            }
            // --- Slash — moved up front so it's always visible (slash menu
            //     in Claude Code requires a clean tap, and the previous
            //     position 11 was in the LazyRow's scroll-edge zone where
            //     gestures can absorb taps as scroll starts). ---
            item {
                ExtraKey(label = "/") {
                    fireKey(KeySpec("/", byteArrayOf('/'.code.toByte())))
                }
            }
            // --- Ctrl (modifier) ---
            item {
                ModifierKey(
                    label = "Ctrl",
                    state = ctrl,
                    onTap = { ctrl = toggleSticky(ctrl) },
                    onLongPress = { ctrl = lockSticky(ctrl) },
                )
            }
            // --- Alt (modifier) ---
            item {
                ModifierKey(
                    label = "Alt",
                    state = alt,
                    onTap = { alt = toggleSticky(alt) },
                    onLongPress = { alt = lockSticky(alt) },
                )
            }
            // --- Shift (modifier) — armed for one-shot via tap, locked
            //     via long-press. Required for keys like Shift+PgUp /
            //     Shift+PgDn (the actual scroll trigger Claude Code's
            //     binding wants — bare PgUp = ESC[5~ doesn't fire its
            //     scroll handler; ESC[5;2~ does).
            item {
                ModifierKey(
                    label = "Shift",
                    state = shift,
                    onTap = { shift = toggleSticky(shift) },
                    onLongPress = { shift = lockSticky(shift) },
                )
            }
            // --- Arrows (with Shift+arrow xterm sequences) ---
            item {
                ExtraKey(label = "←") {
                    fireKey(KeySpec(
                        label = "←",
                        bytes = byteArrayOf(0x1b, '['.code.toByte(), 'D'.code.toByte()),
                        shiftBytes = byteArrayOf(
                            0x1b, '['.code.toByte(),
                            '1'.code.toByte(), ';'.code.toByte(),
                            '2'.code.toByte(), 'D'.code.toByte()
                        ),
                    ))
                }
            }
            item {
                ExtraKey(label = "↓") {
                    fireKey(KeySpec(
                        label = "↓",
                        bytes = byteArrayOf(0x1b, '['.code.toByte(), 'B'.code.toByte()),
                        shiftBytes = byteArrayOf(
                            0x1b, '['.code.toByte(),
                            '1'.code.toByte(), ';'.code.toByte(),
                            '2'.code.toByte(), 'B'.code.toByte()
                        ),
                    ))
                }
            }
            item {
                ExtraKey(label = "↑") {
                    fireKey(KeySpec(
                        label = "↑",
                        bytes = byteArrayOf(0x1b, '['.code.toByte(), 'A'.code.toByte()),
                        shiftBytes = byteArrayOf(
                            0x1b, '['.code.toByte(),
                            '1'.code.toByte(), ';'.code.toByte(),
                            '2'.code.toByte(), 'A'.code.toByte()
                        ),
                    ))
                }
            }
            item {
                ExtraKey(label = "→") {
                    fireKey(KeySpec(
                        label = "→",
                        bytes = byteArrayOf(0x1b, '['.code.toByte(), 'C'.code.toByte()),
                        shiftBytes = byteArrayOf(
                            0x1b, '['.code.toByte(),
                            '1'.code.toByte(), ';'.code.toByte(),
                            '2'.code.toByte(), 'C'.code.toByte()
                        ),
                    ))
                }
            }
            // --- Page Up / Page Down — bare PgUp routes through
            //     onScrollLines (alt-screen forwards ESC[5~; main-screen
            //     scrolls local transcript). Shift+PgUp routes through
            //     fireKey, which sends ESC[5;2~ — the byte sequence
            //     Claude Code's scroll binding actually fires on. ---
            item {
                ExtraKey(label = "PgUp", widthDp = 56) {
                    if (shift != StickyState.Off) {
                        fireKey(KeySpec(
                            label = "PgUp",
                            bytes = byteArrayOf(
                                0x1b, '['.code.toByte(),
                                '5'.code.toByte(), '~'.code.toByte()
                            ),
                            shiftBytes = byteArrayOf(
                                0x1b, '['.code.toByte(),
                                '5'.code.toByte(), ';'.code.toByte(),
                                '2'.code.toByte(), '~'.code.toByte()
                            ),
                        ))
                    } else {
                        onScrollLines(-10)
                    }
                }
            }
            item {
                ExtraKey(label = "PgDn", widthDp = 56) {
                    if (shift != StickyState.Off) {
                        fireKey(KeySpec(
                            label = "PgDn",
                            bytes = byteArrayOf(
                                0x1b, '['.code.toByte(),
                                '6'.code.toByte(), '~'.code.toByte()
                            ),
                            shiftBytes = byteArrayOf(
                                0x1b, '['.code.toByte(),
                                '6'.code.toByte(), ';'.code.toByte(),
                                '2'.code.toByte(), '~'.code.toByte()
                            ),
                        ))
                    } else {
                        onScrollLines(10)
                    }
                }
            }
            // --- Punctuation shortcuts ---
            item {
                ExtraKey(label = "@") {
                    fireKey(KeySpec("@", byteArrayOf('@'.code.toByte())))
                }
            }
            // --- Attach slot (CliAttachButton) — adjacent to the mic ---
            if (attachSlot != null) {
                item {
                    Box(
                        modifier = Modifier
                            .defaultMinSize(minWidth = 44.dp, minHeight = 36.dp),
                        contentAlignment = Alignment.Center,
                    ) {
                        attachSlot()
                    }
                }
            }
            // --- Mic slot (CliMicButton from Task 6.2) ---
            item {
                Box(
                    modifier = Modifier
                        .defaultMinSize(minWidth = 44.dp, minHeight = 36.dp),
                    contentAlignment = Alignment.Center,
                ) {
                    micSlot()
                }
            }
            // --- Hyphen ---
            item {
                ExtraKey(label = "-") {
                    fireKey(KeySpec("-", byteArrayOf('-'.code.toByte())))
                }
            }
            // --- Secondary keys (scroll right to reveal) ---
            item {
                ExtraKey(label = "Home", widthDp = 56) {
                    fireKey(KeySpec("Home", byteArrayOf(0x1b, '['.code.toByte(), 'H'.code.toByte())))
                }
            }
            item {
                ExtraKey(label = "End", widthDp = 52) {
                    fireKey(KeySpec("End", byteArrayOf(0x1b, '['.code.toByte(), 'F'.code.toByte())))
                }
            }
            }
        }
    }
}

/** Standard tappable key. Single tap fires onTap; long-press is ignored.
 *
 * Uses Modifier.clickable instead of pointerInput+detectTapGestures so we
 * get the system ripple animation as visible tap feedback (helps the
 * operator distinguish "my tap didn't land" from "my tap landed but the
 * action did nothing"). Clickable also coexists better with the parent
 * LazyRow's horizontal scroll: pointerInput captures all gestures
 * indiscriminately and can fight with the row's scroll handler at the
 * scroll-edge zones, while clickable cleanly defers to the parent on
 * dragging gestures.
 */
@Composable
private fun ExtraKey(
    label: String,
    widthDp: Int = 44,
    onTap: () -> Unit,
) {
    val shape = RoundedCornerShape(6.dp)
    Box(
        modifier = Modifier
            .defaultMinSize(minWidth = widthDp.dp, minHeight = 36.dp)
            .height(36.dp)
            .clip(shape)
            .background(MaterialTheme.colorScheme.surface)
            .border(
                BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
                shape,
            )
            .clickFeedback(onClick = { onTap() })
            .padding(horizontal = 8.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = label,
            style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.onSurface,
            fontFamily = FontFamily.Monospace,
            fontSize = 14.sp,
        )
    }
}

/**
 * Modifier key (Ctrl / Alt). Visually reflects sticky state:
 *   Off     — surface background, normal weight
 *   Pending — primary-tinted background ("armed")
 *   Locked  — primary background + accent border ("locked")
 */
@Composable
private fun ModifierKey(
    label: String,
    state: StickyState,
    onTap: () -> Unit,
    onLongPress: () -> Unit,
) {
    val shape = RoundedCornerShape(6.dp)
    val colors = MaterialTheme.colorScheme

    val bg: Color = when (state) {
        StickyState.Off -> colors.surface
        StickyState.Pending -> colors.primaryContainer
        StickyState.Locked -> colors.primary
    }
    val fg: Color = when (state) {
        StickyState.Off -> colors.onSurface
        StickyState.Pending -> colors.onPrimaryContainer
        StickyState.Locked -> colors.onPrimary
    }
    val border: BorderStroke = when (state) {
        StickyState.Locked -> BorderStroke(1.5.dp, colors.tertiary)
        StickyState.Pending -> BorderStroke(1.dp, colors.primary)
        StickyState.Off -> BorderStroke(1.dp, colors.outlineVariant)
    }
    val weight = if (state == StickyState.Locked) FontWeight.Bold else FontWeight.Medium

    Box(
        modifier = Modifier
            .defaultMinSize(minWidth = 52.dp, minHeight = 36.dp)
            .height(36.dp)
            .clip(shape)
            .background(bg)
            .border(border, shape)
            .pointerInput(label, state) {
                detectTapGestures(
                    onTap = { onTap() },
                    onLongPress = { onLongPress() },
                )
            }
            .padding(horizontal = 8.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = label,
            style = MaterialTheme.typography.labelLarge,
            color = fg,
            fontFamily = FontFamily.Monospace,
            fontWeight = weight,
            fontSize = 14.sp,
        )
    }
}
