package com.aiblackbox.portal.ui.theme

import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color

// =============================================================================
// Brand Colors — ported from Portal CSS _variables.css
// =============================================================================
val BbxBlack      = Color(0xFF000000)   // --bg
val BbxDark       = Color(0xFF0E0E10)   // dark surface (topbar, cards)
val BbxSurface    = Color(0xFF17181B)   // elevated surface
val BbxRed        = Color(0xFFE10600)   // brand red (containers)
val BbxAccent     = Color(0xFFFF4A4A)   // --accent (buttons, icons, labels)
val BbxWhite      = Color(0xFFFFFFFF)   // --text
val BbxDim        = Color(0xFFC9C9C9)   // --muted

// =============================================================================
// Neutral Scale — --neutral-0 through --neutral-1000
// =============================================================================
val Neutral0    = Color(0xFF000000)
val Neutral50   = Color(0xFF0A0A0A)
val Neutral100  = Color(0xFF141414)
val Neutral150  = Color(0xFF1A1A1A)
val Neutral200  = Color(0xFF222222)
val Neutral250  = Color(0xFF2C2C2C)
val Neutral300  = Color(0xFF333333)
val Neutral400  = Color(0xFF444444)
val Neutral500  = Color(0xFF555555)
val Neutral600  = Color(0xFF666666)
val Neutral700  = Color(0xFF888888)
val Neutral800  = Color(0xFFAAAAAA)
val Neutral850  = Color(0xFFBBBBBB)
val Neutral900  = Color(0xFFCCCCCC)
val Neutral950  = Color(0xFFDDDDDD)
val Neutral1000 = Color(0xFFE0E0E0)

// =============================================================================
// Semantic Colors
// =============================================================================
val BubbleAssistant = BbxBlack      // --bubble
// Assistant bubble: fully transparent so the ember backdrop glows THROUGH the
// generated text (matches the web, which clears the bubble entirely). Over the
// pure-black screen it reads identical when no embers are present (idle).
val BubbleAssistantBg = Color.Transparent
val BubbleUser      = Neutral250    // --bubble-user
val TextPrimary     = BbxWhite      // --text
val TextMuted       = BbxDim        // --muted
val Border          = Neutral300    // --border

// =============================================================================
// Syntax Highlighting — full set from Portal _variables.css
// =============================================================================
val HighlightSnapshot      = Color(0xFFBB86FC)  // --highlight-snapshot (Purple)
val HighlightCitation      = Color(0xFF4DD0E1)  // --highlight-citation (Cyan)
val HighlightHeader        = Color(0xFFFFA726)  // --highlight-header (Orange)
val HighlightKeyword       = Color(0xFF66BB6A)  // --highlight-keyword (Green)
val HighlightLink          = Color(0xFF64B5F6)  // --highlight-link (Light blue)
val HighlightNumber        = Color(0xFFFF4A4A)  // --highlight-number (Red)
val HighlightLabel         = Color(0xFF64B5F6)  // --highlight-label (Blue)
val HighlightQuoteMark     = Color(0xFFFFD700)  // --highlight-quote-mark (Gold)
val HighlightQuotedText    = Color(0xFFFFEB3B)  // --highlight-quoted-text (Yellow)
val HighlightSquareBracket = Color(0xFFFF6EC7)  // --highlight-square-bracket (Pink)
val HighlightRoundBracket  = Color(0xFF26C6DA)  // --highlight-round-bracket (Teal)
val HighlightBracketText   = Color(0xFF80DEEA)  // --highlight-bracket-text (Light teal)

// =============================================================================
// Glow Colors (for effects)
// =============================================================================
val GlowAccent = Color(0x40FF4A4A)  // --shadow-glow-accent @ 25% alpha
val GlowBlue   = Color(0x404A9EFF)  // --shadow-glow-blue @ 25% alpha
val GlowGreen  = Color(0x4027D980)  // --shadow-glow-green @ 25% alpha
val SolidGreen  = Color(0xFF27D980)

// Ember palette — the 5 cinematic-particle colors from the website ember system
// (Apps/landing-page/app.js initCinematicParticles('embers')). RGB → hex:
//   255,74,74 / 255,120,50 / 255,180,50 / 255,220,100 / 255,250,200
val EmberRed         = Color(0xFFFF4A4A)  // 255,74,74  (== BbxAccent)
val EmberOrange      = Color(0xFFFF7832)  // 255,120,50
val EmberYellowOrange = Color(0xFFFFB432) // 255,180,50
val EmberYellow      = Color(0xFFFFDC64)  // 255,220,100
val EmberWhiteHot    = Color(0xFFFFFAC8)  // 255,250,200
// Ordered palette for the weighted picker / per-color glow sprites (colorIndex 0..4).
val EmberColors: List<Color> = listOf(EmberRed, EmberOrange, EmberYellowOrange, EmberYellow, EmberWhiteHot)

