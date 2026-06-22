package com.aiblackbox.portal.ui.theme

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

// =============================================================================
// Glass Morphism System — ported from Portal CSS _variables.css
//
// Portal uses CSS backdrop-filter: blur() which blurs what's BEHIND the element.
// Compose's Modifier.blur() blurs the element's OWN CONTENT (including text!).
// There is no direct Compose equivalent for backdrop blur.
//
// Our approach: high-opacity semi-transparent backgrounds on pure-black (#000)
// screens produce an equivalent visual effect without blurring child content.
// The background opacity (0.75-0.95) provides the "frosted" look against the
// black app background, while borders and inset highlights add depth.
//
// CSS glass presets:
//   --glass-bg:              rgba(20, 20, 20, 0.75)
//   --glass-bg-hover:        rgba(30, 30, 30, 0.8)
//   --glass-border:          1px solid rgba(255,255,255,0.08)
//   --glass-border-hover:    1px solid rgba(255,255,255,0.14)
//   --glass-inset-highlight: inset 0 1px 0 rgba(255,255,255,0.04)
// =============================================================================

// Specific glass tints matching Portal's actual element backgrounds
// These are more opaque than the generic glass tokens for better readability
val GlassFloatingBubble = Color(0xF2222222)     // rgba(34,34,34,0.95) — topbar bubbles
val GlassComposerInput  = Color(0xFF1C1C1E)     // opaque (was 0.92) — textarea wrapper
val GlassProviderPill   = Color(0xFF1C1C1E)     // opaque (was 0.88) — provider/model bubble

@Immutable
data class BlackBoxGlass(
    // Blur radii (stored for reference; applied only to dedicated blur layers)
    val blurSm: Dp = 10.dp,   // --blur-sm
    val blurMd: Dp = 16.dp,   // --blur-md
    val blurLg: Dp = 20.dp,   // --blur-lg
    val blurXl: Dp = 24.dp,   // --blur-xl

    // Glass surface colors
    val background: Color = GlassBg,
    val backgroundHover: Color = GlassBgHover,
    val border: Color = GlassBorder,
    val borderHover: Color = GlassBorderHover,
    val insetHighlight: Color = GlassInsetHighlight,

    // Specific surface tints
    val floatingBubble: Color = GlassFloatingBubble,
    val composerInput: Color = GlassComposerInput,
    val providerPill: Color = GlassProviderPill,

    // Border width (1px in CSS → 1dp)
    val borderWidth: Dp = 1.dp,
)

val LocalBlackBoxGlass = staticCompositionLocalOf { BlackBoxGlass() }

// =============================================================================
// Glass Surface Modifier — applies the full glassmorphism look
//
// Usage:
//   Box(modifier = Modifier.glassSurface(shape = RoundedCornerShape(16.dp)))
//   Box(modifier = Modifier.glassSurface(shape = PillShape, bg = GlassFloatingBubble))
// =============================================================================

/**
 * Applies the Portal's glassmorphism look: semi-transparent background,
 * subtle border, and an inset top highlight for depth.
 *
 * IMPORTANT: Does NOT blur content. CSS backdrop-filter has no Compose
 * equivalent — the high-opacity background against our black app surface
 * produces the same frosted appearance without making text unreadable.
 *
 * @param shape The clip shape (e.g., RoundedCornerShape)
 * @param bg Override background color (defaults to glass token)
 * @param hovered Whether to use hover-state colors
 * @param elevation Shadow elevation in dp (0 = no shadow)
 */
/** Stronger border for floating UI elements that need to stand out */
val GlassBorderStrong = Color(0x40FFFFFF)  // rgba(255,255,255,0.25) — visible on black

fun Modifier.glassSurface(
    shape: Shape,
    bg: Color = Color.Unspecified,
    hovered: Boolean = false,
    elevation: Dp = 0.dp,
    borderOverride: Color = Color.Unspecified,
): Modifier {
    val background = when {
        bg != Color.Unspecified -> bg
        hovered -> GlassBgHover
        else -> GlassBg
    }
    val borderColor = when {
        borderOverride != Color.Unspecified -> borderOverride
        hovered -> GlassBorderHover
        else -> GlassBorder
    }
    val highlight = GlassInsetHighlight

    return this
        .then(
            if (elevation > 0.dp) {
                Modifier.shadow(elevation, shape, ambientColor = Color.Black, spotColor = Color.Black)
            } else {
                Modifier
            }
        )
        .clip(shape)
        .background(background, shape)
        .border(1.dp, borderColor, shape)
        .drawBehind {
            // Inset top highlight — mimics CSS inset 0 1px 0 rgba(255,255,255,0.04)
            drawLine(
                color = highlight,
                start = Offset(0f, 0f),
                end = Offset(size.width, 0f),
                strokeWidth = 1.dp.toPx(),
            )
        }
}
