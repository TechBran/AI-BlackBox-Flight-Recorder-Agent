package com.aiblackbox.portal.ui.feedback

import android.content.Context
import android.media.AudioManager
import android.view.HapticFeedbackConstants
import android.view.SoundEffectConstants
import android.view.View
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.composed
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.semantics.Role

// =============================================================================
// PressFeedback — one native-feeling press feedback for every button.
//
// Chosen feel ("Native"):
//   - ringer NORMAL (phone audible) -> system CLICK sound + a subtle KEYBOARD_TAP haptic
//   - ringer VIBRATE / SILENT       -> VIRTUAL_KEY haptic only
//
// Both primitives are still gated by the OS against the user's system settings
// (Touch sounds / Haptic feedback), so this never forces feedback a user has
// turned off. Centralizing here means the feel is tuned in exactly one place.
// =============================================================================

/**
 * Fire native press feedback on this View. Safe to call from any click handler.
 */
fun View.performPressFeedback() {
    val audible = (context.getSystemService(Context.AUDIO_SERVICE) as? AudioManager)
        ?.ringerMode == AudioManager.RINGER_MODE_NORMAL
    if (audible) {
        playSoundEffect(SoundEffectConstants.CLICK)
        performHapticFeedback(HapticFeedbackConstants.KEYBOARD_TAP)
    } else {
        performHapticFeedback(HapticFeedbackConstants.VIRTUAL_KEY)
    }
}

/**
 * Returns a stable lambda that fires [performPressFeedback] on the current View.
 * Use to add native feedback to a Material component's onClick without changing
 * its other behavior:
 *
 *   val feedback = rememberPressFeedback()
 *   IconButton(onClick = { feedback(); onSomething() }) { ... }
 */
@Composable
fun rememberPressFeedback(): () -> Unit {
    val view = LocalView.current
    return remember(view) { { view.performPressFeedback() } }
}

/**
 * Drop-in replacement for [Modifier.clickable] that fires native press feedback
 * before invoking [onClick]. Keeps clickable's default ripple/indication, so it
 * looks identical and only adds the feedback.
 *
 *   Box(modifier = Modifier.clickFeedback { onSomething() }) { ... }
 */
fun Modifier.clickFeedback(
    enabled: Boolean = true,
    onClickLabel: String? = null,
    role: Role? = null,
    onClick: () -> Unit,
): Modifier = composed {
    val view = LocalView.current
    clickable(enabled = enabled, onClickLabel = onClickLabel, role = role) {
        view.performPressFeedback()
        onClick()
    }
}

/**
 * Variant for the explicit-[MutableInteractionSource] form some call sites use
 * (e.g. to drive a custom indication). Mirrors [Modifier.clickable]'s signature.
 */
fun Modifier.clickFeedback(
    interactionSource: MutableInteractionSource,
    indication: androidx.compose.foundation.Indication?,
    enabled: Boolean = true,
    onClickLabel: String? = null,
    role: Role? = null,
    onClick: () -> Unit,
): Modifier = composed {
    val view = LocalView.current
    clickable(
        interactionSource = interactionSource,
        indication = indication,
        enabled = enabled,
        onClickLabel = onClickLabel,
        role = role,
    ) {
        view.performPressFeedback()
        onClick()
    }
}