// =============================================================================
// Computer Use — CU accent + status tokens, ported from Portal's
// #6c3bd1 / #8b5cf6 purple and _browser.css status colors.
// Single source of truth for the CU palette (values match Portal exactly).
// =============================================================================
val CuAccent       = Color(0xFF8B5CF6)   // CU purple accent
val CuAccentDark   = Color(0xFF6C3BD1)   // CU purple, dark variant
val CuAccentDim    = Color(0xFFC4B5FD)   // CU purple, dim labels/text
val CuAccentBg     = Color(0x146C3BD1)   // CU purple bg @ ~8% alpha
val CuAccentBorder = Color(0x408B5CF6)   // CU purple border @ ~25% alpha
val CuSuccess      = Color(0xFF4ADE80)   // CU status green
val CuSuccessGlow  = Color(0x804ADE80)   // CU status green @ 50% for glow
val CuError        = Color(0xFFF87171)   // CU status red
val CuWarning      = Color(0xFFF97316)   // CU status orange

// =============================================================================
// Glass Morphism — ported from Portal --glass-* tokens
// =============================================================================
val GlassBg             = Color(0xBF141414)  // --glass-bg: rgba(20,20,20,0.75)
val GlassBgHover        = Color(0xCC1E1E1E)  // --glass-bg-hover: rgba(30,30,30,0.8)
val GlassBorder         = Color(0x14FFFFFF)   // --glass-border: rgba(255,255,255,0.08)
val GlassBorderHover    = Color(0x24FFFFFF)   // --glass-border-hover: rgba(255,255,255,0.14)
val GlassInsetHighlight = Color(0x0AFFFFFF)   // --glass-inset-highlight: rgba(255,255,255,0.04)

// =============================================================================
// Focus Ring
// =============================================================================
val FocusRingColor = BbxAccent  // --focus-ring uses --accent

// =============================================================================
// Extended Color Palette — accessed via BlackBoxTheme.extendedColors
// =============================================================================
@Immutable
data class BlackBoxExtendedColors(
    // Brand
    val black: Color = BbxBlack,
    val dark: Color = BbxDark,
    val surface: Color = BbxSurface,
    val red: Color = BbxRed,
    val accent: Color = BbxAccent,
    val white: Color = BbxWhite,
    val dim: Color = BbxDim,

    // Semantic
    val bubbleAssistant: Color = BubbleAssistant,
    val bubbleUser: Color = BubbleUser,
    val textPrimary: Color = TextPrimary,
    val textMuted: Color = TextMuted,
    val border: Color = Border,

    // Syntax
    val highlightSnapshot: Color = HighlightSnapshot,
    val highlightCitation: Color = HighlightCitation,
    val highlightHeader: Color = HighlightHeader,
    val highlightKeyword: Color = HighlightKeyword,
    val highlightLink: Color = HighlightLink,
    val highlightNumber: Color = HighlightNumber,
    val highlightLabel: Color = HighlightLabel,
    val highlightQuoteMark: Color = HighlightQuoteMark,
    val highlightQuotedText: Color = HighlightQuotedText,
    val highlightSquareBracket: Color = HighlightSquareBracket,
    val highlightRoundBracket: Color = HighlightRoundBracket,
    val highlightBracketText: Color = HighlightBracketText,

    // Glow
    val glowAccent: Color = GlowAccent,
    val glowBlue: Color = GlowBlue,
    val glowGreen: Color = GlowGreen,
    val solidGreen: Color = SolidGreen,

    // Glass
    val glassBg: Color = GlassBg,
    val glassBgHover: Color = GlassBgHover,
    val glassBorder: Color = GlassBorder,
    val glassBorderHover: Color = GlassBorderHover,
    val glassInsetHighlight: Color = GlassInsetHighlight,

    // Focus
    val focusRing: Color = FocusRingColor,

    // Disabled
    val disabledAlpha: Float = 0.45f,  // from _base.css opacity for :disabled
)

val LocalBlackBoxColors = staticCompositionLocalOf { BlackBoxExtendedColors() }
